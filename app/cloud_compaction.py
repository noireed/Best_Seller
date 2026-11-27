"""Bound encrypted state history without changing either live checkpoint blob."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

REPOSITORY = "noireed/Best_Seller"
BRANCH = "aqua-team-5-state"
FILES = ("research-state.enc", "simulation-state.enc")


def checkpoint_tree(raw):
    entries = {}
    for line in raw.splitlines():
        meta, path = line.split("\t", 1)
        mode, kind, identity = meta.split()
        if path not in FILES or path in entries or mode != "100644" or kind != "blob" or not re.fullmatch(r"[0-9a-f]{40}", identity):
            raise ValueError("Unexpected state tree entry")
        entries[path] = identity
    if set(entries) != set(FILES):
        raise ValueError("Both live checkpoint files are required")
    return entries


def compact(*, token, workspace=None, run=subprocess.run):
    if not token:
        raise ValueError("GitHub authentication is required")
    root = (Path(workspace) if workspace else Path.cwd()).resolve() / ".state-maintenance"
    root.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    for key in ("GIT_TRACE", "GIT_TRACE_CURL", "GIT_CURL_VERBOSE", "GIT_TRACE_PACKET"):
        env.pop(key, None)
    index = int(env.get("GIT_CONFIG_COUNT", "0"))
    env.update({"GIT_CONFIG_COUNT": str(index+1),
        f"GIT_CONFIG_KEY_{index}": "http.https://github.com/.extraheader",
        f"GIT_CONFIG_VALUE_{index}": "AUTHORIZATION: basic " + base64.b64encode(("x-access-token:"+token).encode()).decode()})
    remote = "https://github.com/" + REPOSITORY + ".git"
    try:
        with TemporaryDirectory(prefix="compact-", dir=root) as temp:
            directory = Path(temp).resolve()
            if directory.parent != root.resolve():
                raise ValueError("Invalid maintenance directory")

            def git(args, *, data=None, allow_failure=False):
                result = run(["git", *args], cwd=str(directory), input=data.encode("utf-8") if data is not None else None,
                             text=False, capture_output=True, env=env)
                if isinstance(result.stdout, bytes):
                    result.stdout = result.stdout.decode("utf-8", errors="replace")
                if isinstance(result.stderr, bytes):
                    result.stderr = result.stderr.decode("utf-8", errors="replace")
                if result.returncode and not allow_failure:
                    raise RuntimeError("State maintenance git operation failed: " + args[0])
                return result

            git(["init", "--bare", "."])
            for _ in range(4):
                # Depth one downloads current files, never the multi-gigabyte old history.
                git(["fetch", "--depth=1", remote, BRANCH])
                old = git(["rev-parse", "FETCH_HEAD"]).stdout.strip()
                entries = checkpoint_tree(git(["ls-tree", old, "--", *FILES]).stdout)
                tree = git(["mktree"], data="".join(
                    f"100644 blob {entries[path]}\t{path}\n" for path in FILES)).stdout.strip()
                new = git(["-c", "user.name=AQuA state maintenance", "-c",
                    "user.email=actions@users.noreply.github.com", "commit-tree", tree],
                    data="Compact encrypted simulation state history\n").stdout.strip()
                if checkpoint_tree(git(["ls-tree", new, "--", *FILES]).stdout) != entries:
                    raise ValueError("New checkpoint paths or hashes differ; refusing publication")
                result = git(["push", f"--force-with-lease=refs/heads/{BRANCH}:{old}", remote,
                              f"{new}:refs/heads/{BRANCH}"], allow_failure=True)
                if result.returncode:
                    if "stale info" in result.stderr or "fetch first" in result.stderr:
                        continue  # A live writer won. Fetch its newest checkpoint and retry.
                    raise RuntimeError("State maintenance push was rejected")
                sizes = {path: int(git(["cat-file", "-s", entries[path]]).stdout.strip()) for path in FILES}
                return {"status": "compacted", "branch": BRANCH, "previous_head": old,
                        "new_head": new, "checkpoint_blobs": entries, "retained_bytes": sum(sizes.values())}
            raise RuntimeError("Live state changed repeatedly; no stale checkpoint was published")
    finally:
        try:
            root.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    print(json.dumps(compact(token=os.environ.get("GH_TOKEN", ""))), flush=True)
