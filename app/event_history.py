# 개발 담당: 최준혁
"""Observed-time financial event history; ambiguous stories are never auto-linked."""
from copy import deepcopy
from collections import defaultdict
import hashlib
import json
import re
from datetime import timezone

from .dart_analysis import _claim_period
from .economic_news import economic_indicators, rescore
from .news import parse_time

CORRECTION = re.compile(r"정정|수정|\bcorrect(?:ed|ion)\b|\brevis(?:ed|ion)\b", re.I)


def scope_key(symbol, event, title, url):
    change = event.get("reported_change")
    period = _claim_period(title)
    if (change and period and re.search(r"연결|\bconsolidated\b", title, re.I)
            and not re.search(r"별도|\bstandalone\b", title, re.I)
            and change["comparison_basis"] in ("year_on_year", "quarter_on_quarter")):
        scope = [symbol, change["metric"], *period, change["comparison_basis"],
                 bool(re.search(r"누적|\bYTD\b|year.to.date", title, re.I))]
        return "financial:" + json.dumps(scope, ensure_ascii=False, separators=(",", ":"))
    # No fuzzy title grouping: a new contract or a different fire is not the same event.
    return "unlinked:" + hashlib.sha256((symbol+url+event["event"]).encode()).hexdigest()


def _signed(event):
    change = event.get("reported_change")
    return (change["value"] * (1 if change["direction"] == "increase" else -1)) if change else None


class EventHistory:
    def __init__(self, db):
        self.db = db
        db.execute("""CREATE TABLE IF NOT EXISTS economic_event_history(
            identity TEXT PRIMARY KEY, symbol TEXT, scope TEXT, published TEXT, observed TEXT, payload TEXT)""")
        db.execute("CREATE INDEX IF NOT EXISTS event_history_lookup ON economic_event_history(symbol,scope,observed)")
        db.commit()

    def ingest(self, report, aliases, *, now, max_age_hours=48):
        if not report or report.get("status") != "ok":
            return
        symbol = report["symbol"]
        with self.db:
            for article in report.get("articles", []):
                single = economic_indicators({"status": "ok", "symbol": symbol, "articles": [article]},
                                             symbol, aliases, now=now, max_age_hours=max_age_hours)
                for row in single["evidence"]:
                    for event in row["events"]:
                        scope = scope_key(symbol, event, row["title"], row["url"])
                        payload = {**{key: row[key] for key in ("url", "title", "source_host", "published_at", "observed_at")},
                                   "event": event, "correction": bool(CORRECTION.search(row["title"]))}
                        identity = hashlib.sha256(json.dumps([symbol, row["url"], row["title"], row["published_at"], event],
                                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                        self.db.execute("INSERT OR IGNORE INTO economic_event_history VALUES(?,?,?,?,?,?)",
                            (identity, symbol, scope, row["published_at"], row["observed_at"], json.dumps(payload, ensure_ascii=False, allow_nan=False)))

    def snapshot(self, symbol, *, now):
        now = now.astimezone(timezone.utc)
        rows = self.db.execute("SELECT scope,payload FROM economic_event_history WHERE symbol=? AND observed<=? AND published<=? ORDER BY published,observed LIMIT 10001",
                               (symbol, now.isoformat(), now.isoformat())).fetchall()
        if len(rows) > 10000:
            return {"status": "unavailable", "reason": "history_snapshot_limit", "groups": []}
        groups = defaultdict(list)
        for scope, raw in rows:
            row = json.loads(raw)
            # Parse too: ISO text comparisons alone do not prove availability for other offsets.
            if parse_time(row["observed_at"]) <= now and parse_time(row["published_at"]) <= now:
                groups[scope].append(row)
        result = []
        for scope, observations in groups.items():
            observations.sort(key=lambda r: (parse_time(r["published_at"]), parse_time(r["observed_at"]), r["url"]))
            suppressed = {}
            if scope.startswith("financial:"):
                first_reported, latest_markers, latest_corrections = {}, {}, {}
                for item in observations:
                    value, at = _signed(item["event"]), parse_time(item["published_at"])
                    if item["event"]["assertion"] == "reported":
                        first_reported[value] = min(at, first_reported.get(value, at))
                    if item["event"]["assertion"] in ("denied", "withdrawn"):
                        latest_markers[value] = max(at, latest_markers.get(value, at))
                    if item["correction"]:
                        host = item["source_host"]
                        latest_corrections[host] = max(at, latest_corrections.get(host, at))
                for row in observations:
                    value = _signed(row["event"])
                    at = parse_time(row["published_at"])
                    marker = latest_markers.get(value)
                    correction = latest_corrections.get(row["source_host"])
                    if marker and marker >= first_reported.get(value, at):
                        suppressed[row["url"]] = "later_denial_or_withdrawal"
                    elif correction and correction > at:
                        suppressed[row["url"]] = "same_publisher_correction"
            active = [r for r in observations if r["url"] not in suppressed and r["event"]["assertion"] == "reported"]
            values = [_signed(r["event"]) for r in active if _signed(r["event"]) is not None]
            conflicting = bool(values and max(values)-min(values) > 1.0)
            result.append({"scope": scope, "linkage": "explicit_financial_period" if scope.startswith("financial:") else "same_article_only",
                "state": "conflicting" if conflicting else "revised" if suppressed else "observed",
                "first_published_at": observations[0]["published_at"], "suppressed_urls": suppressed,
                "observations": observations})
        return {"status": "ok", "symbol": symbol, "as_of": now.isoformat(), "groups": result,
                "limitations": "Period/metric/basis matching only; no proof of common causal event or independent sourcing."}


def apply_history(features, snapshot, *, now, half_life_hours=12, max_age_hours=48, min_sources=2):
    result = deepcopy(features)
    result["event_history"] = {"status": "unavailable", "blocked_urls": [], "recycled_urls": []}
    if not snapshot or snapshot.get("status") != "ok" or snapshot.get("symbol") != result["symbol"]:
        return result
    if parse_time(snapshot["as_of"]) > now:
        return result
    groups = {g["scope"]: g for g in snapshot["groups"]}
    metadata = result["event_history"]
    metadata.update(status="applied", groups=[{k: g[k] for k in ("scope", "linkage", "state")} for g in snapshot["groups"]])
    for row in result["evidence"]:
        for event in row["events"]:
            group = groups.get(scope_key(result["symbol"], event, row["title"], row["url"]))
            if not group:
                continue
            if row["url"] in group["suppressed_urls"] or group["state"] == "conflicting":
                row["impact"] = 0.0
                row["history_excluded"] = group["suppressed_urls"].get(row["url"], "conflicting_claims")
                if row["url"] not in metadata["blocked_urls"]:
                    metadata["blocked_urls"].append(row["url"])
            matching = [r for r in group["observations"] if _signed(r["event"]) == _signed(event)
                        and r["event"]["assertion"] == event["assertion"]]
            if matching:
                first = min(parse_time(r["published_at"]) for r in matching)
                age = (now-first).total_seconds()/3600
                if first < parse_time(row["published_at"]):
                    row["weight"] = min(row["weight"], 2**(-age/half_life_hours)/(1+row["delay_hours"]/12))
                    row["event_first_published_at"] = first.isoformat()
                    if age > max_age_hours:
                        row["impact"] = 0.0
                        if row["url"] not in metadata["blocked_urls"]:
                            metadata["blocked_urls"].append(row["url"])
                    if row["url"] not in metadata["recycled_urls"]:
                        metadata["recycled_urls"].append(row["url"])
    return rescore(result, min_sources)
