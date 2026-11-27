# 개발 담당: 최준혁
"""Project-billed Gemini transport with durable accounting and bounded reuse."""
import hashlib
import json
import re
import time
from datetime import timedelta

import requests

from .news import parse_time


class GeminiClient:
    def __init__(self, settings, store, *, session=None, clock=None, sleep=None):
        self.settings, self.store = settings, store
        self.key = getattr(settings, "gemini_api_key", "")
        self.session = session or requests.Session()
        # Developer and Vertex quotas are separate. Past paid-search reservations
        # must not exhaust the newly selected free-analysis allowance.
        self.account = hashlib.sha256((getattr(settings, "gemini_backend", "developer") + "\0" + self.key).encode()).hexdigest()
        self.reserved = 0
        self.reserved_day = None
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.last_call = None
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS gemini_usage(account TEXT, day TEXT, used INTEGER,
                PRIMARY KEY(account,day));
            CREATE TABLE IF NOT EXISTS gemini_cache(account TEXT, identity TEXT, at TEXT, report TEXT,
                PRIMARY KEY(account,identity));
        """)
        store.db.commit()

    def generate(self, purpose, payload, now):
        s = self.settings
        if not self.key:
            return {"status": "disabled"}
        backend = getattr(s, "gemini_backend", "developer")
        if backend == "developer" and payload.get("tools"):
            return {"status": "paid_tools_disabled"}
        if (not re.fullmatch(r"gemini-[a-z0-9.-]+", s.gemini_model)
                or backend == "vertex" and not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", s.gemini_project)):
            return {"status": "invalid_configuration"}
        # Only public evidence is passed in payload. The key never enters cache material.
        identity = hashlib.sha256(json.dumps(["v2", backend, s.gemini_project, s.gemini_model, purpose, payload],
            sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT at,report FROM gemini_cache WHERE account=? AND identity=?",
                             (self.account, identity)).fetchone()
            if row and 0 <= (now-parse_time(row[0])).total_seconds() < s.gemini_cache_seconds:
                report = json.loads(row[1])
                # Never extend the original observation time on a cache hit.
                db.commit()
                return {**report, "cached": True}
            day = now.date().isoformat()
            if self.reserved_day != day:
                self.reserved = 0
                self.reserved_day = day
            checkpoint_needed = self.reserved == 0
            if checkpoint_needed:
                used = db.execute("SELECT used FROM gemini_usage WHERE account=? AND day=?",
                                  (self.account, day)).fetchone()
                remaining = s.gemini_daily_budget - (used[0] if used else 0)
                if remaining <= 0:
                    db.commit()
                    return {"status": "daily_budget", "observed_at": now.isoformat()}
                block_size = s.gemini_reviews_per_cycle if backend == "developer" else 2 * len(s.symbols)
                block = min(block_size, remaining)
                db.execute("INSERT INTO gemini_usage VALUES(?,?,?) ON CONFLICT(account,day) DO UPDATE SET used=used+excluded.used",
                           (self.account, day, block))
                self.reserved = block
            db.commit()
        except Exception:
            db.rollback()
            raise
        # Reserve a whole batch once, avoiding one Git commit per search. Unused reservations
        # are not refunded after crashes, so replacement runners cannot overspend this counter.
        if checkpoint_needed:
            try:
                self.store.order_checkpoint()
            except Exception:
                self.reserved = 0
                raise
        self.reserved -= 1
        if backend == "developer":
            if self.last_call is not None:
                self.sleep(max(0, s.gemini_min_interval_seconds - (self.clock()-self.last_call)))
            self.last_call = self.clock()
            url = "https://generativelanguage.googleapis.com/v1beta/models/" + s.gemini_model + ":generateContent"
        else:
            url = ("https://aiplatform.googleapis.com/v1/projects/" + s.gemini_project
                   + "/locations/global/publishers/google/models/" + s.gemini_model + ":generateContent")
        try:
            response = self.session.post(url, headers={"x-goog-api-key": self.key},
                json=payload, timeout=(5, 35), allow_redirects=False)
            if response.status_code != 200:
                error = response.json().get("error", {})
                reasons = [d.get("reason") for d in error.get("details", []) if d.get("reason")]
                report = {"status": "http_error", "http_status": response.status_code,
                          "error_status": error.get("status"), "error_reasons": reasons}
            else:
                body = response.json()
                candidate = body.get("candidates", [{}])[0]
                content = "\n".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", [])
                                    if not p.get("thought"))
                report = {"status": "ok" if content.strip() else "empty_response", "text": content,
                          "grounding": candidate.get("groundingMetadata", {}),
                          "usage": body.get("usageMetadata", {}), "finish_reason": candidate.get("finishReason")}
        except (requests.RequestException, ValueError, TypeError, KeyError, IndexError, AttributeError):
            report = {"status": "transport_or_format_error"}
        report.update(observed_at=now.isoformat(), model=s.gemini_model, project=s.gemini_project, backend=backend)
        # Error responses are cached too: a provider error is visible, never an approval.
        db.execute("INSERT OR REPLACE INTO gemini_cache VALUES(?,?,?,?)",
                   (self.account, identity, now.isoformat(), json.dumps(report, ensure_ascii=False, allow_nan=False)))
        db.execute("DELETE FROM gemini_cache WHERE at < ?", ((now-timedelta(seconds=s.gemini_cache_seconds)).isoformat(),))
        db.commit()
        return report

    def release_reservations(self):
        # Successful cycle completion can refund slots that were never attempted.
        # A crashed process cannot refund, retaining conservative crash accounting.
        if self.reserved:
            self.store.db.execute("UPDATE gemini_usage SET used=MAX(0,used-?) WHERE account=? AND day=?",
                                  (self.reserved, self.account, self.reserved_day))
            self.store.db.commit()
            self.reserved = 0
