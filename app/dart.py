# 개발 담당: 최준혁
"""Read-only Open DART adapter. Current observations, not historical snapshots."""
from contextlib import contextmanager
from calendar import monthrange
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile

import requests


class DartError(Exception):
    pass


class DartClient:
    BASE = "https://opendart.fss.or.kr/api/"

    def __init__(self, api_key, db_path="data/dart_cache.db", *, ttl_seconds=3600,
                 daily_budget=500, session=None, clock=None):
        self.api_key, self.db_path = api_key, str(db_path)
        self.ttl_seconds, self.daily_budget = ttl_seconds, daily_budget
        self.session = session or requests.Session()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.account = hashlib.sha256(api_key.encode()).hexdigest()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS dart_cache(account TEXT, request TEXT,
                    observed TEXT, expires REAL, payload TEXT, PRIMARY KEY(account,request));
                CREATE TABLE IF NOT EXISTS dart_usage(account TEXT, day TEXT, used INTEGER,
                    PRIMARY KEY(account,day));
                CREATE TABLE IF NOT EXISTS dart_control(account TEXT PRIMARY KEY, blocked REAL);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _now(self):
        now = self.clock()
        if now.tzinfo is None:
            raise DartError("DART_CLOCK_REQUIRES_TIMEZONE")
        return now.astimezone(timezone.utc)

    def _request(self, endpoint, params, *, ttl=None):
        if not self.api_key:
            raise DartError("DART_API_KEY 미설정")
        now = self._now()
        request = json.dumps([endpoint, params], sort_keys=True)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT observed,expires,payload FROM dart_cache WHERE account=? AND request=?",
                             (self.account, request)).fetchone()
            if row and row[1] > now.timestamp() and datetime.fromisoformat(row[0]) <= now:
                return json.loads(row[2]), row[0]
            blocked = db.execute("SELECT blocked FROM dart_control WHERE account=?", (self.account,)).fetchone()
            if blocked and blocked[0] > now.timestamp():
                raise DartError("DART_COOLDOWN")
            day = now.date().isoformat()
            used = db.execute("SELECT used FROM dart_usage WHERE account=? AND day=?", (self.account, day)).fetchone()
            if used and used[0] >= self.daily_budget:
                raise DartError("DART_LOCAL_DAILY_BUDGET")
            db.execute("INSERT INTO dart_usage VALUES(?,?,1) ON CONFLICT(account,day) DO UPDATE SET used=used+1",
                       (self.account, day))
        try:
            response = self.session.get(self.BASE + endpoint,
                params={**params, "crtfc_key": self.api_key}, timeout=15)
            if response.status_code != 200:
                raise DartError("DART_HTTP_FAILURE")
            if endpoint == "corpCode.xml":
                content = response.content
                if len(content) > 20_000_000:
                    raise DartError("DART_CORP_FILE_TOO_LARGE")
                try:
                    with zipfile.ZipFile(io.BytesIO(content)) as archive:
                        info = archive.getinfo("CORPCODE.xml")
                        if info.file_size > 100_000_000:
                            raise DartError("DART_CORP_XML_TOO_LARGE")
                        content = archive.read(info)
                except zipfile.BadZipFile:
                    root = ET.fromstring(content)
                    status = root.findtext("status", "")
                    safe_status = status if re.fullmatch(r"\d{3}", status) else "INVALID_XML"
                    raise DartError("DART_STATUS_" + safe_status) from None
                root = ET.fromstring(content)
                payload = {row.findtext("stock_code", "").strip(): row.findtext("corp_code", "")
                           for row in root.findall("list") if row.findtext("stock_code", "").strip()}
            else:
                payload = response.json()
                status = payload.get("status")
                if status == "013":
                    payload = {"status": "013", "list": [], "total_page": 0}
                elif status != "000":
                    safe_status = status if isinstance(status, str) and re.fullmatch(r"\d{3}", status) else "INVALID"
                    raise DartError("DART_STATUS_" + safe_status)
            observed = self._now()
            with self._db() as db:
                db.execute("INSERT OR REPLACE INTO dart_cache VALUES(?,?,?,?,?)",
                    (self.account, request, observed.isoformat(), observed.timestamp() + (ttl or self.ttl_seconds),
                     json.dumps(payload, ensure_ascii=False, allow_nan=False)))
            return payload, observed.isoformat()
        except Exception as exc:
            # Never include request URLs, provider messages or credentials in errors.
            code = str(exc) if isinstance(exc, DartError) else "DART_TRANSPORT_OR_FORMAT_ERROR"
            seconds = 86400 if code.endswith(("010", "011", "012", "020", "901")) else 60
            with self._db() as db:
                db.execute("INSERT INTO dart_control VALUES(?,?) ON CONFLICT(account) DO UPDATE SET blocked=MAX(blocked,excluded.blocked)",
                           (self.account, self._now().timestamp() + seconds))
            raise DartError(code) from None

    def collect(self, symbol):
        result = {"provider": "dart", "symbol": symbol, "status": "unavailable",
                  "as_of": self._now().isoformat(), "filings": [], "financials": None,
                  "limitations": ["현재 수집 시점의 공시이며 과거 시점 재현용 자료가 아님",
                                  "공시 제목만으로 호재·악재를 확정하지 않음"]}
        try:
            if not re.fullmatch(r"\d{6}", symbol):
                raise DartError("DART_INVALID_SYMBOL")
            mapping, _ = self._request("corpCode.xml", {}, ttl=86400)
            corp = mapping.get(symbol)
            if not corp or not re.fullmatch(r"\d{8}", corp):
                raise DartError("DART_CORP_NOT_FOUND")
            now = self._now()
            # DART search dates use the Korean calendar date.
            today = now.astimezone(timezone(timedelta(hours=9))).date()
            filings, observations = [], []
            for page in range(1, 11):
                payload, observed = self._request("list.json", {"corp_code": corp,
                    "bgn_de": (today-timedelta(days=550)).strftime("%Y%m%d"), "end_de": today.strftime("%Y%m%d"),
                    "pblntf_ty": "A", "page_no": page, "page_count": 100,
                    "sort": "date", "sort_mth": "desc", "last_reprt_at": "N"})
                observations.append(observed)
                filings.extend(payload.get("list", []))
                if page >= int(payload.get("total_page", 0)):
                    break
            else:
                raise DartError("DART_FILINGS_TRUNCATED")
            filings = [f for f in filings if f.get("corp_code") == corp and
                       re.fullmatch(r"\d{14}", f.get("rcept_no", "")) and
                       re.fullmatch(r"\d{8}", f.get("rcept_dt", "")) and f["rcept_dt"] <= today.strftime("%Y%m%d")]
            filings.sort(key=lambda f: (f["rcept_dt"], f["rcept_no"]), reverse=True)
            result.update(status="ok", corp_code=corp, observed_at=max(observations),
                filings=[{"title": f["report_nm"], "receipt": f["rcept_no"], "date": f["rcept_dt"],
                          "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + f["rcept_no"]} for f in filings[:30]])
            # Pick most recent accounting period, then most recent correction for that period.
            candidates = []
            for filing in filings:
                name = filing.get("report_nm", "")
                match = re.search(r"(사업|반기|분기)보고서\s*\((\d{4})\.(\d{2})\)", name)
                if not match or "첨부" in name:
                    continue
                kind, year, month = match.groups()
                code = {("사업", "12"): "11011", ("반기", "06"): "11012",
                        ("분기", "03"): "11013", ("분기", "09"): "11014"}.get((kind, month))
                if code and datetime(int(year), int(month), monthrange(int(year), int(month))[1]).date() <= today:
                    candidates.append((int(year), int(month), filing["rcept_no"], code, filing))
            if not candidates:
                result["financial_status"] = "no_supported_calendar_year_report"
                return result
            year, month, receipt, code, filing = max(candidates, key=lambda c: c[:3])
            rows, observed = self._request("fnlttSinglAcntAll.json", {"corp_code": corp,
                "bsns_year": str(year), "reprt_code": code, "fs_div": "CFS"})
            # Latest endpoint may return a newer correction than the cached filing list.
            # Do not silently match it to another filing or another accounting period.
            accounts = rows.get("list", [])
            if not accounts or any(r.get("rcept_no") != receipt or r.get("corp_code") != corp or
                    r.get("bsns_year") != str(year) or r.get("reprt_code") != code for r in accounts):
                result["financial_status"] = "receipt_or_period_mismatch_or_no_data"
                return result
            from .dart_analysis import financial_metrics
            result["financials"] = financial_metrics(accounts, year, month, receipt)
            result["financials"].update(observed_at=observed, filing_date=filing["rcept_dt"],
                url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + receipt)
            result["financial_status"] = result["financials"]["status"]
        except DartError as exc:
            result.update(status="unavailable", reason=str(exc), financials=None)
        except (KeyError, ValueError, TypeError, AttributeError):
            result.update(status="unavailable", reason="DART_RESPONSE_FORMAT_ERROR", financials=None)
        return result


def build_dart_service(settings):
    if not getattr(settings, "dart_enabled", False):
        return None
    return DartClient(settings.dart_api_key, settings.dart_db_path,
                      ttl_seconds=settings.dart_cache_seconds, daily_budget=settings.dart_daily_budget)
