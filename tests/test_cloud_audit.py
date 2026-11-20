import json
import sqlite3
from app.cloud_audit import summarize


def test_audit_reads_latest_actual_reports_without_order_or_credentials():
    db=sqlite3.connect(":memory:")
    db.execute("CREATE TABLE policy_decisions(id INTEGER PRIMARY KEY,report TEXT)")
    def add(row):db.execute("INSERT INTO policy_decisions(report) VALUES(?)",(json.dumps(row),))
    add({"symbol":"000660","action":"BUY"})  # Older engine; do not mislabel as grounded.
    row={"symbol":"005930","action":"HOLD","quantity":0,"execution":"NO_ORDER",
         "market_research":{"status":"ok","observed_at":"2026-11-20T03:00:00Z","sources":[{}]},
         "gemini_review":{"status":"ok","verdict":"HOLD","reason":"risk","sources":[{"domain":"samsung.com"}]},
         "dart":{"financial_status":"ok","financials":{"observed_at":"2026-11-20T00:00:00Z"},"intraday":{"status":"ok","filings":[]}},
         "private_key":"must never be included"}
    add({**row,"action":"BUY"});add(row)
    result=summarize(db,{"started_at":"2026-11-20T03:00:00Z"})
    assert result["latest_grounded_symbols"] == 1
    assert result["rows"][0]["action"] == "HOLD"
    assert result["rows"][0]["gemini_verdict"] == "HOLD"
    assert result["rows"][0]["financial_status"] == "ok"
    assert "must never be included" not in json.dumps(result)
    assert db.execute("SELECT COUNT(*) FROM policy_decisions").fetchone()[0] == 3
    db.close()
