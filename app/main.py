import argparse, logging, time
from .config import get_settings
from .api_client import TradingAPI
from .llm_analyst import LLMAnalyst
from .storage import Store
from .engine import TradingEngine

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--scan-only", action="store_true")
    args = parser.parse_args()
    s = get_settings()
    logging.basicConfig(level=getattr(logging, s.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not s.team_code or s.team_code == "PUT_YOUR_TEAM_CODE_HERE":
        raise SystemExit(".env에 본인 TEAM_CODE를 설정하세요.")
    if not s.dry_run and not s.enable_trading:
        raise SystemExit("주문 활성화에는 DRY_RUN=false와 ENABLE_TRADING=true가 모두 필요합니다.")
    api = TradingAPI(s.api_base_url, s.team_code)
    print("로그인:", api.login())
    print("팀 확인:", api.me())
    engine = TradingEngine(s, api, LLMAnalyst(s.openai_api_key, s.openai_model), Store(s.db_path))
    while True:
        engine.run_cycle(scan_only=args.scan_only)
        if args.once: break
        time.sleep(max(60, s.loop_seconds))

if __name__ == "__main__":
    main()
