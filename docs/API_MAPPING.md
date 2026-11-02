# API 매핑
| API | 구현 |
|---|---|
| POST `/api/auth/login` | `TradingAPI.login` |
| GET `/api/auth/me` | `TradingAPI.me` |
| GET `/api/stocks/price` | `TradingAPI.prices` |
| GET `/api/stocks/{symbol}/candles` | `TradingAPI.candles` |
| GET `/api/portfolio` | `TradingAPI.portfolio` |
| GET `/api/orders/limit` | `TradingAPI.limits` |
| POST `/api/orders` | `TradingAPI.place_order` |
| GET `/api/orders` | `TradingAPI.orders` |
| DELETE `/api/orders/{orderId}` | `TradingAPI.cancel_order` |

뉴스 조회 API는 제공된 명세에 없으므로 현재 LLM 분석은 시세/지표/포트폴리오에 한정됩니다. 일일 손실 제한도 신뢰할 수 있는 일중 기준가 시계열이 없어 아직 자동 차단하지 않습니다.
