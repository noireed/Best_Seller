"""Run one team-5 simulation cycle with a durable, encrypted state checkpoint."""
import base64
from collections import Counter
from datetime import datetime, time, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import zipfile
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet

from .api_client import APIError, TradingAPI
from .config import Settings
from .cloud_recovery import reconcile_orders
from .dart import build_dart_service
from .dart import DartClient
from .intraday_disclosures import IntradayDisclosures
from .engine import TradingEngine
from .llm_analyst import LLMAnalyst
from .news import build_news_service
from .news import parse_time
from .research_universe import with_research_aliases
from .storage import Store

HOST = "http://136.119.248.78"
REPOSITORY = "noireed/Best_Seller"
STATE_BRANCH = "aqua-team-5-state"
STATE_PATH = "simulation-state.enc"
DATABASES = ("trading.db", "news.db", "dart.db")
RESEARCH_FILES = ("research.json", "news.db", "dart.db")


class Checkpoint:
    def __init__(self, *, research=False):
        self.cipher = Fernet(os.environ["AQUA_STATE_KEY"].encode())
        self.session = requests.Session()
        self.session.headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
        path = "research-state.enc" if research else STATE_PATH
        self.url = f"https://api.github.com/repos/{REPOSITORY}/contents/{path}"
        self.files = RESEARCH_FILES if research else ("trading.db", "cycle.json", "intraday.db")
        self.sha = None

    def restore(self, directory, *, optional=False, recovery_sha=None, allow_recovery=False):
        response = self.session.get(self.url, params={"ref": STATE_BRANCH},
                                    headers={"Accept": "application/vnd.github.raw+json"}, timeout=20)
        if optional and response.status_code == 404:
            return
        response.raise_for_status()
        blob = response.content
        self.sha = hashlib.sha1(f"blob {len(blob)}\0".encode() + blob).hexdigest()
        payload = self.cipher.decrypt(blob)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            status = archive.read("status").decode()
            if status not in ("ready", "running", "blocked"):
                raise RuntimeError("Invalid checkpoint status")
            if status != "ready" and self.sha != recovery_sha and not allow_recovery:
                raise RuntimeError("Previous cycle requires order reconciliation")
            for name in self.files:
                if name in archive.namelist():
                    (directory / name).write_bytes(archive.read(name))
            return status

    def save(self, directory, status, *, message="Checkpoint AQuA simulation"):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("status", status)
            for name in self.files:
                path = directory / name
                if path.exists():
                    archive.write(path, name)
        encrypted = self.cipher.encrypt(buffer.getvalue())
        body = {
            "message": message,
            "branch": STATE_BRANCH,
            "content": base64.b64encode(encrypted).decode(),
        }
        if self.sha:
            body["sha"] = self.sha
        response = self.session.put(self.url, json=body, timeout=30)
        response.raise_for_status()
        self.sha = response.json()["content"]["sha"]


class ResearchSnapshot:
    def __init__(self, rows, source, max_age):
        self.rows, self.source, self.max_age = rows, source, max_age

    def collect(self, symbol):
        row = self.rows.get(symbol)
        now = datetime.now(ZoneInfo("UTC"))
        if row and parse_time(row["checked_at"]) <= now <= parse_time(row["checked_at"]) + self.max_age:
            report = row[self.source]
            if self.source != "news" or all(parse_time(a["observed_at"]) <= now for a in report["articles"]):
                return report
        return {"symbol": symbol, "status": "unavailable", "articles": [],
                "as_of": now.isoformat(), "reason": "research_snapshot_missing_or_stale"}


def refresh_research(directory, settings):
    checkpoint = Checkpoint(research=True)
    checkpoint.restore(directory, optional=True)
    news, dart = build_news_service(settings), build_dart_service(settings)
    if dart is None:
        raise RuntimeError("DART must be configured for research")
    rows = {}
    for symbol in settings.symbols:
        news_row, dart_row = news.collect(symbol), dart.collect(symbol)
        rows[symbol] = {"checked_at": datetime.now(ZoneInfo("UTC")).isoformat(),
                        "news": news_row, "dart": dart_row}
        print(json.dumps({"symbol": symbol, "news": news_row["status"],
                          "news_providers": news_row.get("providers", []),
                          "dart": dart_row["status"], "financial": dart_row.get("financial_status"),
                          "document": dart_row.get("document", {}).get("status")}), flush=True)
    (directory / "research.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    checkpoint.save(directory, "ready")


def overnight_resumed(metadata, now, rows, symbols):
    if (len(rows) != len(symbols) or {row.get("symbol") for row in rows} != set(symbols)
            or any(row.get("execution") == "DATA_ERROR" for row in rows)):
        return False
    if not metadata.get("standby_since") or not metadata.get("next_open_at"):
        return False
    opening = parse_time(metadata["next_open_at"]).astimezone(ZoneInfo("Asia/Seoul"))
    since = parse_time(metadata["standby_since"]).astimezone(ZoneInfo("Asia/Seoul"))
    verified = metadata.get("overnight_verified_at")
    return (since.date() < now.date() and opening <= now
            and now.weekday() < 5 and time(9) <= now.time() < time(15)
            and (not verified or parse_time(verified) < opening))


def main():
    probe = os.environ.get("CLOUD_PROBE") == "1"
    research = os.environ.get("CLOUD_RESEARCH") == "1"
    recovery_sha = os.environ.get("RECOVERY_SHA")
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    if not (probe or research or recovery_sha) and (now.weekday() >= 5 or not time(9) <= now.time() < time(15)):
        print("::notice::Skipped outside simulation trading hours at " + now.isoformat(), flush=True)
        return
    with TemporaryDirectory() as temp:
        directory = Path(temp)
        settings = with_research_aliases(Settings(
            _env_file=None, api_base_url=HOST, dry_run=probe, enable_trading=not probe,
            db_path=str(directory / DATABASES[0]),
            news_api_db_path=str(directory / DATABASES[1]),
            dart_db_path=str(directory / DATABASES[2]),
            news_enabled=not probe and bool(os.environ.get("GNEWS_API_KEY")),
            dart_enabled=not probe and bool(os.environ.get("DART_API_KEY")),
            gnews_cache_seconds=10800, dart_cache_seconds=14400,
            research_comparison_enabled=True,
        ))
        if research:
            refresh_research(directory, settings)
            return
        api = TradingAPI(HOST, settings.team_code)
        api.request_timeout = 8
        try:
            team_id = api.login().get("teamId")
        except APIError as exc:
            if exc.status is None or exc.status == 429 or exc.status >= 500:
                print(json.dumps({"status": "login_retry", "http_status": exc.status}), flush=True)
                raise SystemExit(75) from exc
            raise
        if str(team_id) != "5":
            raise RuntimeError("Expected AQuA team 5")
        checkpoint = Checkpoint()
        if recovery_sha:
            checkpoint.restore(directory, recovery_sha=recovery_sha)
            if checkpoint.sha != recovery_sha:
                raise RuntimeError("Recovery checkpoint changed")
            orders = api.orders(size=100)
            if (orders.get("totalPages") != 1 or len(orders["orders"]) != 1
                    or str(orders["orders"][0]["orderId"]) != "100"
                    or orders["orders"][0]["status"] != "FILLED"
                    or orders["orders"][0]["filledQuantity"] != 9):
                raise RuntimeError("Orders differ from the reconciled first-cycle history")
            checkpoint.save(directory, "ready")
            print("Recovered checkpoint after confirming the only server order is the earlier filled order")
            return
        cycle_path = directory / "cycle.json"
        cycle_metadata = {}
        if not probe:
            status = checkpoint.restore(directory, allow_recovery=True)
            if cycle_path.exists():
                cycle_metadata = json.loads(cycle_path.read_text(encoding="utf-8"))
            if status != "ready":
                if not cycle_metadata.get("durable_orders"):
                    print("::warning::Legacy interrupted checkpoint requires order reconciliation", flush=True)
                    raise SystemExit(75)
                recovery_store = Store(settings.db_path,
                                       order_checkpoint=lambda: checkpoint.save(directory, "running"))
                try:
                    recovered = reconcile_orders(recovery_store, api)
                finally:
                    recovery_store.db.close()
                checkpoint.save(directory, "ready" if recovered else "blocked")
                print(json.dumps({"status": "orders_reconciled" if recovered else "awaiting_order_confirmation"}),
                      flush=True)
                if not recovered:
                    raise SystemExit(75)
            if cycle_metadata.get("started_at"):
                previous = parse_time(cycle_metadata["started_at"])
                elapsed = (now - previous).total_seconds()
                if elapsed < 0:
                    raise RuntimeError("Previous cycle timestamp is in the future")
                if elapsed < 900:
                    print(json.dumps({"status": "recent_cycle", "previous": previous.isoformat(),
                                      "retry_after_seconds": 900 - elapsed,
                                      "next_cycle_at": (previous + timedelta(seconds=900)).isoformat()}), flush=True)
                    return
        Checkpoint(research=True).restore(directory, optional=True)
        path = directory / "research.json"
        research_rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not probe:
            cycle_metadata["durable_orders"] = True
            cycle_path.write_text(json.dumps(cycle_metadata), encoding="utf-8")
            checkpoint.save(directory, "running")
        store = Store(settings.db_path, order_checkpoint=(
            None if probe else lambda: checkpoint.save(directory, "running")))
        try:
            baseline_dart = ResearchSnapshot(research_rows, "dart", timedelta(hours=24))
            live_dart = (IntradayDisclosures(baseline_dart, DartClient(
                settings.dart_api_key, directory / "intraday.db", daily_budget=settings.dart_daily_budget))
                if not probe and settings.dart_api_key else baseline_dart)
            rows = TradingEngine(
                settings, api, LLMAnalyst("", settings.openai_model), store,
                news=ResearchSnapshot(research_rows, "news", timedelta(hours=48)),
                dart=live_dart,
            ).run_cycle(scan_only=probe)
            unresolved = store.unresolved_orders()
            if not probe:
                with store.db:
                    for table in ("decisions", "policy_decisions", "news_reports"):
                        store.db.execute(f"DELETE FROM {table} WHERE id <= (SELECT MAX(id)-120 FROM {table})")
                store.db.execute("VACUUM")
        finally:
            store.db.close()
        data_failed = bool(rows) and all(row.get("execution") == "DATA_ERROR" for row in rows)
        if not probe:
            verified = not unresolved and overnight_resumed(cycle_metadata, now, rows, settings.symbols)
            if not data_failed:
                cycle_metadata["started_at"] = now.isoformat()
                cycle_metadata["completed_at"] = datetime.now(ZoneInfo("Asia/Seoul")).isoformat()
                if verified:
                    cycle_metadata["overnight_verified_at"] = now.isoformat()
                cycle_path.write_text(json.dumps(cycle_metadata), encoding="utf-8")
            message = "Checkpoint AQuA simulation"
            if not unresolved and not data_failed:
                message = f"AQuA cycle completed: {len(rows)} symbols"
            if verified:
                message = f"AQuA overnight resume verified {now.date()}: {len(rows)} symbols"
                print(json.dumps({"status": "overnight_resume_verified", "at": now.isoformat()}), flush=True)
            checkpoint.save(directory, "blocked" if unresolved else "ready", message=message)
        counts = Counter(row.get("execution") for row in rows)
        for row in rows:
            comparison = row.get("policy_comparison", {})
            economic = row.get("economic_news", {})
            gemini = row.get("gemini_review", {})
            print(json.dumps({"symbol": row.get("symbol"), "action": row["action"], "execution": row["execution"],
                              "news": row.get("news", {}).get("status"), "dart": row.get("dart", {}).get("status"),
                              "news_adjustment": row.get("decision", {}).get("news_adjustment"),
                              "news_changed": comparison.get("news_changed_action"),
                              "dart_changed": comparison.get("dart_changed_action"),
                              "gemini_status": gemini.get("status"), "gemini_verdict": gemini.get("verdict"),
                              "gemini_http_status": gemini.get("http_status"),
                              "search_status": row.get("market_research", {}).get("status"),
                              "search_http_status": row.get("market_research", {}).get("http_status"),
                              "search_error_reasons": row.get("market_research", {}).get("error_reasons"),
                              "search_sources": len(row.get("market_research", {}).get("sources", [])),
                              "intraday_filings": len(row.get("dart", {}).get("intraday", {}).get("filings", [])),
                              "dart_checks": len(economic.get("dart_validation", {}).get("checks", []))}))
        print(json.dumps({"team": 5, "symbols": len(rows), "executions": counts}, ensure_ascii=True))
        if probe and settings.gemini_api_key and not any(
                row.get("market_research", {}).get("status") == "ok" for row in rows):
            raise RuntimeError("Cloud probe obtained no grounded Gemini research; inspect search statuses")
        if unresolved:
            print("::warning::Order confirmation pending; the next cycle will reconcile server history", flush=True)
            raise SystemExit(75)
        if data_failed:
            raise SystemExit(75)


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as exc:
        print(json.dumps({"status": "checkpoint_retry", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(75) from None
