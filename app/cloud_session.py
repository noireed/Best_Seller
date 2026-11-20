# 개발 담당: 최준혁
"""Keep a cloud runner checking the market without waiting for each cron event."""
from datetime import datetime, time, timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys
import time as timer
from zoneinfo import ZoneInfo

import requests

from .cloud_cycle import Checkpoint, REPOSITORY
from .news import parse_time

KST = ZoneInfo("Asia/Seoul")
SESSION_SECONDS = 330 * 60
CYCLE_SECONDS = 900


def handoff():
    for delay in (0, 5, 15, 30):
        if delay:
            timer.sleep(delay)
        try:
            response = requests.post(
                f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/simulation.yml/dispatches",
                headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                         "Accept": "application/vnd.github+json"},
                json={"ref": "main", "inputs": {"trade": True, "session": True}},
                timeout=20,
            )
            response.raise_for_status()
            print(json.dumps({"status": "handoff_requested"}), flush=True)
            return
        except requests.RequestException:
            print(json.dumps({"status": "handoff_retry"}), flush=True)
    raise RuntimeError("Cloud session handoff failed after four attempts")


def next_market_open(now):
    candidate = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def record_standby(now, opening):
    with TemporaryDirectory() as temp:
        directory = Path(temp)
        checkpoint = Checkpoint()
        status = checkpoint.restore(directory, allow_recovery=True)
        path = directory / "cycle.json"
        metadata = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        since = metadata.get("standby_since")
        verified = metadata.get("overnight_verified_at")
        if not since or (verified and parse_time(verified) >= parse_time(since)):
            metadata["standby_since"] = now.isoformat()
        metadata["next_open_at"] = opening.isoformat()
        path.write_text(json.dumps(metadata), encoding="utf-8")
        checkpoint.save(directory, status, message="AQuA standby until " + opening.isoformat())


def run_session(duration_seconds=None):
    deadline = timer.monotonic() + (SESSION_SECONDS if duration_seconds is None else duration_seconds)
    next_cycle = None
    waiting_until = None
    while True:
        now = datetime.now(KST)
        if timer.monotonic() >= deadline - 600:
            if os.environ.get("CLOUD_CONTINUATION_RESERVED") == "true":
                print(json.dumps({"status": "handoff_ready"}), flush=True)
            else:
                handoff()
            return
        if now.weekday() >= 5 or not time(9) <= now.time() < time(15):
            opening = next_market_open(now)
            if waiting_until != opening:
                try:
                    record_standby(now, opening)
                except requests.RequestException:
                    print(json.dumps({"status": "standby_checkpoint_retry"}), flush=True)
                    timer.sleep(60)
                    continue
                print(json.dumps({"status": "waiting_for_open", "at": now.isoformat(),
                                  "next_open_at": opening.isoformat()}), flush=True)
                waiting_until = opening
            next_cycle = None
            timer.sleep(min(30, (opening - now).total_seconds()))
            continue
        waiting_until = None
        if next_cycle is not None and now < next_cycle:
            timer.sleep(min(30, (next_cycle - now).total_seconds()))
            continue
        print(json.dumps({"status": "cycle_started", "at": now.isoformat()}), flush=True)
        # A timed-out cycle may contain an accepted order. Its checkpoint handles reconciliation.
        try:
            subprocess.run([sys.executable, "-u", "-m", "app.cloud_cycle"],
                           check=True, timeout=600)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            if isinstance(exc, subprocess.CalledProcessError) and exc.returncode == 78:
                raise
            next_cycle = datetime.now(KST) + timedelta(seconds=60)
            print(json.dumps({"status": "cycle_retry", "error_type": type(exc).__name__,
                              "next_cycle_at": next_cycle.isoformat()}), flush=True)
            continue
        next_cycle = now + timedelta(seconds=CYCLE_SECONDS)
        finished = datetime.now(KST)
        if next_cycle <= finished:
            next_cycle = finished + timedelta(seconds=CYCLE_SECONDS)
        print(json.dumps({"status": "cycle_completed", "at": finished.isoformat(),
                          "next_cycle_at": next_cycle.isoformat()}), flush=True)


if __name__ == "__main__":
    if os.environ.get("CLOUD_SESSION") == "true":
        minutes = int(os.environ.get("CLOUD_SESSION_MINUTES", "330"))
        if not 11 <= minutes <= 330:
            raise ValueError("Session duration must be between 11 and 330 minutes")
        run_session(duration_seconds=minutes * 60)
    else:
        from .cloud_cycle import main
        main()
