import httpx

class AquaClient:
    def __init__(self, base_url: str, team_code: str):
        self.client = httpx.Client(base_url=base_url, timeout=15)
        self.team_code = team_code
        self.token = None

    def login(self):
        r = self.client.post("/api/auth/login", json={"teamCode": self.team_code})
        r.raise_for_status()
        self.token = r.json()["accessToken"]
        self.client.headers.update({"Authorization": f"Bearer {self.token}"})

    def stocks(self, keyword=""):
        r = self.client.get("/api/stocks", params={"keyword": keyword})
        r.raise_for_status()
        return r.json()

    def prices(self, symbols):
        r = self.client.get("/api/stocks/price", params={"symbols": ",".join(symbols)})
        r.raise_for_status()
        return r.json()

    def candles(self, symbol, interval="1d"):
        r = self.client.get(f"/api/stocks/{symbol}/candles", params={"interval": interval})
        r.raise_for_status()
        return r.json()

    def orderbook(self, symbol):
        r = self.client.get(f"/api/stocks/{symbol}/orderbook")
        r.raise_for_status()
        return r.json()

    def order_limit(self):
        r = self.client.get("/api/orders/limit")
        r.raise_for_status()
        return r.json()

    def place_order(self, symbol, side, quantity, price=None):
        body = {"symbol": symbol, "order_code": side, "quantity": quantity}
        if price is not None:
            body["price"] = price
        r = self.client.post("/api/orders", json=body)
        r.raise_for_status()
        return r.json()

    def cancel_order(self, order_id):
        r = self.client.delete(f"/api/orders/{order_id}")
        r.raise_for_status()
        return r.json()
