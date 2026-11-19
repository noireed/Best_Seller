# 개발 담당: 최준혁
"""Keep a cloud runner checking the market without waiting for each cron event."""
from datetime import datetime, time, timedelta
import json
import os
import subprocess
import sys
import time as timer
from zoneinfo import ZoneInfo

import requests

from .cloud_cycle import REPOSITORY

KST = ZoneInfo("Asia/Seoul")
SESSION_SECONDS = 330 * 60
CYCLE_SECONDS = 900


def handoff():
    response = requests.post(
        f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/simulation.yml/dispatches",
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Accept": "application/vnd.github+json"},
        json={"ref": "main", "inputs": {"trade": True, "session": True}},
        timeout=20,
    )
    response.raise_for_status()
    print(json.dumps({"status": "handoff_requested"}), flush=True)


def run_session():
    deadline = timer.monotonic() + SESSION_SECONDS
    next_cycle = None
    waiting = False
    while True:
        now = datetime.now(KST)
        if now.weekday() >= 5 or now.time() >= time(15):
            print(json.dumps({"status": "market_closed", "at": now.isoformat()}), flush=True)
            return
        if timer.monotonic() >= deadline - 600:
            handoff()
            return
        if now.time() < time(9):
            if not waiting:
                print(json.dumps({"status": "waiting_for_open", "at": now.isoformat()}), flush=True)
                waiting = True
            timer.sleep(30)
            continue
        if next_cycle is not None and now < next_cycle:
            timer.sleep(min(30, (next_cycle - now).total_seconds()))
            continue
        print(json.dumps({"status": "cycle_started", "at": now.isoformat()}), flush=True)
        # A timed-out cycle may contain an accepted order. Its checkpoint handles reconciliation.
        subprocess.run([sys.executable, "-u", "-m", "app.cloud_cycle"],
                       check=True, timeout=600)
        next_cycle = now + timedelta(seconds=CYCLE_SECONDS)
        finished = datetime.now(KST)
        if next_cycle <= finished:
            next_cycle = finished + timedelta(seconds=CYCLE_SECONDS)
        print(json.dumps({"status": "cycle_completed", "at": finished.isoformat(),
                          "next_cycle_at": next_cycle.isoformat()}), flush=True)


if __name__ == "__main__":
    if os.environ.get("CLOUD_SESSION") == "true":
        run_session()
    else:
        from .cloud_cycle import main
        main()
