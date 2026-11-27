# 프로젝트 인계 메모 (2026-11-27)

- 저장소: https://github.com/noireed/Best_Seller, 기본 브랜치 main. 학교 자연어처리 모의투자 프로젝트이며 별도의 개인 투자 AI 프로젝트 코드는 사용하지 않습니다.
- 실행 코드: app/. 원래 팀 구조 aqua_hybrid_agent/도 보존합니다. 자동 실행은 GitHub Actions이며 국내 40종목을 평일 한국 시간 09:00~15:00에 15분 간격으로 확인하고 야간·주말에도 후속 실행을 이어갑니다.
- 모델: GEMINI_BACKEND=developer. 무료 공개 뉴스 RSS와 DART 자료를 일반 Gemini API에 제공하며, 소진된 교육용 크레딧을 사용하는 유료 Vertex 검색 경로로 자동 전환하지 않습니다. 기본 하루 100회·회차 8개·요청 간격 12초, 실제 공급자 무료 한도는 별도입니다. Gemini 확인이 없으면 매수를 보류하고 가격 기준 매도는 모델 오류로 취소하지 않습니다.
- 11월 25일 유한양행 3주 요청의 통신 실패로 전체 거래가 멈췄던 문제를 수정했습니다. 전체 주문 이력과 보유·가용 수량을 시간차로 두 번 대조해 NO_SERVER_ORDER_OBSERVED로 보존했고 원주문은 재전송하지 않았습니다. 11월 27일 10:53에 미확정 0건 및 40종목 처리 재개를 확인했습니다.
- 현재 로컬 API 설정은 저장소 루트 .env, 기존 로컬 거래 이력은 data/trading.db에 있습니다. 이전 outputs/Best_Seller 복사본에서 내용 일치를 확인해 옮겼습니다. .env와 data/는 Git 추적 대상이 아닙니다. 키를 출력하거나 커밋하지 마세요.
- GitHub 비밀 설정: AQUA_TEAM_CODE, AQUA_GEMINI_API_KEY, AQUA_DART_API_KEY, AQUA_GNEWS_API_KEY, AQUA_STATE_KEY. 값은 소스에 없습니다. GitHub 인증은 연결 도구 또는 기존 Git 자격 증명을 사용합니다.
- 실제 클라우드 상태는 aqua-team-5-state의 simulation-state.enc와 research-state.enc입니다. 로컬 data/trading.db는 과거 로컬 이력이며 현재 서버 상태의 사본이 아닙니다. 상태 브랜치는 최신 두 파일만 보존하고 cloud_compaction으로 누적 이력을 정리합니다. 파일 해시는 그대로이며 force-with-lease로 동시 쓰기를 보호합니다. main의 개발·기여 이력은 정리하지 않습니다.
- 조회 진단: AQuA trading audit 워크플로 / app.cloud_audit. 정상 기동 표시만 보지 말고 checkpoint_status, cycle_metadata의 최근 분석·완료 시각, 미확정 주문, Gemini·자료 상태를 확인합니다.
- 가격 검증 원자료는 data/archives/strategy-candles-20261120.json.gz로 압축 보존했습니다. 비교 결과와 해시는 examples/strategy_comparison_20261120.json, 실제 공시 뒤 주가 관측 사례는 examples/filing_outcomes.json입니다. 과거 뉴스·Gemini 의견을 포함한 수익 검증으로 해석하지 않습니다.
- 설정과 정책은 README.md 및 docs/GROUNDED_DECISIONS.md, 의존성은 requirements.txt를 참고합니다. 테스트는 python -m pytest -q입니다. 캐시는 필요할 때 재생성됩니다.
- 사용자는 화면 조작을 금지했습니다. 터미널·API로 작업하고, 필요한 계정 확인은 정확한 링크와 항목을 제시합니다. 긴 모니터링은 사용량을 낭비하므로 요청된 검증 범위에서 종료합니다.
