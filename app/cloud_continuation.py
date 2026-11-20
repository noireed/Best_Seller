# 개발 담당: 최준혁
"""Queue a successor before a continuous session starts doing work."""
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def reserve_successor():
    repository = os.environ["GITHUB_REPOSITORY"]
    payload = json.dumps({"ref": "main", "inputs": {"trade": True, "session": True}}).encode()
    request = Request(
        f"https://api.github.com/repos/{repository}/actions/workflows/simulation.yml/dispatches",
        data=payload,
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Content-Type": "application/json", "Accept": "application/vnd.github+json"},
        method="POST",
    )
    for attempt in range(4):
        if attempt:
            time.sleep(15 * attempt)
        try:
            with urlopen(request, timeout=20) as response:
                if response.status != 204:
                    raise RuntimeError("Continuation was not accepted")
            print(json.dumps({"status": "successor_reserved"}), flush=True)
            return
        except (HTTPError, URLError, TimeoutError):
            print(json.dumps({"status": "successor_reservation_retry"}), flush=True)
    raise RuntimeError("Cannot reserve a successor")


if __name__ == "__main__":
    reserve_successor()
