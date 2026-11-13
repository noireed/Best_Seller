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

from .api_client import TradingAPI
from .config import Settings
from .dart import build_dart_service
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
        self.files = RESEARCH_FILES if research else ("trading.db",)
        self.sha = None

    def restore(self, directory, *, optional=False):
        response = self.session.get(self.url, params={"ref": STATE_BRANCH},
                                    headers={"Accept": "application/vnd.github.raw+json"}, timeout=20)
        if optional and response.status_code == 404:
            return
        response.raise_for_status()
        blob = response.content
        self.sha = hashlib.sha1(f"blob {len(blob)}\0".encode() + blob).hexdigest()
        payload = self.cipher.decrypt(blob)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if archive.read("status") != b"ready":
                raise RuntimeError("Previous cycle requires order reconciliation")
            for name in self.files:
                if name in archive.namelist():
                    (directory / name).write_bytes(archive.read(name))

    def save(self, directory, status):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("status", status)
            for name in self.files:
                path = directory / name
                if path.exists():
                    archive.write(path, name)
        encrypted = self.cipher.encrypt(buffer.getvalue())
        body = {
            "message": "Checkpoint AQuA simulation",
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
        if row and parse_time(row["checked_at"]) <= datetime.now(ZoneInfo("UTC")) <= parse_time(row["checked_at"]) + self.max_age:
            return row[self.source]
        return {"symbol": symbol, "status": "unavailable", "articles": [],
                "as_of": datetime.now(ZoneInfo("UTC")).isoformat(), "reason": "research_snapshot_missing_or_stale"}


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


def main():
    probe = os.environ.get("CLOUD_PROBE") == "1"
    research = os.environ.get("CLOUD_RESEARCH") == "1"
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    if not (probe or research) and (now.weekday() >= 5 or not time(9) <= now.time() < time(15)):
        print("Skipped outside simulation trading hours")
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
        if str(api.login().get("teamId")) != "5":
            raise RuntimeError("Expected AQuA team 5")
        checkpoint = Checkpoint()
        if not probe:
            checkpoint.restore(directory)
        Checkpoint(research=True).restore(directory, optional=True)
        path = directory / "research.json"
        research_rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not probe:
            checkpoint.save(directory, "running")
        store = Store(settings.db_path)
        try:
            rows = TradingEngine(
                settings, api, LLMAnalyst("", settings.openai_model), store,
                news=ResearchSnapshot(research_rows, "news", timedelta(hours=48)),
                dart=ResearchSnapshot(research_rows, "dart", timedelta(hours=24)),
            ).run_cycle(scan_only=probe)
            unresolved = store.unresolved_orders()
            if not probe:
                with store.db:
                    for table in ("decisions", "policy_decisions", "news_reports"):
                        store.db.execute(f"DELETE FROM {table} WHERE id <= (SELECT MAX(id)-120 FROM {table})")
                store.db.execute("VACUUM")
        finally:
            store.db.close()
        if not probe:
            checkpoint.save(directory, "blocked" if unresolved else "ready")
        counts = Counter(row.get("execution") for row in rows)
        for row in rows:
            comparison = row.get("policy_comparison", {})
            economic = row.get("economic_news", {})
            print(json.dumps({"symbol": row.get("symbol"), "action": row["action"], "execution": row["execution"],
                              "news": row.get("news", {}).get("status"), "dart": row.get("dart", {}).get("status"),
                              "news_adjustment": row.get("decision", {}).get("news_adjustment"),
                              "news_changed": comparison.get("news_changed_action"),
                              "dart_changed": comparison.get("dart_changed_action"),
                              "dart_checks": len(economic.get("dart_validation", {}).get("checks", []))}))
        print(json.dumps({"team": 5, "symbols": len(rows), "executions": counts}, ensure_ascii=True))
        if unresolved or counts.get("DATA_ERROR") == len(rows):
            raise RuntimeError("Simulation cycle requires review")


if __name__ == "__main__":
    main()
