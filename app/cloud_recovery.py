# 개발 담당: 최준혁
"""Resolve interrupted submissions against complete server order history."""
from datetime import datetime, timedelta, timezone
import hashlib
import math
import json

from .news import parse_time


def reconcile_orders(store, api, *, now=None):
    now = now or datetime.now(timezone.utc)
    pending = store.db.execute(
        "SELECT id,at,symbol,report FROM order_intents WHERE state IN ('SUBMITTING','UNKNOWN')"
    ).fetchall()
    if not pending:
        return True
    first = api.orders(page=0, size=100)
    pages = first.get("totalPages")
    if type(pages) is not int or pages < 1:
        return False
    orders = list(first["orders"])
    for page in range(1, pages):
        response = api.orders(page=page, size=100)
        if response.get("totalPages") != pages:
            return False
        orders.extend(response["orders"])
    identities = [str(row["orderId"]) for row in orders]
    if len(identities) != len(set(identities)):
        return False
    used = set()
    for (raw,) in store.db.execute("SELECT report FROM order_intents WHERE state='RESPONDED'"):
        identity = json.loads(raw).get("orderId")
        if identity is not None:
            used.add(str(identity))
    recorded = {str(json.loads(raw).get("orderId"))
                for (raw,) in store.db.execute("SELECT response FROM orders")}
    if not (recorded-{"None"}) <= set(identities):
        # An apparently empty/short history must not erase known accepted orders.
        return False
    for identity, at, symbol, raw in pending:
        request = json.loads(raw)
        started = parse_time(at)
        if request.get("orderId") is not None:
            matches = [row for row in orders if str(row["orderId"]) == str(request["orderId"])]
        else:
            matches = [row for row in orders
                       if row.get("symbol") == symbol
                       and row.get("side") == request.get("action")
                       and type(request.get("quantity")) is int
                       and row.get("quantity") == request["quantity"]
                       and started - timedelta(seconds=5) <= parse_time(row["createdAt"])
                       <= started + timedelta(seconds=120)]
        if not matches and not request.get("orderId") and now-started >= timedelta(minutes=10):
            # A timeout is not a rejection. Establish a stable negative observation using
            # the complete server history AND unchanged, unreserved issuer holdings.
            # The original request is never replayed, and no fill/rejection is invented.
            context = request.get("order_context") or {}
            held_before = context.get("held_quantity")
            if held_before is None and request.get("action") == "BUY":
                sizing = request.get("position_sizing") or {}
                price = request.get("price", 0)
                if type(price) in (int, float) and math.isfinite(price) and price > 0:
                    value = sizing.get("held_value")
                    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                        quotient = value/price
                        if quotient.is_integer():
                            held_before = int(quotient)
            nearby = [r for r in orders if r.get("symbol") == symbol
                      and parse_time(r["createdAt"]) >= started-timedelta(seconds=5)]
            active = [r for r in orders if r.get("symbol") == symbol and r.get("status") in ("OPEN", "PARTIAL")]
            if type(held_before) is int and held_before >= 0 and not nearby and not active:
                portfolio = api.portfolio()
                holdings = portfolio["holdings"]
                if not isinstance(holdings, list):
                    raise ValueError("invalid reconciliation holdings")
                holding = next((h for h in holdings if h["symbol"] == symbol), {})
                held, available = holding.get("quantity", 0), holding.get("availableQuantity", 0)
                if type(held) is int and type(available) is int and held == available == held_before:
                    fingerprint = hashlib.sha256(json.dumps(sorted(orders, key=lambda r: str(r["orderId"])),
                        sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
                    previous = request.get("absence_check") or {}
                    elapsed = ((now-parse_time(previous["at"])).total_seconds()
                               if previous.get("at") else -1)
                    if (previous.get("history_sha256") == fingerprint
                            and previous.get("held_quantity") == held and elapsed >= 60):
                        store.finish_order(identity, "RESPONDED", {**request,
                            "outcome": "NO_SERVER_ORDER_OBSERVED", "reconciled_at": now.isoformat(),
                            "confirmation": {"first_at": previous["at"], "last_at": now.isoformat(),
                                "history_sha256": fingerprint, "held_quantity": held,
                                "server_order_count": len(orders)},
                            "not_replayed": True})
                        continue
                    if previous.get("history_sha256") != fingerprint or previous.get("held_quantity") != held:
                        store.note_order_check(identity, {"at": now.isoformat(),
                            "history_sha256": fingerprint, "held_quantity": held})
        if len(matches) != 1:
            continue
        order = matches[0]
        if str(order["orderId"]) in used or order.get("status") not in ("FILLED", "CANCELLED"):
            continue
        used.add(str(order["orderId"]))
        if str(order["orderId"]) not in recorded:
            price = order.get("executedPrice") or order.get("price") or request.get("price") or 0
            store.order(symbol, order["side"], order["quantity"], price, order["status"], order)
            recorded.add(str(order["orderId"]))
        store.finish_order(identity, "RESPONDED", order)
    return store.unresolved_orders() == 0
