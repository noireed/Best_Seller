from datetime import datetime, timedelta, timezone
import time
from unittest.mock import Mock

import pytest
import requests

from app.api_client import APIError, TradingAPI
from app.check_api import inspect_api


def response(data, status=200, headers=None):
    r = Mock()
    r.status_code, r.ok, r.content, r.headers = status, status < 400, b"json", headers or {}
    r.json.return_value = data
    return r


def api():
    client = TradingAPI("http://example.test", "test-team")
    client.session = Mock()
    client.token, client.token_expiry = "test-token", time.monotonic() + 10000
    client.session.request.return_value = response({})
    return client


def test_login_uses_server_expiry_and_counts_call():
    client = api()
    client.session.post.return_value = response({"accessToken": "new-token",
        "expiresAt": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()})
    client.login()
    assert 7100 < client.token_expiry - time.monotonic() < 7200
    assert len(client.last_calls) == 1
    assert client.session.post.call_args.kwargs["json"] == {"teamCode": "test-team"}


@pytest.mark.parametrize("data", [{}, {"accessToken": "x", "expiresAt": "bad"},
    {"accessToken": "x", "expiresAt": "2020-01-01T12:00:00+09:00"},
    {"accessToken": "x", "expiresAt": "2030-01-01T12:00:00"}])
def test_bad_login_response(data):
    client = api()
    client.session.post.return_value = response(data)
    with pytest.raises(APIError):
        client.login()


def test_new_read_endpoints_and_public_auth():
    client = api()
    client.stocks("삼성")
    assert client.session.request.call_args.args[:2] == ("GET", "http://example.test/api/stocks")
    assert client.session.request.call_args.kwargs["params"] == {"keyword": "삼성"}
    client.orderbook("005930")
    assert client.session.request.call_args.args[1].endswith("/005930/orderbook")
    client.leaderboard()
    assert client.session.request.call_args.args[1].endswith("/api/leaderboard")
    assert "Authorization" not in client.session.request.call_args.kwargs["headers"]


def test_before_encoded_as_query_parameter():
    client = api()
    before = "2026-04-01T09:00:00+09:00"
    client.candles("005930", count=5, before=before)
    kwargs = client.session.request.call_args.kwargs
    prepared = requests.Request("GET", "http://example.test", params=kwargs["params"]).prepare()
    assert "%2B09%3A00" in prepared.url


def test_candle_history_deduplicates_inclusive_boundary():
    client = api()
    older = {"timestamp": "2026-11-01T09:00:00+09:00", "closePrice": 10}
    newer = {"timestamp": "2026-11-02T09:00:00+09:00", "closePrice": 20}
    client.candles = Mock(side_effect=[{"candles": [newer, older], "nextBefore": older["timestamp"]},
                                       {"candles": [older], "nextBefore": None}])
    report = client.candle_history("005930")
    assert report["complete"]
    assert report["candles"] == [older, newer]


def test_candle_history_reports_truncation_and_cursor_loop():
    client = api()
    client.candles = Mock(return_value={"candles": [], "nextBefore": "2026-04-01T09:00:00+09:00"})
    assert not client.candle_history("005930", max_pages=1)["complete"]
    with pytest.raises(APIError, match="커서"):
        client.candle_history("005930", max_pages=3)


def test_order_page_and_status_contract():
    client = api()
    client.orders("PARTIAL", page=2, size=30)
    assert client.session.request.call_args.kwargs["params"] == {"page": 2, "size": 30, "status": "PARTIAL"}
    with pytest.raises(ValueError):
        client.orders("INVALID")


def test_error_categories_and_retry_after_preserved_without_body():
    client = api()
    client.session.request.return_value = response(
        {"error": {"code": "RATE_LIMIT_EXCEEDED", "message": "secret-detail"}},
        429, {"Retry-After": "12"})
    with pytest.raises(APIError) as caught:
        client.portfolio()
    exc = caught.value
    assert (exc.status, exc.code, exc.retry_after) == (429, "RATE_LIMIT_EXCEEDED", "12")
    assert "secret-detail" not in str(exc)
    assert client.session.request.call_count == 1


def test_order_timeout_is_not_replayed():
    client = api()
    client.session.request.side_effect = requests.Timeout()
    with pytest.raises(APIError):
        client.place_order("005930", "BUY", 1)
    assert client.session.request.call_count == 1


def test_401_refresh_only_once():
    client = api()
    client.session.request.side_effect = [response({}, 401), response({"teamId": 1, "teamName": "test"})]
    client.session.post.return_value = response({"accessToken": "renewed",
        "expiresAt": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()})
    assert client.me()["teamId"] == 1
    assert client.session.post.call_count == 1
    assert client.session.request.call_count == 2


def test_public_diagnostic_does_not_authenticate_or_order():
    client = Mock()
    client.leaderboard.return_value = {"updatedAt": "now", "rankings": []}
    result = inspect_api(client, ["005930"])
    assert result["authenticated_checks"] == "skipped"
    client.me.assert_not_called()
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()


def test_authenticated_diagnostic_stops_on_auth_error():
    client = Mock()
    client.leaderboard.return_value = {"updatedAt": "now", "rankings": []}
    client.me.side_effect = APIError("failed", status=401, code="INVALID_TEAM_CODE")
    result = inspect_api(client, ["005930"], authenticated=True)
    assert result["authenticated_checks"] == "stopped_after_auth_error"
    client.portfolio.assert_not_called()
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()


def test_authenticated_diagnostic_visits_documented_reads_only():
    client = Mock()
    client.leaderboard.return_value = {"updatedAt": "now", "rankings": []}
    client.me.return_value = {"teamId": 1, "teamName": "test"}
    client.stocks.return_value = {"stocks": [{"symbol": "005930", "name": "삼성전자"}]}
    client.prices.return_value = {"result": [{"symbol": "005930", "price": 72000}]}
    client.portfolio.return_value = {"holdings": [], "availableCash": 100000000, "totalAssetValue": 100000000}
    client.limits.return_value = {"remainingTrades": 30, "cooldowns": []}
    client.orders.return_value = {"orders": [], "totalPages": 0}
    client.orderbook.return_value = {"asks": [{"price": 72100, "volume": 8500}], "bids": []}
    client.candles.return_value = {"candles": [], "nextBefore": None}
    report = inspect_api(client, ["005930"], authenticated=True)
    assert len(report["checks"]) == 9
    assert all(c["status"] == "ok" for c in report["checks"])
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()
