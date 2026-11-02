# API 매핑
| API | 구현 |
|---|---|
| POST `/api/auth/login` | `TradingAPI.login` |
| GET `/api/auth/me` | `TradingAPI.me` |
| GET `/api/stocks` | `TradingAPI.stocks` |
| GET `/api/stocks/price` | `TradingAPI.prices` |
| GET `/api/stocks/{symbol}/orderbook` | `TradingAPI.orderbook` |
| GET `/api/stocks/{symbol}/candles` | `TradingAPI.candles(before=...)` / `candle_history` |
| GET `/api/portfolio` | `TradingAPI.portfolio` |
| GET `/api/orders/limit` | `TradingAPI.limits` |
| POST `/api/orders` | `TradingAPI.place_order` |
| GET `/api/orders` | `TradingAPI.orders(status, page, size)` |
| DELETE `/api/orders/{orderId}` | `TradingAPI.cancel_order` |
| GET `/api/leaderboard` | `TradingAPI.leaderboard` (인증 없이 조회) |

거래 API에는 뉴스 조회 경로가 없습니다. 뉴스는 별도 공급원에서 수집합니다. 일일 손실 제한은 아직 자동 차단하지 않습니다.

## 뉴스 공급원

| API | 구현 | 인증 |
|---|---|---|
| GET `https://naverapihub.apigw.ntruss.com/search/v1/news` | `NaverNewsClient.search` | `X-NCP-APIGW-API-KEY-ID`, `X-NCP-APIGW-API-KEY` |
| GET `https://gnews.io/api/v4/search` | `GNewsClient.search` | `apikey` |

자동 분석에는 GNews를 사용합니다. 네이버 API HUB 통신 어댑터는 포함하지만 이용 조건에 따라
NewsService가 호출 전에 차단하며 경제사건 계산·LLM 입력·분석 저장에서도 제외합니다.
GNews의 로컬 캐시·호출 예산·오류 중지는 DurableNewsClient가 처리합니다.

## AQuA 실제 문서 대조 (2026-11-03)

http://136.119.248.78/docs 에 직접 HTTP GET을 수행해 응답 200과 본문을 확인했습니다.
인증 방식은 팀 코드 → 로그인 토큰 → Authorization Bearer이며 별도의 AQuA API 키는 아닙니다.

뉴스 API 명세:
- https://docs.gnews.io/endpoints/search-endpoint
- https://api.ncloud-docs.com/docs/naver-api-hub-search-news
