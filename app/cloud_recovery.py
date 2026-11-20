# 개발 담당: 최준혁
"""Resolve interrupted submissions against complete server order history."""
from datetime import timedelta
import json

from .news import parse_time


def reconcile_orders(store, api):
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
