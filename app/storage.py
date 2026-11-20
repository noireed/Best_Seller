import sqlite3, json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from .event_history import EventHistory

class Store:
    def __init__(self, path, *, order_checkpoint=None):
        self.order_checkpoint = order_checkpoint or (lambda: None)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, at TEXT, symbol TEXT, technical TEXT, llm TEXT, action TEXT, reason TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, at TEXT, symbol TEXT, side TEXT, quantity INTEGER, price INTEGER, status TEXT, response TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS news_reports(id INTEGER PRIMARY KEY, at TEXT, symbol TEXT, status TEXT, report TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS policy_decisions(id INTEGER PRIMARY KEY, at TEXT, symbol TEXT, report TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS order_intents(id INTEGER PRIMARY KEY, at TEXT, symbol TEXT, state TEXT, report TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS company_event_outcomes(symbol TEXT PRIMARY KEY, report TEXT)")
        self.db.commit()
        self.event_history = EventHistory(self.db)

    def outcome_library(self):
        return {symbol: json.loads(report) for symbol, report in self.db.execute("SELECT symbol,report FROM company_event_outcomes")}

    def save_outcomes(self, reports):
        self.db.executemany("INSERT OR REPLACE INTO company_event_outcomes VALUES(?,?)",
            [(symbol, json.dumps(report, ensure_ascii=False, allow_nan=False)) for symbol, report in reports.items()])
        self.db.commit()

    def record_event_history(self, report, aliases, *, now, max_age_hours=48):
        self.event_history.ingest(report, aliases, now=now, max_age_hours=max_age_hours)
        return self.event_history.snapshot(report["symbol"], now=now)

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
        try:
            self.order_checkpoint()
        except Exception:
            # No submission has started when the pre-order checkpoint fails.
            self.db.execute("UPDATE order_intents SET state='RESPONDED',report=? WHERE id=?",
                            (json.dumps({"not_submitted": True}), row.lastrowid))
            self.db.commit()
            raise
        return row.lastrowid

    def finish_order(self, identity, state, report):
        if state not in ("RESPONDED", "UNKNOWN"):
            raise ValueError("invalid submission state")
        if state == "UNKNOWN":
            current = self.db.execute("SELECT report FROM order_intents WHERE id=?", (identity,)).fetchone()
            report = {**json.loads(current[0]), "last_error": report}
        self.db.execute("UPDATE order_intents SET state=?,report=? WHERE id=?",
                        (state, json.dumps(report, ensure_ascii=False, allow_nan=False), identity))
        self.db.commit()
        self.order_checkpoint()

    def order(self, symbol, side, quantity, price, status, response):
        self.db.execute("INSERT INTO orders(at,symbol,side,quantity,price,status,response) VALUES(?,?,?,?,?,?,?)",
            (datetime.now(ZoneInfo("Asia/Seoul")).isoformat(), symbol, side, quantity, price, status,
             json.dumps(response, ensure_ascii=False)))
        self.db.commit()
