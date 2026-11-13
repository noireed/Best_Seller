"""Run one team-5 simulation cycle with a durable, encrypted state checkpoint."""
import base64
from collections import Counter
from datetime import datetime, time
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
from .storage import Store

HOST = "http://136.119.248.78"
REPOSITORY = "noireed/Best_Seller"
STATE_BRANCH = "aqua-team-5-state"
STATE_PATH = "simulation-state.enc"
DATABASES = ("trading.db", "news.db", "dart.db")


class Checkpoint:
    def __init__(self):
        self.cipher = Fernet(os.environ["AQUA_STATE_KEY"].encode())
        self.session = requests.Session()
        self.session.headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
        self.url = f"https://api.github.com/repos/{REPOSITORY}/contents/{STATE_PATH}"
        self.sha = None

    def restore(self, directory):
        response = self.session.get(self.url, params={"ref": STATE_BRANCH},
                                    headers={"Accept": "application/vnd.github.raw+json"}, timeout=20)
        response.raise_for_status()
        blob = response.content
        self.sha = hashlib.sha1(f"blob {len(blob)}\0".encode() + blob).hexdigest()
        payload = self.cipher.decrypt(blob)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if archive.read("status") != b"ready":
                raise RuntimeError("Previous cycle requires order reconciliation")
            for name in DATABASES:
                if name in archive.namelist():
                    (directory / name).write_bytes(archive.read(name))

    def save(self, directory, status):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("status", status)
            for name in DATABASES:
                path = directory / name
                if path.exists():
                    archive.write(path, name)
        encrypted = self.cipher.encrypt(buffer.getvalue())
        response = self.session.put(self.url, json={
            "message": "Checkpoint AQuA simulation",
            "branch": STATE_BRANCH,
            "sha": self.sha,
            "content": base64.b64encode(encrypted).decode(),
        }, timeout=30)
        response.raise_for_status()
        self.sha = response.json()["content"]["sha"]


def main():
    probe = os.environ.get("CLOUD_PROBE") == "1"
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    if not probe and (now.weekday() >= 5 or not time(9) <= now.time() < time(15)):
        print("Skipped outside simulation trading hours")
        return
    with TemporaryDirectory() as temp:
        directory = Path(temp)
        settings = Settings(
            _env_file=None, api_base_url=HOST, dry_run=probe, enable_trading=not probe,
            db_path=str(directory / DATABASES[0]),
            news_api_db_path=str(directory / DATABASES[1]),
            dart_db_path=str(directory / DATABASES[2]),
            news_enabled=not probe and bool(os.environ.get("GNEWS_API_KEY")),
            dart_enabled=not probe and bool(os.environ.get("DART_API_KEY")),
            research_comparison_enabled=False,
        )
        api = TradingAPI(HOST, settings.team_code)
        if str(api.login().get("teamId")) != "5":
            raise RuntimeError("Expected AQuA team 5")
        checkpoint = Checkpoint()
        checkpoint.restore(directory)
        if not probe:
            checkpoint.save(directory, "running")
        store = Store(settings.db_path)
        try:
            rows = TradingEngine(
                settings, api, LLMAnalyst("", settings.openai_model), store,
                news=build_news_service(settings) if settings.news_enabled else None,
                dart=build_dart_service(settings),
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
        print(json.dumps({"team": 5, "symbols": len(rows), "executions": counts}, ensure_ascii=True))
        if unresolved or counts.get("DATA_ERROR") == len(rows):
            raise RuntimeError("Simulation cycle requires review")


if __name__ == "__main__":
    main()
