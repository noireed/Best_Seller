# Best_Seller — AQuA Hybrid Trading Agent

모의투자 API를 사용하는 LLM + 정량분석 하이브리드 자동매매 프로젝트입니다.
종목별 뉴스를 수집하고 경제사건을 분석해 매매 판단의 보조지표로 사용합니다.

## 모의투자 전략

기본 감시 대상은 국내 주식 40종목입니다. 일봉으로 추세와 모멘텀을 확인하고, 조건이 맞을 때 가용 현금이나 보유 수량에 맞춰 주문 수량을 계산합니다.

| 판단 | 가격 지표 기준 |
| --- | --- |
| 매수 후보 | 종가 > EMA20 > EMA50, 최근 20거래일 수익률이 양수, RSI14가 50~70 |
| 매도 후보 | 종가 < EMA20, 최근 20거래일 수익률이 음수, RSI14가 50 미만 |
| 보류 | 가격 조건이 맞지 않거나 데이터가 부족한 경우 |

매수 목표액은 현금 유보분을 뺀 총자산을 감시종목 수로 나눠 계산하며, 종목 비중 한도도 적용합니다. 목표액에서 기존 보유액을 뺀 뒤 가용 현금과 주문 한도에 맞춰 수량을 계산합니다. 매도 수량에는 매도 가능한 보유 수량과 주문 한도를 적용합니다. 지표 설명은 [Fidelity EMA](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/ema)와 [Charles Schwab 기술 지표](https://www.schwab.com/learn/story/choosing-technical-indicators-to-analyze-stocks)를 참고했습니다.

2026년 11월 13일 5팀 계정에서 자동으로 제출한 셀트리온 9주 매수 주문의 전량 체결을 확인했습니다. 이 주문은 감시종목 20개 설정에서 실행했으며, 확장한 40종목은 조회 전용으로 분석을 확인했습니다.

**안전 기본값:** `DRY_RUN=true`, `ENABLE_TRADING=false`입니다. 주문 API를 호출하지 않는 상태로 시작합니다. 이 프로젝트는 제공된 모의투자 API 전용이며 실계좌 주문 기능은 없습니다.

## 설치
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```
`.env`에 본인 팀 코드를 입력하세요. 뉴스 조회에는 `GNEWS_API_KEY`가 필요하며 `OPENAI_API_KEY`는 선택입니다.
실제 키가 들어 있는 `.env`와 실행 중 생성되는 `data/`는 커밋하지 않습니다.

## 실행

GitHub Actions는 기본 브랜치에서 평일 한국 시간 09:07부터 14:52까지 15분마다 5팀 모의투자 사이클을 실행합니다. 지연되어 장이 끝난 작업은 건너뜁니다. GitHub의 Actions 화면에서 `AQuA simulation`을 비활성화하면 예약 실행을 멈출 수 있습니다.

예약 실행에는 저장소의 `AQUA_TEAM_CODE`, `AQUA_GNEWS_API_KEY`, `AQUA_DART_API_KEY`, `AQUA_STATE_KEY` 비밀 설정을 사용합니다. 전용 상태 브랜치에는 암호화한 주문·분석 기록을 저장합니다. 주문 결과가 불확실하거나 실행이 중단되면 다음 예약 주문을 보류하므로 서버 주문 내역과 저장 상태를 대조해야 합니다.

40종목의 뉴스·공시는 평일 08:47와 12:03(한국 시간)에 별도 작업으로 수집합니다. 주문 작업은 저장된 자료를 읽고 종목별 뉴스 보정 및 뉴스·공시 반영 전후의 판단 변화를 Actions 로그에 기록합니다.

수수료 비교는 실습 서버의 40종목 일봉에서 2026-08-14~11-12의 60거래일을 사용했습니다. 전일까지의 데이터로 신호를 계산하고 다음 거래일 시가에 체결되는 것으로 가정했습니다. 매수·매도 각각 0.015% 수수료와 매도세 0.20%를 원 단위로 절사해 적용했습니다.

| 가격 규칙 | 비용 전 | 비용 후 | 모의 주문 수 |
| --- | ---: | ---: | ---: |
| EMA만 사용 | -2.84% | -2.87% | 137 |
| 기존 EMA·RSI·모멘텀 | -2.63% | -2.80% | 87 |

두 규칙은 동일한 초기 자금 5,000만 원과 포지션 한도를 사용했습니다. 평가 종료 시점의 잔고는 종가로 평가했습니다. 호가·슬리피지·장중 체결과 과거 시점의 뉴스·공시 효과는 이 비교에서 확인하기 어렵습니다. 동일한 형식의 일봉 JSON은 `python -m app.fee_comparison candles.json`으로 비교할 수 있습니다.

로컬에서 실행하려면 아래 명령을 사용합니다.
```bash
python -m app.main --once --scan-only
python -m app.main --once
python -m app.main
```

`--once --scan-only`는 조회와 분석을 한 번 실행합니다. 주문을 활성화한 상태에서는 `--once`로 한 사이클, 옵션 없이 반복 실행합니다. 반복 실행은 사이클 완료 후 기본 15분 간격이며, 종료할 때는 `Ctrl+C`를 누릅니다.

뉴스 수집과 로컬 API 사용 현황은 별도로 확인할 수 있습니다. 아래 명령은 주문하지 않습니다.
```bash
python -m app.news_cli --provider gnews --symbol 005930 --output data/news.json
python -m app.news_cli --api-status
python -m app.dart_cli --symbol 005930 --output data/dart.json
python -m app.company_cli --symbol 035420 --output data/company_035420.json
```

외부 API 호출 없이 합성 자료로 판단 규칙과 분류 결과를 확인할 수 있습니다.
```bash
python -m app.strategy_demo
python -m app.news_evaluation --output data/evaluation.json
python -m app.event_evaluation evaluate --dataset examples/event_diagnostics.json --split development --output data/event_evaluation.json
python -m app.policy_comparison --dataset examples/policy_comparison.json --output data/policy_comparison.json
python -m app.company_cli --fixture examples/company_event_history.json --output data/company_history_demo.json
python -m pytest -q
```

## 구성
- API 인증, 종목 검색, 시세/캔들/호가/포트폴리오/주문 내역/리더보드 조회
- 선택적 OpenAI LLM 보조 판단. 사용 가능하고 신뢰도 조건을 충족하면 정량 80% + LLM 20%, 그 외에는 정량 점수를 기준으로 사용
- GNews 영문 뉴스 수집과 종목 관련성·중복·발행/수집 시각·공급 지연 검사
- 이익·매출·계약·주주환원·운영 위험의 규칙 기반 분석과 매출 변화율 추출
- 출처 조건과 시간 감쇠를 적용한 뉴스 보조점수. 조정 한도는 기본 ±0.15이며 수익률 예측값이 아님
- 종목 비중, 주문 금액, 가용 현금·수량, 장 운영시간, 쿨다운 등 사전 위험검사
- SQLite 의사결정/주문/뉴스 기록, 영속 캐시, 로컬 호출 예산과 오류 후 호출 중지
- 기사 제거 전후의 판단 비교, 분류 평가와 오답 확인 도구
- DART 공시·연결재무제표 수집, 매출·영업이익·영업이익률 비교, 비교 조건이 명확한 매출 기사의 수치 대조
- 경제사건의 기업·근거 위치·기간 표현, 매출·영업이익 변화율, 보도·전망·부인·철회·불확실성 구분
- 실제 뉴스의 독립 정답 검토 목록, 사건·수치 평가, 동일 입력의 가격/뉴스/DART 판단 비교 기록
- 사건 주체·협력사·거래 상대방의 보수적 구분, 명시적 회계기간의 정정·부인·재보도 이력
- 공시 원문 사업·위험·재무 문단 수집, 유동비율·부채/자본·누적 영업현금흐름 확인, 비교 가능한 긍정 실적 뉴스의 제한적 감쇠

## 설정
- `API_BASE_URL=http://136.119.248.78`
- `DRY_RUN=true` 주문 API 호출 없음
- `ENABLE_TRADING=false` 주문 비활성화
- `MAX_POSITION_PCT=0.15` 종목별 최대 비중
- `MAX_ORDER_VALUE_KRW=2000000` 단일 주문 최대 금액
- `CASH_RESERVE_PCT=0.10` 현금 최소 보유 비율
- `WATCHLIST` 기본값은 `.env.example`의 국내 주식 40종목입니다.
- `NEWS_ENABLED=true`, `NEWS_PROVIDERS=gnews` 뉴스 분석 활성화
- `NEWS_USE_IN_LLM=false` 뉴스의 LLM 직접 입력 비활성화
- `GNEWS_DAILY_BUDGET=80` 로컬 일일 호출 예산
- `GNEWS_CACHE_SECONDS=3600` 뉴스 캐시 유지 시간
- `DART_ENABLED=true`, `DART_API_KEY=` 선택적 DART 연동. 키가 없으면 기존 뉴스 판단을 유지
- `DART_DAILY_BUDGET=500`, `DART_CACHE_SECONDS=3600` 로컬 호출 예산과 캐시 유지 시간
- `RESEARCH_COMPARISON_ENABLED=true` 실제 판단과 별도로 LLM을 제외한 세 가지 연구용 판단 및 입력 기록
- `EVENT_HISTORY_ENABLED=true`, `DART_DOCUMENT_ENABLED=true` 사건 이력과 공시 원문 수집
- `FUNDAMENTAL_GUARD_ENABLED=true` 같은 기간의 매출 증가·이익 감소 또는 영업손실에 따른 긍정 실적 뉴스 감쇠. 실험적 규칙이며 수익률 검증값은 아님

## 주의 및 한계
- 거래 API 기준 시간은 09:00~15:00, 시간당 주문 30회, 동일 종목 30분 쿨다운, 분당 호출 100회입니다. 클라이언트는 한 프로세스에서 분당 60회로 제한합니다.
- 왕복 수수료/세금은 약 0.23%이며 시장가 주문은 일부 체결 후 잔량이 취소될 수 있습니다.
- 거래 API에는 뉴스 검색 경로가 없습니다. GNews를 별도 공급원으로 사용하며 무료 플랜의 12시간 지연을 표시하고 감쇠합니다.
- 네이버 API HUB 통신 어댑터는 포함하지만 현행 이용 조건에 따라 자동 수집·분석·저장 경로에서는 제외합니다.
- 뉴스 분석은 학습된 금융 감성 모델이 아닌 규칙 기반 도구입니다. 합성 자료의 평가 결과를 실제 뉴스 정확도나 수익률로 해석하지 않습니다.
- DART는 실시간 뉴스 대체재가 아닙니다. 현재 관측한 공시를 사용하며 과거 시점의 공시 상태를 복원하지 않습니다. 연결 기준·연도·분기·전년 동기 비교가 명확한 매출 기사만 대조하고, 불일치 기사는 뉴스 점수와 LLM 입력에서 제외합니다. 일치하더라도 별도 매수 점수를 더하지 않습니다.
- 로컬 뉴스 호출 예산은 같은 API 키와 같은 SQLite 파일을 공유하는 실행에만 적용됩니다. 다른 컴퓨터나 외부 프로그램의 호출은 포함하지 않습니다.
- 일일 손실 제한, 미체결 주문의 자동 대사, 호가별 예상 체결비용 계산은 아직 완성되지 않았습니다. 응답이 불명확한 주문은 기록하고 새 주문을 차단합니다.
- 모의투자 환경에서 `--scan-only`와 DRY_RUN으로 검증한 뒤에만 `DRY_RUN=false`, `ENABLE_TRADING=true`로 변경하세요.

뉴스 모듈의 상세 설정과 평가는 [기능 안내](docs/NEWS_GUIDE_V5.md), 점수 계산은 [판단 규칙](docs/NEWS_DECISION_V4.md), API 경로는 [API 매핑](docs/API_MAPPING.md)을 참고하세요.
DART 인증키 설정과 대조 조건은 [공시·재무정보 안내](docs/DART_GUIDE.md)를 참고하세요.
경제사건 추출, 실제 뉴스 정답 검토 및 기능별 비교는 [분석·평가 안내](docs/EVENT_RESEARCH.md)를 참고하세요.
기업 귀속·공시 원문·재무 해석·사건 이력은 [기업 분석 안내](docs/COMPANY_RESEARCH.md)를 참고하세요.
