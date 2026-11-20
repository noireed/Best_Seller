# 개발 담당: 최준혁
"""One paginated market-wide DART scan per cycle, including non-periodic filings."""
import re
from datetime import timedelta, timezone

from .dart import DartError


class IntradayDisclosures:
    def __init__(self, baseline, client):
        self.baseline, self.client = baseline, client
        self.scan = None
        self.mapping = {}

    def _scan(self):
        now = self.client._now()
        today = now.astimezone(timezone(timedelta(hours=9))).strftime("%Y%m%d")
        self.mapping, _ = self.client._request("corpCode.xml", {}, ttl=86400)
        rows, observations = [], []
        for page in range(1, 21):
            payload, observed = self.client._request("list.json", {
                "bgn_de": today, "end_de": today, "page_no": page, "page_count": 100,
                "sort": "date", "sort_mth": "desc", "last_reprt_at": "N"}, ttl=300)
            observations.append(observed)
            rows.extend(payload.get("list", []))
            if page >= int(payload.get("total_page", 0)):
                return {"status": "ok", "observed_at": max(observations), "date": today, "rows": rows}
        raise DartError("DART_INTRADAY_FILINGS_TRUNCATED")

    def collect(self, symbol):
        try:
            baseline = self.baseline.collect(symbol) if self.baseline else {"symbol": symbol, "status": "unavailable"}
        except Exception as exc:
            baseline = {"symbol": symbol, "status": "unavailable", "reason": "baseline_" + type(exc).__name__}
        if self.scan is None:
            try:
                self.scan = self._scan()
            except DartError as exc:
                self.scan = {"status": "unavailable", "reason": str(exc)}
        intraday = {k: v for k, v in self.scan.items() if k != "rows"}
        corp = self.mapping.get(symbol)
        intraday["filings"] = [
            {"title": row.get("report_nm", ""), "receipt": row["rcept_no"], "date": row["rcept_dt"],
             "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + row["rcept_no"]}
            for row in self.scan.get("rows", [])
            if corp and row.get("corp_code") == corp and row.get("rcept_dt") == self.scan["date"]
            and re.fullmatch(r"\d{14}", row.get("rcept_no", ""))]
        return {**baseline, "intraday": intraday}
