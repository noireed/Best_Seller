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
        self.db.execute("CREATE TABLE IF NOT EXISTS news_reports(id INTEGER PRIMARY KEY, at TEXT, symbol TEXT, status TEXT, report TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS policy_decisions(id INTEGER PRIMARY KEY, at TEXT, symbol TEXT, report TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS order_intents(id INTEGER PRIMARY KEY, at TEXT, symbol TEXT, state TEXT, report TEXT)")
        self.db.commit()

    def news(self, report):
        if any(a.get("provider") == "naver" for a in report.get("articles", [])):
            raise ValueError("Naver search content is excluded from analysis storage")
        self.db.execute("INSERT INTO news_reports(at,symbol,status,report) VALUES(?,?,?,?)",
                        (report["as_of"], report["symbol"], report["status"],
                         json.dumps(report, ensure_ascii=False, allow_nan=False)))
        self.db.commit()

    def decision(self, symbol, technical, llm, action, reason):
        self.db.execute("INSERT INTO decisions(at,symbol,technical,llm,action,reason) VALUES(?,?,?,?,?,?)",
            (datetime.now(ZoneInfo("Asia/Seoul")).isoformat(), symbol,
             json.dumps(technical, ensure_ascii=False), json.dumps(llm, ensure_ascii=False), action, reason))
        self.db.commit()

    def policy_decision(self, symbol, report):
        self.db.execute("INSERT INTO policy_decisions(at,symbol,report) VALUES(?,?,?)",
                        (datetime.now(ZoneInfo("Asia/Seoul")).isoformat(), symbol,
                         json.dumps(report, ensure_ascii=False, allow_nan=False)))
        self.db.commit()

    def unresolved_orders(self):
        return self.db.execute("SELECT COUNT(*) FROM order_intents WHERE state IN ('SUBMITTING','UNKNOWN')").fetchone()[0]

    def begin_order(self, symbol, report):
        row = self.db.execute("INSERT INTO order_intents(at,symbol,state,report) VALUES(?,?,?,?)",
            (datetime.now(ZoneInfo("Asia/Seoul")).isoformat(), symbol, "SUBMITTING",
             json.dumps(report, ensure_ascii=False, allow_nan=False)))
        self.db.commit()
        return row.lastrowid

    def finish_order(self, identity, state, report):
        if state not in ("RESPONDED", "UNKNOWN"):
            raise ValueError("invalid submission state")
        self.db.execute("UPDATE order_intents SET state=?,report=? WHERE id=?",
                        (state, json.dumps(report, ensure_ascii=False, allow_nan=False), identity))
        self.db.commit()

    def order(self, symbol, side, quantity, price, status, response):
        self.db.execute("INSERT INTO orders(at,symbol,side,quantity,price,status,response) VALUES(?,?,?,?,?,?,?)",
            (datetime.now(ZoneInfo("Asia/Seoul")).isoformat(), symbol, side, quantity, price, status,
             json.dumps(response, ensure_ascii=False)))
        self.db.commit()
