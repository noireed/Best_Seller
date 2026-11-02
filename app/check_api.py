"""Read-only connection inspection. Never calls order creation/cancellation."""
import argparse
import json
from pathlib import Path

from .api_client import APIError, TradingAPI
from .config import get_settings


def inspect_api(api, symbols, *, authenticated=False):
    report = {"mode": "read_only", "checks": []}

    def check(name, operation, summarize):
        try:
            data = operation()
            report["checks"].append({"name": name, "status": "ok", "summary": summarize(data)})
        except (APIError, ValueError, KeyError, TypeError) as exc:
            report["checks"].append({"name": name, "status": "error",
                                      "code": getattr(exc, "code", None),
                                      "http_status": getattr(exc, "status", None),
                                      "retry_after": getattr(exc, "retry_after", None),
                                      "error_type": type(exc).__name__})

    check("leaderboard", api.leaderboard,
          lambda d: {"updated_at": d["updatedAt"], "team_count": len(d["rankings"])})
    if not authenticated:
        report["authenticated_checks"] = "skipped"
        return report
    # No explicit login response is ever serialized into the report.
    check("team", api.me, lambda d: {"team_id": d["teamId"], "team_name": d["teamName"]})
    if report["checks"][-1]["status"] != "ok":
        report["authenticated_checks"] = "stopped_after_auth_error"
        return report
    check("stocks", api.stocks, lambda d: {"count": len(d["stocks"])})
    check("prices", lambda: api.prices(symbols), lambda d: {"count": len(d["result"])})
    check("portfolio", api.portfolio,
          lambda d: {"holding_count": len(d["holdings"]),
                     "available_cash_present": "availableCash" in d,
                     "total_asset_value_present": "totalAssetValue" in d})
    check("limits", api.limits,
          lambda d: {"remaining_trades": d["remainingTrades"], "cooldown_count": len(d["cooldowns"])})
    check("orders", api.orders,
          lambda d: {"count": len(d["orders"]), "total_pages": d["totalPages"]})
    for symbol in symbols:
        check(f"orderbook:{symbol}", lambda s=symbol: api.orderbook(s),
              lambda d: {"asks": len(d["asks"]), "bids": len(d["bids"])})
        check(f"candles:{symbol}", lambda s=symbol: api.candles(s, count=5),
              lambda d: {"count": len(d["candles"]), "next_page": bool(d.get("nextBefore"))})
    report["authenticated_checks"] = "attempted"
    return report


def main():
    parser = argparse.ArgumentParser(description="AQuA 읽기 전용 연결 점검")
    parser.add_argument("--authenticated", action="store_true", help="설정된 TEAM_CODE로 팀 조회도 점검")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = get_settings()
    if args.authenticated and not settings.team_code:
        parser.error("인증 점검에는 .env의 TEAM_CODE가 필요합니다.")
    report = inspect_api(TradingAPI(settings.api_base_url, settings.team_code), settings.symbols,
                         authenticated=args.authenticated)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if any(c["status"] == "error" for c in report["checks"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
