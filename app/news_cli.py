# 개발 담당: 최준혁
"""Standalone news analysis. This entry point never instantiates TradingAPI."""
import argparse
import json
from pathlib import Path

from .config import get_settings
from .news import NewsService, build_news_service, load_aliases, parse_time
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description="종목 뉴스 분석 (주문 기능 없음)")
    parser.add_argument("--symbol", default="005930")
    parser.add_argument("--fixture", type=Path, help="네이버 items 형태의 로컬 JSON")
    parser.add_argument("--as-of", help="fixture 전용 기준 시각 (시간대 포함)")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--db", help="선택: 분석 이력을 저장할 SQLite 경로")
    parser.add_argument("--provider", choices=("naver", "gnews", "both"), help="조회 공급원 선택")
    parser.add_argument("--api-status", action="store_true", help="네트워크 없이 로컬 API 예산/캐시 확인")
    args = parser.parse_args()
    if args.as_of and not args.fixture:
        parser.error("--as-of는 --fixture와 함께 사용하세요.")
    settings = get_settings()
    if args.api_status:
        service = build_news_service(settings)
        print(json.dumps([s.client.health() for s in service.services if hasattr(s.client, "health")], indent=2))
        return 0
    if args.fixture:
        data = json.loads(args.fixture.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            parser.error("fixture에는 items 배열이 필요합니다.")
        class FixtureClient:
            provider = "fixture"
            def search(self, query, display):
                return data["items"][:display]
        clock = (lambda: parse_time(args.as_of)) if args.as_of else None
        service = NewsService(FixtureClient(), load_aliases(settings.news_symbol_aliases),
                              max_age_hours=settings.news_max_age_hours,
                              display=settings.news_display, clock=clock)
    else:
        if args.provider:
            settings = settings.model_copy(update={"news_providers": "naver,gnews" if args.provider == "both" else args.provider})
        service = build_news_service(settings)
    report = service.collect(args.symbol)
    report["source_mode"] = "fixture" if args.fixture else settings.news_providers
    if args.db:
        store = Store(args.db)
        try:
            store.news(report)
        finally:
            store.db.close()
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 2 if report["status"] == "unavailable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
