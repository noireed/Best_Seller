# 개발 담당: 최준혁
"""Persistent GNews cache, atomic local request budget and failure cooldowns."""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .news import NewsError


class DurableNewsClient:
    provider = "gnews"

    def __init__(self, client, db_path, *, daily_limit=80, ttl_seconds=3600,
                 min_interval=2.1, clock=None, sleep=None):
        self.client, self.db_path = client, str(db_path)
        self.delay_hours = client.delay_hours
        self.daily_limit, self.ttl_seconds = daily_limit, ttl_seconds
        self.min_interval = min_interval
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or time.sleep
        self.namespace = hashlib.sha256(client.api_key.encode()).hexdigest()
        self.last_observed_at, self.last_cache_hit = None, False
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS news_usage(account TEXT, day TEXT, requests INTEGER,
                    PRIMARY KEY(account,day));
                CREATE TABLE IF NOT EXISTS news_control(account TEXT PRIMARY KEY,
                    next_request REAL NOT NULL DEFAULT 0, blocked_until REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS news_cache(account TEXT, request TEXT, observed_at TEXT,
                    expires_at REAL, payload TEXT, PRIMARY KEY(account,request));
                CREATE TABLE IF NOT EXISTS news_attempts(id INTEGER PRIMARY KEY, account TEXT,
                    at TEXT, request TEXT, status TEXT);
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _now(self):
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("clock must include timezone")
        return now.astimezone(timezone.utc)

    def _reserve(self):
        # Reservation counts a crashed/uncertain request too; never refund unknown attempts.
        for _ in range(3):
            now = self._now()
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                control = db.execute("SELECT next_request,blocked_until FROM news_control WHERE account=?",
                                     (self.namespace,)).fetchone() or (0, 0)
                if control[1] > now.timestamp():
                    raise NewsError("NEWS_API_COOLDOWN: 일시 호출 중지")
                used = db.execute("SELECT requests FROM news_usage WHERE account=? AND day=?",
                                  (self.namespace, now.date().isoformat())).fetchone()
                if used and used[0] >= self.daily_limit:
                    raise NewsError("NEWS_DAILY_BUDGET: 로컬 일일 호출 예산 소진")
                wait = max(0, control[0] - now.timestamp())
                if wait == 0:
                    db.execute("INSERT INTO news_usage VALUES(?,?,1) ON CONFLICT(account,day) DO UPDATE SET requests=requests+1",
                               (self.namespace, now.date().isoformat()))
                    db.execute("INSERT INTO news_control(account,next_request) VALUES(?,?) ON CONFLICT(account) DO UPDATE SET next_request=excluded.next_request",
                               (self.namespace, now.timestamp() + self.min_interval))
                    return
            if wait > 3:
                raise NewsError("NEWS_REQUEST_SPACING: 호출 간격 대기 중")
            self.sleep(wait + .01)
        raise NewsError("NEWS_REQUEST_SPACING: 다른 프로세스의 호출 대기 중")

    def search(self, query, display=10):
        if not self.client.api_key:
            raise NewsError("GNEWS_API_KEY 미설정")
        if not isinstance(query, str) or not query.strip() or len(query) > 200 or not 1 <= display <= 100:
            raise NewsError("invalid query or display")
        self.last_cache_hit = False
        request = hashlib.sha256(json.dumps([query, min(display, 10), "en", "publishedAt"]).encode()).hexdigest()
        now = self._now()
        with self._connect() as db:
            row = db.execute("SELECT observed_at,expires_at,payload FROM news_cache WHERE account=? AND request=?",
                             (self.namespace, request)).fetchone()
        if row and row[1] > now.timestamp():
            self.last_observed_at, self.last_cache_hit = row[0], True
            try:
                cached = json.loads(row[2])
                if not isinstance(cached, list):
                    raise ValueError("invalid cache payload")
                return cached
            except (ValueError, TypeError):
                raise NewsError("NEWS_CACHE_UNREADABLE: 로컬 캐시 형식 오류") from None
        self._reserve()
        try:
            items = self.client.search(query, display)
            if not isinstance(items, list):
                raise NewsError("invalid provider items")
            payload = json.dumps(items, ensure_ascii=False, allow_nan=False)
        except Exception as exc:
            now = self._now()
            status = exc.status if isinstance(exc, NewsError) else None
            if status == 400:
                with self._connect() as db:
                    db.execute("INSERT INTO news_attempts(account,at,request,status) VALUES(?,?,?,?)",
                               (self.namespace, now.isoformat(), request, "400"))
                raise NewsError("NEWS_QUERY_REJECTED: HTTP 400", status=400) from None
            if status == 403:
                until = datetime.combine(now.date()+timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()
            else:
                seconds = 3600 if status == 401 else 60
                if isinstance(exc, NewsError) and exc.retry_after:
                    try:
                        seconds = max(seconds, min(86400, float(exc.retry_after)))
                    except (ValueError, TypeError):
                        try:
                            retry_at = parsedate_to_datetime(exc.retry_after)
                            seconds = max(seconds, min(86400, (retry_at-now).total_seconds()))
                        except (ValueError, TypeError, OverflowError):
                            pass
                until = now.timestamp()+seconds
            with self._connect() as db:
                db.execute("UPDATE news_control SET blocked_until=MAX(blocked_until,?) WHERE account=?",
                           (until, self.namespace))
                db.execute("INSERT INTO news_attempts(account,at,request,status) VALUES(?,?,?,?)",
                           (self.namespace, now.isoformat(), request, str(status or 'transport_error')))
            raise NewsError(f"NEWS_PROVIDER_FAILURE: HTTP {status or 'unknown'}; cooldown active", status=status) from None
        observed = self._now()
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO news_cache VALUES(?,?,?,?,?)",
                       (self.namespace, request, observed.isoformat(), observed.timestamp()+self.ttl_seconds, payload))
            db.execute("INSERT INTO news_attempts(account,at,request,status) VALUES(?,?,?,?)",
                       (self.namespace, observed.isoformat(), request, "200"))
        self.last_observed_at = observed.isoformat()
        return items

    def health(self):
        now = self._now()
        with self._connect() as db:
            used = db.execute("SELECT requests FROM news_usage WHERE account=? AND day=?",
                              (self.namespace, now.date().isoformat())).fetchone()
            control = db.execute("SELECT blocked_until FROM news_control WHERE account=?", (self.namespace,)).fetchone()
            cached = db.execute("SELECT COUNT(*) FROM news_cache WHERE account=? AND expires_at>?",
                                (self.namespace, now.timestamp())).fetchone()[0]
            outcomes = dict(db.execute("SELECT status,COUNT(*) FROM news_attempts WHERE account=? AND at>=? GROUP BY status",
                                      (self.namespace, now.date().isoformat())).fetchall())
        used = used[0] if used else 0
        return {"provider": "gnews", "utc_day": now.date().isoformat(), "local_attempts": used,
                "local_daily_limit": self.daily_limit, "remaining_local_budget": max(0, self.daily_limit-used),
                "valid_cache_entries": cached, "cooldown_active": bool(control and control[0]>now.timestamp()),
                "next_retry_at": datetime.fromtimestamp(control[0], timezone.utc).isoformat() if control and control[0]>now.timestamp() else None,
                "daily_reset_at": datetime.combine(now.date()+timedelta(days=1), datetime.min.time(), timezone.utc).isoformat(),
                "completed_request_outcomes": outcomes,
                "scope": "same API key and same SQLite file only; not server usage"}
