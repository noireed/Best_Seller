from datetime import datetime
import json
from unittest.mock import MagicMock, Mock, patch
from urllib.error import URLError

import pytest

from app.api_client import APIError
from app.cloud_recovery import reconcile_orders
from app import cloud_watchdog, cloud_cycle, cloud_session
from app import cloud_continuation
from app.config import Settings
from app.engine import TradingEngine
from app.storage import Store

NOW = datetime.fromisoformat("2026-11-20T09:00:00+09:00")


def pending_store(tmp_path):
    store = Store(str(tmp_path / "trading.db"))
    identity = store.begin_order("005930", {"action": "BUY", "quantity": 2, "price": 100})
    store.db.execute("UPDATE order_intents SET at=?", (NOW.isoformat(),))
    store.db.commit()
    return store, identity


def order(**changes):
    return {"orderId": 42, "symbol": "005930", "side": "BUY", "quantity": 2,
            "status": "FILLED", "createdAt": "2026-11-20T09:00:02+09:00", **changes}


def test_unknown_intent_keeps_request_and_reconciles_without_resubmission(tmp_path):
    store, identity = pending_store(tmp_path)
    store.finish_order(identity, "UNKNOWN", {"error_type": "Timeout"})
    saved = json.loads(store.db.execute("SELECT report FROM order_intents").fetchone()[0])
    assert saved["quantity"] == 2 and saved["action"] == "BUY"
    api = Mock()
    api.orders.return_value = {"totalPages": 1, "orders": [order()]}
    assert reconcile_orders(store, api)
    assert store.unresolved_orders() == 0
    assert reconcile_orders(store, api)
    assert store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    api.place_order.assert_not_called()
    api.orders.assert_called_once()
    store.db.close()


@pytest.mark.parametrize("rows", [[], [order(status="OPEN")], [order(status="PARTIAL")],
    [order(side="SELL")], [order(quantity=3)], [order(symbol="000660")],
    [order(createdAt="2026-11-20T09:05:00+09:00")],
    [order(), order(orderId=43)], [order(), order()]])
def test_ambiguous_or_pending_history_does_not_replay_orders(tmp_path, rows):
    store, _ = pending_store(tmp_path)
    api = Mock()
    api.orders.return_value = {"totalPages": 1, "orders": rows}
    assert not reconcile_orders(store, api)
    assert store.unresolved_orders() == 1
    api.place_order.assert_not_called()
    store.db.close()


def test_recovery_reads_all_pages_before_matching(tmp_path):
    store, _ = pending_store(tmp_path)
    api = Mock()
    api.orders.side_effect = [{"totalPages": 2, "orders": []},
                              {"totalPages": 2, "orders": [order()]}]
    assert reconcile_orders(store, api)
    assert api.orders.call_count == 2
    store.db.close()


def test_recovery_does_not_assign_one_server_order_twice(tmp_path):
    store, _ = pending_store(tmp_path)
    store.begin_order("005930", {"action": "BUY", "quantity": 2})
    store.db.execute("UPDATE order_intents SET at=?", (NOW.isoformat(),))
    store.db.commit()
    api = Mock()
    api.orders.return_value = {"totalPages": 1, "orders": [order()]}
    assert not reconcile_orders(store, api)
    assert store.unresolved_orders() == 1
    store.db.close()


@pytest.mark.parametrize("error, execution, unresolved", [
    (None, "FILLED", 0), (400, "REJECTED", 0), (429, "REJECTED", 0),
    (500, "UNKNOWN_ORDER_STATE", 1),
])
def test_order_acknowledgement_survives_following_failures(tmp_path, error, execution, unresolved):
    states = []
    store = Store(str(tmp_path / "trading.db"))
    store.order_checkpoint = lambda: states.append(store.unresolved_orders())
    settings = Settings(_env_file=None, gemini_required_for_buy=False, watchlist="005930", dry_run=False, enable_trading=True)
    api, llm = Mock(), Mock()
    api.portfolio.side_effect = [{"totalAssetValue": 100000000, "availableCash": 100000000,
                                 "holdings": []}, APIError("read failed", status=503)]
    api.limits.return_value = {"remainingTrades": 10, "cooldowns": []}
    api.prices.return_value = {"result": [{"symbol": "005930", "price": 70000,
                                          "timestamp": NOW.isoformat()}]}
    api.candles.return_value = {"candles": []}
    api.place_order.return_value = {"orderId": 42, "status": "FILLED"}
    if error:
        api.place_order.side_effect = APIError("order error", status=error)
    llm.assess.return_value = {"availability": "disabled"}
    with patch("app.engine.technical_signal", return_value={"score": 1, "data_ready": True}):
        rows = TradingEngine(settings, api, llm, store, clock=lambda: NOW).run_cycle()
    assert rows[0]["execution"] == execution
    assert store.unresolved_orders() == unresolved
    assert states == [1, unresolved]
    api.place_order.assert_called_once()
    store.db.close()


def test_failed_pre_order_checkpoint_marks_submission_as_not_started(tmp_path):
    store = Store(str(tmp_path / "trading.db"), order_checkpoint=Mock(side_effect=RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        store.begin_order("005930", {"action": "BUY", "quantity": 2})
    assert store.unresolved_orders() == 0
    raw = store.db.execute("SELECT report FROM order_intents").fetchone()[0]
    assert json.loads(raw) == {"not_submitted": True}
    store.db.close()


@pytest.mark.parametrize("mode, expected", [
    ("terminated", "restarted"), ("replacement", "replacement_active"),
    ("disabled", "disabled"), ("manual", "not_continuous"), ("research", "not_trade"),
])
def test_watchdog_recovers_only_missing_continuous_trade(monkeypatch, mode, expected):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    session, dispatch = Mock(), Mock()
    session.headers = {}
    monkeypatch.setattr(cloud_watchdog.requests, "Session", Mock(return_value=session))
    monkeypatch.setattr(cloud_watchdog, "handoff", dispatch)

    def get(url, **kwargs):
        if url.endswith("/actions/runs/1"):
            value = {"head_branch": "main", "event": "workflow_dispatch",
                     "display_title": "AQuA manual check" if mode == "manual" else "AQuA continuous session"}
        elif url.endswith("/actions/runs/1/jobs"):
            value = {"jobs": [{"name": "trade", "conclusion": "skipped" if mode == "research" else "failure"}]}
        elif url.endswith("/actions/workflows/simulation.yml"):
            value = {"state": "disabled_manually" if mode == "disabled" else "active"}
        elif url.endswith("/actions/workflows/simulation.yml/runs"):
            value = {"workflow_runs": ([{"id": 2, "status": "queued", "event": "workflow_dispatch",
                                         "display_title": "AQuA continuous session"}]
                                       if mode == "replacement" else [])}
        elif url.endswith("/actions/runs/2/jobs"):
            value = {"jobs": []}
        else:
            raise AssertionError(url)
        response = Mock()
        response.json.return_value = value
        return response

    session.get.side_effect = get
    assert cloud_watchdog.recover_session(1) == expected
    assert dispatch.call_count == (expected == "restarted")


@pytest.mark.parametrize("instant, count, broken, verified, expected", [
    ("2026-11-23T09:00:00+09:00", 2, False, None, True),
    ("2026-11-23T08:59:59+09:00", 2, False, None, False),
    ("2026-11-21T09:00:00+09:00", 2, False, None, False),
    ("2026-11-23T09:00:00+09:00", 1, False, None, False),
    ("2026-11-23T09:00:00+09:00", 2, True, None, False),
    ("2026-11-23T09:01:00+09:00", 2, False, "2026-11-23T09:00:00+09:00", False),
])
def test_overnight_verification_requires_next_market_and_complete_real_cycle(
        instant, count, broken, verified, expected):
    metadata = {"standby_since": "2026-11-20T15:00:00+09:00",
                "next_open_at": "2026-11-23T09:00:00+09:00",
                "overnight_verified_at": verified}
    rows = [{"symbol": symbol, "execution": "NO_ORDER"} for symbol in ("005930", "000660")][:count]
    if broken:
        rows[0]["execution"] = "DATA_ERROR"
    assert cloud_cycle.overnight_resumed(metadata, datetime.fromisoformat(instant), rows,
                                         ["005930", "000660"]) is expected


def test_standby_preserves_weekend_start_across_successive_runners(monkeypatch):
    metadata = {}
    checkpoint = Mock()

    def restore(directory, **kwargs):
        (directory / "cycle.json").write_text(json.dumps(metadata), encoding="utf-8")
        return "ready"

    def save(directory, status, **kwargs):
        metadata.update(json.loads((directory / "cycle.json").read_text(encoding="utf-8")))

    checkpoint.restore.side_effect = restore
    checkpoint.save.side_effect = save
    monkeypatch.setattr(cloud_session, "Checkpoint", Mock(return_value=checkpoint))
    opening = datetime.fromisoformat("2026-11-23T09:00:00+09:00")
    cloud_session.record_standby(datetime.fromisoformat("2026-11-20T15:00:00+09:00"), opening)
    cloud_session.record_standby(datetime.fromisoformat("2026-11-23T08:00:00+09:00"), opening)
    assert metadata["standby_since"] == "2026-11-20T15:00:00+09:00"
    metadata["overnight_verified_at"] = opening.isoformat()
    cloud_session.record_standby(datetime.fromisoformat("2026-11-23T15:00:00+09:00"),
                                 datetime.fromisoformat("2026-11-24T09:00:00+09:00"))
    assert metadata["standby_since"] == "2026-11-23T15:00:00+09:00"


def test_successor_is_reserved_without_application_dependencies(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "noireed/Best_Seller")
    response = MagicMock()
    response.status = 204
    response.__enter__.return_value = response
    send = Mock(return_value=response)
    monkeypatch.setattr(cloud_continuation, "urlopen", send)
    cloud_continuation.reserve_successor()
    request = send.call_args.args[0]
    assert request.full_url.endswith("/repos/noireed/Best_Seller/actions/workflows/simulation.yml/dispatches")
    assert json.loads(request.data) == {"ref": "main", "inputs": {"trade": True, "session": True}}


def test_successor_reservation_retries_transport_errors(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "noireed/Best_Seller")
    response = MagicMock()
    response.status = 204
    response.__enter__.return_value = response
    send = Mock(side_effect=[URLError("offline"), response])
    sleep = Mock()
    monkeypatch.setattr(cloud_continuation, "urlopen", send)
    monkeypatch.setattr(cloud_continuation.time, "sleep", sleep)
    cloud_continuation.reserve_successor()
    assert send.call_count == 2
    sleep.assert_called_once_with(15)


@pytest.mark.parametrize("legacy", [False,True])
def test_absent_timed_out_order_requires_two_stable_observations_and_unchanged_holdings(tmp_path,legacy):
    from datetime import timedelta
    store,identity=pending_store(tmp_path)
    row=json.loads(store.db.execute("SELECT report FROM order_intents").fetchone()[0])
    row.update(position_sizing={"held_value":2400},price=100)
    if not legacy:row["order_context"]={"held_quantity":24}
    store.db.execute("UPDATE order_intents SET report=?",(json.dumps(row),));store.db.commit()
    store.finish_order(identity,"UNKNOWN",{"error_type":"APIError","http_status":None})
    api=Mock();api.orders.return_value={"totalPages":1,"orders":[]}
    api.portfolio.return_value={"holdings":[{"symbol":"005930","quantity":24,"availableQuantity":24}]}
    start=NOW+timedelta(minutes=11)
    assert not reconcile_orders(store,api,now=start)
    assert not reconcile_orders(store,api,now=start+timedelta(seconds=59))
    assert reconcile_orders(store,api,now=start+timedelta(seconds=60))
    result=json.loads(store.db.execute("SELECT report FROM order_intents").fetchone()[0])
    assert result["outcome"] == "NO_SERVER_ORDER_OBSERVED" and result["not_replayed"]
    assert result["last_error"]["error_type"] == "APIError"
    assert store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    api.place_order.assert_not_called();store.db.close()


@pytest.mark.parametrize("held,available,rows",[(25,25,[]),(24,23,[]),
    (24,24,[order(quantity=3)]),(24,24,[order(status="OPEN",createdAt="2026-11-20T08:00:00+09:00")])])
def test_absence_never_clears_changed_or_reserved_holdings_or_ambiguous_history(tmp_path,held,available,rows):
    from datetime import timedelta
    store,identity=pending_store(tmp_path)
    store.db.execute("UPDATE order_intents SET report=?",(json.dumps({"action":"BUY","quantity":2,"order_context":{"held_quantity":24}}),));store.db.commit()
    api=Mock();api.orders.return_value={"totalPages":1,"orders":rows}
    api.portfolio.return_value={"holdings":[{"symbol":"005930","quantity":held,"availableQuantity":available}]}
    assert not reconcile_orders(store,api,now=NOW+timedelta(hours=1))
    assert not reconcile_orders(store,api,now=NOW+timedelta(hours=2))
    api.place_order.assert_not_called();store.db.close()


def test_late_order_appearing_after_absence_observation_is_reconciled_as_fill(tmp_path):
    from datetime import timedelta
    store,identity=pending_store(tmp_path)
    store.db.execute("UPDATE order_intents SET report=?",(json.dumps({"action":"BUY","quantity":2,"order_context":{"held_quantity":24}}),));store.db.commit()
    api=Mock();api.orders.return_value={"totalPages":1,"orders":[]}
    api.portfolio.return_value={"holdings":[{"symbol":"005930","quantity":24,"availableQuantity":24}]}
    assert not reconcile_orders(store,api,now=NOW+timedelta(hours=1))
    api.orders.return_value={"totalPages":1,"orders":[order()]}
    assert reconcile_orders(store,api,now=NOW+timedelta(hours=1,minutes=1))
    assert store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    api.place_order.assert_not_called();store.db.close()


def test_shortened_server_history_cannot_confirm_absence(tmp_path):
    from datetime import timedelta
    store,_=pending_store(tmp_path)
    store.order("000660","BUY",1,100,"FILLED",{"orderId":99})
    api=Mock();api.orders.return_value={"totalPages":1,"orders":[]}
    assert not reconcile_orders(store,api,now=NOW+timedelta(days=1))
    api.portfolio.assert_not_called();store.db.close()
