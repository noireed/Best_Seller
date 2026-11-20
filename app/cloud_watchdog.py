# 개발 담당: 최준혁
"""Restart a terminated continuous session when no replacement is queued."""
import json
import os

import requests

from .cloud_cycle import REPOSITORY
from .cloud_session import handoff


def is_continuous(run):
    return run.get("event") == "schedule" or run.get("display_title") == "AQuA continuous session"


def recover_session(run_id):
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    base = f"https://api.github.com/repos/{REPOSITORY}"

    def get(path, **params):
        response = session.get(base + path, params=params, timeout=20)
        response.raise_for_status()
        return response.json()

    run = get(f"/actions/runs/{run_id}")
    if run.get("head_branch") != "main" or not is_continuous(run):
        return "not_continuous"
    jobs = get(f"/actions/runs/{run_id}/jobs")["jobs"]
    if not any(job["name"] == "trade" and job["conclusion"] != "skipped" for job in jobs):
        return "not_trade"
    workflow = get("/actions/workflows/simulation.yml")
    if workflow["state"] != "active":
        return "disabled"
    runs = get("/actions/workflows/simulation.yml/runs", branch="main", per_page=100)["workflow_runs"]
    for candidate in runs:
        if candidate["status"] == "completed" or not is_continuous(candidate):
            continue
        jobs = get(f'/actions/runs/{candidate["id"]}/jobs')["jobs"]
        # Newly queued runs may not have jobs yet.
        if not jobs or any(job["name"] == "trade" and job["conclusion"] != "skipped" for job in jobs):
            return "replacement_active"
    handoff()
    return "restarted"


if __name__ == "__main__":
    result = recover_session(int(os.environ["COMPLETED_RUN_ID"]))
    print(json.dumps({"status": result}), flush=True)
