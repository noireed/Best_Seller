# Best_Seller — AQuA Hybrid Trading Agent

모의투자 API를 사용하는 LLM + 정량분석 하이브리드 자동매매 프로젝트입니다.

**안전 기본값:** `DRY_RUN=true`, `ENABLE_TRADING=false`입니다. 주문 API를 호출하지 않는 상태로 시작합니다. 이 프로젝트는 제공된 모의투자 API 전용이며 실계좌 주문 기능은 없습니다.

## 설치
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```
`.env`에 본인 팀 코드를 입력하세요. `OPENAI_API_KEY`는 선택입니다.

## 실행
```bash
python -m app.main --once --scan-only
python -m app.main --once
python -m app.main
```

## 구성
- API 인증, 시세/캔들/호가/포트폴리오 조회
- EMA20/EMA50, RSI14, 20봉 모멘텀 정량 분석
- 선택적 OpenAI LLM 보조 판단 (키가 없으면 LLM 점수 중립)
- 정량 70% + LLM 30% 결합
- 종목 비중, 주문 금액, 현금 보유비율, 장 운영시간, 종목 쿨다운 등 사전 위험검사
- SQLite 의사결정/주문 로그

## 설정
- `API_BASE_URL=http://136.119.248.78`
- `DRY_RUN=true` 주문 API 호출 없음
- `ENABLE_TRADING=false` 주문 비활성화
- `MAX_POSITION_PCT=0.15` 종목별 최대 비중
- `MAX_ORDER_VALUE_KRW=2000000` 단일 주문 최대 금액
- `CASH_RESERVE_PCT=0.10` 현금 최소 보유 비율
- `WATCHLIST=005930,000660,035420`

## 주의 및 한계
- API 가이드상 거래시간 09:00~15:00, 시간당 30회, 동일 종목 주문 후 30분 쿨다운, 분당 호출 100회입니다. 클라이언트는 분당 60회로 보수 제한합니다.
- 왕복 수수료/세금은 약 0.23%이므로 잦은 매매를 피하도록 전략 임계값을 둡니다.
- API 가이드에는 뉴스 검색 API가 없습니다. 현재 LLM은 뉴스가 아니라 지표/시세/포트폴리오만 해석합니다.
- 일일 손실 제한은 데이터 시계열이 충분하지 않아 아직 강제 적용하지 않습니다.
- 모의투자 환경에서 DRY_RUN으로 충분히 검증한 뒤에만 `DRY_RUN=false`, `ENABLE_TRADING=true`로 변경하세요.
