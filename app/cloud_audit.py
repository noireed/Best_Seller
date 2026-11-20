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
            "price": report.get("price"), "position_sizing": report.get("position_sizing"),
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
    diagnostics = []
    try:
        cached = database.execute("SELECT at,report FROM gemini_cache ORDER BY at DESC LIMIT 160").fetchall()
    except sqlite3.OperationalError:
        cached = []
    target = next((r for r in reports if r.get("symbol") == "068270" and r.get("market_research")), None)
    for at, encoded in cached:
        raw = json.loads(encoded)
        text = raw.get("text", "")
        if '"068270"' not in text:
            continue
        try:
            answer = json.loads(text)
        except ValueError:
            diagnostics.append({"at": at, "parse_error": True, "finish_reason": raw.get("finish_reason"), "raw_text": text[:8000]})
            continue
        if answer.get("symbol") != "068270" or "verdict" not in answer:
            continue
        research = (target or {}).get("market_research", {})
        index = answer.get("claim_id")
        claims = research.get("claims", [])
        claim = claims[index] if type(index) is int and 0 <= index < len(claims) else {}
        quote = answer.get("evidence_quote", "")
        diagnostics.append({"at": at, "answer": answer, "finish_reason": raw.get("finish_reason"),
            "research_observed_at": research.get("observed_at"), "claim_count": len(claims),
            "selected_claim": claim, "quote_in_summary": isinstance(quote,str) and quote in research.get("text", ""),
            "quote_in_claim": isinstance(quote,str) and quote in claim.get("text", "")})
    return {"cycle_metadata": metadata, "latest_grounded_symbols": len(rows), "rows": rows,
            "celltrion_response_diagnostics": diagnostics}


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
