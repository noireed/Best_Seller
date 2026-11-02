import time
import requests

class APIError(RuntimeError):
    pass

class TradingAPI:
    def __init__(self, base_url, team_code):
        self.base_url = base_url.rstrip("/")
        self.team_code = team_code
        self.session = requests.Session()
        self.token = None
        self.token_expiry = 0
        self.last_calls = []

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
                detail = r.json()
            except Exception:
                detail = r.text[:300]
            raise APIError(f"HTTP {r.status_code}: {detail}")

    def login(self):
        if not self.team_code:
            raise APIError("TEAM_CODE가 .env에 설정되지 않았습니다.")
        r = self.session.post(f"{self.base_url}/api/auth/login",
                              json={"teamCode": self.team_code}, timeout=15)
        self._check(r)
        data = r.json()
        self.token = data["accessToken"]
        self.token_expiry = time.monotonic() + 23 * 3600
        return data

    def _request(self, method, path, **kwargs):
        if not self.token or time.monotonic() >= self.token_expiry:
            self.login()
        self._throttle()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        r = self.session.request(method, self.base_url + path, headers=headers,
                                 timeout=20, **kwargs)
        if r.status_code == 401:
            self.token = None
            self.login()
            headers["Authorization"] = f"Bearer {self.token}"
            self._throttle()
            r = self.session.request(method, self.base_url + path, headers=headers,
                                     timeout=20, **kwargs)
        self._check(r)
        return r.json() if r.content else {}

    def me(self): return self._request("GET", "/api/auth/me")
    def prices(self, symbols):
        return self._request("GET", "/api/stocks/price",
                             params={"symbols": ",".join(symbols)})
    def candles(self, symbol, interval="1d", count=100):
        return self._request("GET", f"/api/stocks/{symbol}/candles",
                             params={"interval": interval, "count": count})
    def portfolio(self): return self._request("GET", "/api/portfolio")
    def limits(self): return self._request("GET", "/api/orders/limit")
    def orders(self, status=None):
        params = {"page": 0, "size": 100}
        if status: params["status"] = status
        return self._request("GET", "/api/orders", params=params)
    def place_order(self, symbol, side, quantity, price=None):
        body = {"symbol": symbol, "order_code": side, "quantity": int(quantity)}
        if price is not None: body["price"] = int(price)
        return self._request("POST", "/api/orders", json=body)
    def cancel_order(self, order_id):
        return self._request("DELETE", f"/api/orders/{order_id}")
