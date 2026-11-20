# 개발 담당: 최준혁
"""Read the running trader's encrypted checkpoint without starting a trading cycle."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from .cloud_cycle import Checkpoint


def summarize(database, metadata):
    reports = [json.loads(row[0]) for row in database.execute(
        "SELECT report FROM policy_decisions ORDER BY id DESC LIMIT 120")]
    # Only the new grounded-research engine writes market_research.
    rows = []
    seen = set()
    for report in reports:
        if "market_research" not in report or report.get("symbol") in seen:
            continue
        seen.add(report["symbol"])
        opinion = report.get("gemini_review", {})
        dart = report.get("dart", {})
        research = report.get("market_research", {})
        financials = dart.get("financials") or {}
        rows.append({"symbol": report["symbol"], "action": report["action"],
            "quantity": report["quantity"], "execution": report["execution"],
            "technical_reason": report.get("technical_reason"),
            "baseline_action": report.get("decision", {}).get("baseline_action"),
            "decision_reason": report.get("reason"),
            "research_observed_at": research.get("observed_at"),
            "search_status": research.get("status"), "search_http_status": research.get("http_status"),
            "search_source_count": len(research.get("sources", [])),
            "gemini_status": opinion.get("status"), "gemini_http_status": opinion.get("http_status"),
            "gemini_verdict": opinion.get("verdict"), "gemini_reason": opinion.get("reason"),
            "gemini_counter_case": opinion.get("counter_case"),
            "gemini_observed_at": opinion.get("observed_at"),
            "cited_domains": [source.get("domain") for source in opinion.get("sources", [])],
            "financial_status": dart.get("financial_status"),
            "financial_observed_at": financials.get("observed_at"),
            "financial_filing_date": financials.get("filing_date"),
            "intraday_status": dart.get("intraday", {}).get("status"),
            "intraday_observed_at": dart.get("intraday", {}).get("observed_at"),
            "intraday_filings": dart.get("intraday", {}).get("filings", []),
            "historical_cases": len(report.get("historical_event_outcomes", {}).get("cases", []))})
    return {"cycle_metadata": metadata, "latest_grounded_symbols": len(rows), "rows": rows}


def main():
    with TemporaryDirectory() as temp:
        directory = Path(temp)
        checkpoint = Checkpoint()
        status = checkpoint.restore(directory, allow_recovery=True)
        metadata_path = directory / "cycle.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        database_path = directory / "trading.db"
        database = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            report = summarize(database, metadata)
        finally:
            database.close()
        print(json.dumps({"checkpoint_status": status, "checkpoint_sha": checkpoint.sha,
                          **report}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
