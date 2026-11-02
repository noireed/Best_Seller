import sqlite3, json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, at TEXT, symbol TEXT, technical TEXT, llm TEXT, action TEXT, reason TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, at TEXT, symbol TEXT, side TEXT, quantity INTEGER, price INTEGER, status TEXT, response TEXT)")
        self.db.commit()

    def decision(self, symbol, technical, llm, action, reason):
        self.db.execute("INSERT INTO decisions(at,symbol,technical,llm,action,reason) VALUES(?,?,?,?,?,?)",
            (datetime.now(ZoneInfo("Asia/Seoul")).isoformat(), symbol,
             json.dumps(technical, ensure_ascii=False), json.dumps(llm, ensure_ascii=False), action, reason))
        self.db.commit()

    def order(self, symbol, side, quantity, price, status, response):
        self.db.execute("INSERT INTO orders(at,symbol,side,quantity,price,status,response) VALUES(?,?,?,?,?,?,?)",
            (datetime.now(ZoneInfo("Asia/Seoul")).isoformat(), symbol, side, quantity, price, status,
             json.dumps(response, ensure_ascii=False)))
        self.db.commit()
