import time
from datetime import datetime, timezone
import requests

class APIError(RuntimeError):
    def __init__(self, message, *, status=None, code=None, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.retry_after = status, code, retry_after

class TradingAPI:
    def __init__(self, base_url, team_code):
        self.base_url = base_url.rstrip("/")
        self.team_code = team_code
        self.session = requests.Session()
        self.token = None
        self.token_expiry = 0
        self.last_calls = []
        self.request_timeout = 20

    def _throttle(self):
        now = time.monotonic()
        self.last_calls = [t for t in self.last_calls if now - t < 60]
        if len(self.last_calls) >= 60:
            time.sleep(max(0, 60 - (now - self.last_calls[0])) + 0.1)
        self.last_calls.append(time.monotonic())

    @staticmethod
    def _check(r):
        if not r.ok:
            try:
                error = r.json().get("error", {})
                code = error.get("code") if isinstance(error, dict) else None
            except (ValueError, AttributeError):
                code = None
            # Keep machine-readable categories, without logging server bodies/tokens.
            raise APIError(f"HTTP {r.status_code}: {code or 'API_ERROR'}",
                           status=r.status_code, code=code,
                           retry_after=r.headers.get("Retry-After"))

    def login(self):
        if not self.team_code:
            raise APIError("TEAM_CODE가 .env에 설정되지 않았습니다.")
        self._throttle()
        try:
            r = self.session.post(f"{self.base_url}/api/auth/login",
                                  json={"teamCode": self.team_code}, timeout=15)
        except requests.RequestException as exc:
            raise APIError("로그인 통신 실패") from exc
        self._check(r)
        try:
            data = r.json()
            token = data["accessToken"]
            expires_at = datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00"))
            if not isinstance(token, str) or not token or expires_at.tzinfo is None:
                raise ValueError("invalid token or expiry")
            seconds = (expires_at - datetime.now(timezone.utc)).total_seconds() - 60
            if seconds <= 0:
                raise ValueError("expired token")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise APIError("로그인 응답 형식 또는 만료 시각 오류") from exc
        self.token = token
        self.token_expiry = time.monotonic() + seconds
        return data

    def _request(self, method, path, *, authenticated=True, **kwargs):
        if authenticated and (not self.token or time.monotonic() >= self.token_expiry):
            self.login()
        self._throttle()
        headers = kwargs.pop("headers", {})
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"
        r = self._send(method, path, headers, kwargs)
        if r.status_code == 401 and authenticated:
            self.token = None
            self.login()
            headers["Authorization"] = f"Bearer {self.token}"
            self._throttle()
            r = self._send(method, path, headers, kwargs)
        self._check(r)
        try:
            return r.json() if r.content else {}
        except ValueError as exc:
            raise APIError("API JSON 응답 오류") from exc

    def _send(self, method, path, headers, kwargs):
        try:
            return self.session.request(method, self.base_url + path, headers=headers,
                                        timeout=self.request_timeout, **kwargs)
        except requests.RequestException as exc:
            # A timed-out order may already have been accepted. Never replay it here.
            raise APIError("API 통신 실패: 주문 요청이었다면 내역 조회로 결과 확인 필요") from exc

    def me(self): return self._request("GET", "/api/auth/me")
    def stocks(self, keyword=""):
        return self._request("GET", "/api/stocks", params={"keyword": keyword})
    def orderbook(self, symbol):
        return self._request("GET", f"/api/stocks/{symbol}/orderbook")
    def leaderboard(self):
        return self._request("GET", "/api/leaderboard", authenticated=False)
    def prices(self, symbols):
        return self._request("GET", "/api/stocks/price",
                             params={"symbols": ",".join(symbols)})
    def candles(self, symbol, interval="1d", count=100, before=None):
        if interval not in ("1m", "1d") or not 1 <= count <= 200:
            raise ValueError("interval must be 1m/1d, count must be 1..200")
        params = {"interval": interval, "count": count}
        if before is not None:
            params["before"] = before  # requests encodes +09:00 in query parameters.
        return self._request("GET", f"/api/stocks/{symbol}/candles",
                             params=params)
    def candle_history(self, symbol, interval="1d", count=200, max_pages=5):
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        before, seen, candles = None, set(), {}
        complete = False
        for _ in range(max_pages):
            data = self.candles(symbol, interval, count, before=before)
            for row in data["candles"]:
                candles[row["timestamp"]] = row
            cursor = data.get("nextBefore")
            if not cursor:
                complete = True
                break
            if cursor in seen:
                raise APIError("캔들 페이지 커서 반복")
            seen.add(cursor)
            before = cursor
        return {"symbol": symbol, "interval": interval,
                "candles": sorted(candles.values(), key=lambda r: datetime.fromisoformat(r["timestamp"])),
                "complete": complete, "nextBefore": None if complete else before}
    def portfolio(self): return self._request("GET", "/api/portfolio")
    def limits(self): return self._request("GET", "/api/orders/limit")
    def orders(self, status=None, page=0, size=100):
        if page < 0 or not 1 <= size <= 100:
            raise ValueError("page must be >=0, size must be 1..100")
        if status is not None and status not in ("OPEN", "PARTIAL", "FILLED", "CANCELLED"):
            raise ValueError("invalid order status")
        params = {"page": page, "size": size}
        if status: params["status"] = status
        return self._request("GET", "/api/orders", params=params)
    def place_order(self, symbol, side, quantity, price=None):
        body = {"symbol": symbol, "order_code": side, "quantity": int(quantity)}
        if price is not None: body["price"] = int(price)
        return self._request("POST", "/api/orders", json=body)
    def cancel_order(self, order_id):
        return self._request("DELETE", f"/api/orders/{order_id}")
