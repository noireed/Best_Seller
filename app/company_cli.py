# 개발 담당: 최준혁
"""Read-only company research: collect disclosures, financials and chronological news evidence."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .config import get_settings
from .dart import build_dart_service
from .dart_analysis import validate_news
from .economic_news import economic_indicators
from .event_history import EventHistory, apply_history
from .fundamental_analysis import apply_fundamentals
from .news import build_news_service, load_aliases, parse_time
from .storage import Store


def research(settings, symbol):
    aliases = []
    for raw in (settings.news_symbol_aliases, settings.gnews_symbol_aliases):
        aliases.extend(load_aliases(raw).get(symbol, []))
    if not aliases:
        raise ValueError("종목 별칭 설정이 필요합니다.")
    news = build_news_service(settings).collect(symbol) if settings.news_enabled else None
    service = build_dart_service(settings)
    dart = service.collect(symbol) if service else {"status": "disabled"}
    now = datetime.now(timezone.utc)
    features = economic_indicators(news, symbol, aliases, now=now,
        half_life_hours=settings.news_half_life_hours, max_age_hours=settings.news_max_age_hours,
        allow_delayed=settings.news_allow_delayed_in_strategy, min_sources=settings.news_min_sources)
    store = Store(settings.db_path)
    try:
        snapshot = None
        if news and settings.event_history_enabled:
            snapshot = store.record_event_history(news, aliases, now=now, max_age_hours=settings.news_max_age_hours)
        features = validate_news(features, dart, now=now, min_sources=settings.news_min_sources)
        features = apply_history(features, snapshot, now=now, half_life_hours=settings.news_half_life_hours,
            max_age_hours=settings.news_max_age_hours, min_sources=settings.news_min_sources)
        features = apply_fundamentals(features, dart, now=now, enabled=settings.fundamental_guard_enabled,
                                     min_sources=settings.news_min_sources)
        return {"kind": "COMPANY_ECONOMIC_RESEARCH_NOT_ORDER", "symbol": symbol, "as_of": now.isoformat(),
                "dart": dart, "economic_news": features, "event_history": snapshot,
                "orders": 0, "llm_calls": 0}
    finally:
        store.db.close()


def research_fixture(dataset):
    """Time-ordered synthetic probes; never connect to external APIs or submit orders."""
    db = sqlite3.connect(":memory:")
    history = EventHistory(db)
    rows = []
    try:
        previous = None
        for sample in dataset["snapshots"]:
            now = parse_time(sample["as_of"])
            if previous and now < previous:
                raise ValueError("fixture snapshots must be chronological")
            previous = now
            history.ingest(sample["news"], dataset["aliases"], now=now)
            snapshot = history.snapshot(dataset["symbol"], now=now)
            features = economic_indicators(sample["news"], dataset["symbol"], dataset["aliases"], now=now)
            features = apply_history(features, snapshot, now=now)
            rows.append({"as_of": sample["as_of"], "event_history": snapshot, "economic_news": features})
        return {"kind": "SYNTHETIC_COMPANY_EVENT_HISTORY_NOT_INVESTMENT_PERFORMANCE", "symbol": dataset["symbol"], "snapshots": rows,
                "network_calls": 0, "orders": 0, "llm_calls": 0}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="기업별 공시 원문·재무제표·경제사건 이력 수집 (주문 없음)")
    parser.add_argument("--symbol", default="005930")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = (research_fixture(json.loads(args.fixture.read_text(encoding="utf-8"))) if args.fixture else
              research(get_settings(), args.symbol))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"kind": result["kind"], "symbol": result.get("symbol"), "orders": 0}))


if __name__ == "__main__":
    main()
