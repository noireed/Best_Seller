# 개발 담당: 최준혁
"""The same position sizing calculation for live trading and historical replay."""


def buy_order_size(settings, price, portfolio, holding, symbol_count):
    total = portfolio.get("totalAssetValue", 0)
    reserve = total * settings.cash_reserve_pct
    cash = max(0, portfolio.get("availableCash", 0) - reserve)
    target = min(total * settings.max_position_pct, (total - reserve) / symbol_count)
    held_value = holding.get("quantity", 0) * price
    room = max(0, target - held_value)
    gap = room / target if target > 0 else 0
    band = getattr(settings, "rebalance_band_pct", 0)
    suppressed = holding.get("quantity", 0) > 0 and gap < band
    quantity = int(min(settings.max_order_value_krw, cash / 1.00015, room) // price)
    return {"quantity": 0 if suppressed else quantity, "target_value": target,
            "shortfall_pct": gap, "rebalance_band_pct": band, "suppressed": suppressed}
