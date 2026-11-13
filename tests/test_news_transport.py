from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from unittest.mock import Mock

import pytest

from app.news import NewsError, NewsService
from app.news_transport import DurableNewsClient

NOW = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = NOW
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


def raw_client():
    client = Mock()
    client.api_key, client.delay_hours = "secret-not-for-db", 12
    client.search.return_value = [{"title": "Samsung Electronics record profit", "description": "",
        "originallink": "https://example.org/a", "pubDate": "2026-11-02T12:00:00Z"}]
    return client


def durable(tmp_path, client=None, clock=None, **kwargs):
    clock = clock or Clock()
    return DurableNewsClient(client or raw_client(), tmp_path / "api.db", clock=clock, sleep=clock.sleep, **kwargs)


def test_cache_survives_new_process_instance_and_preserves_observation_time(tmp_path):
    clock, client = Clock(), raw_client()
    first = durable(tmp_path, client, clock)
    expected = first.search("Samsung")
    observed = first.last_observed_at
    clock.sleep(120)
    other_client = raw_client()
    second = durable(tmp_path, other_client, clock)
    assert second.search("Samsung") == expected
    assert second.last_cache_hit and second.last_observed_at == observed
    other_client.search.assert_not_called()
    assert second.health()["local_attempts"] == 1
    service = NewsService(second, {"005930": ["Samsung"]}, clock=clock)
    report = service.collect("005930")
    assert report["cache_hit"] and report["articles"][0]["observed_at"] == observed
    assert report["delay_hours"] == 12


def test_expired_cache_never_reused_after_failure(tmp_path):
    clock, client = Clock(), raw_client()
    wrapper = durable(tmp_path, client, clock, ttl_seconds=60)
    wrapper.search("Samsung")
    clock.sleep(61)
    client.search.side_effect = NewsError("failed", status=503)
    with pytest.raises(NewsError):
        wrapper.search("Samsung")
    assert not wrapper.last_cache_hit
    assert wrapper.health()["local_attempts"] == 2
    with pytest.raises(NewsError, match="COOLDOWN"):
        wrapper.search("Samsung")
    assert client.search.call_count == 2


def test_daily_budget_and_utc_day_reset(tmp_path):
    clock = Clock()
    wrapper = durable(tmp_path, clock=clock, daily_limit=1)
    wrapper.search("Samsung")
    with pytest.raises(NewsError, match="DAILY_BUDGET"):
        wrapper.search("NAVER")
    clock.now = datetime(2026, 11, 4, 0, tzinfo=timezone.utc)
    wrapper.search("NAVER")
    assert wrapper.health()["local_attempts"] == 1


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_error_creates_persistent_cooldown_without_retry(tmp_path, status):
    client, clock = raw_client(), Clock()
    client.search.side_effect = NewsError("url?apikey=secret-not-for-db", status=status, retry_after="120")
    wrapper = durable(tmp_path, client, clock)
    with pytest.raises(NewsError) as caught:
        wrapper.search("Samsung")
    assert "secret-not-for-db" not in str(caught.value)
    assert wrapper.health()["cooldown_active"]
    second = durable(tmp_path, clock=clock)
    with pytest.raises(NewsError, match="COOLDOWN"):
        second.search("Different query")
    assert client.search.call_count == 1
    assert wrapper.health()["local_attempts"] == 1


def test_retry_after_http_date(tmp_path):
    clock, client = Clock(), raw_client()
    client.search.side_effect = NewsError("limited", status=429, retry_after="Tue, 03 Nov 2026 03:05:00 GMT")
    wrapper = durable(tmp_path, client, clock)
    with pytest.raises(NewsError):
        wrapper.search("Samsung")
    clock.sleep(299)
    assert wrapper.health()["cooldown_active"]
    clock.sleep(2)
    assert not wrapper.health()["cooldown_active"]


def test_rejected_query_does_not_block_other_companies(tmp_path):
    client = raw_client()
    articles = client.search.return_value
    client.search.side_effect = [NewsError("invalid query", status=400), articles]
    wrapper = durable(tmp_path, client)
    with pytest.raises(NewsError, match="NEWS_QUERY_REJECTED"):
        wrapper.search("invalid")
    assert wrapper.search("Samsung") == articles
    assert not wrapper.health()["cooldown_active"]


def test_local_spacing_is_shared_across_instances(tmp_path):
    clock = Clock()
    durable(tmp_path, clock=clock).search("Samsung")
    second = durable(tmp_path, clock=clock)
    second.search("NAVER")
    assert (clock.now-NOW).total_seconds() >= 2.1


def test_atomic_budget_under_concurrent_clients(tmp_path):
    clock = Clock()
    wrappers = [durable(tmp_path, clock=clock, daily_limit=3, min_interval=0) for _ in range(8)]
    def query(pair):
        index, wrapper = pair
        try:
            wrapper.search("unique-"+str(index))
            return True
        except NewsError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(query, enumerate(wrappers)))
    assert sum(accepted) == 3
    assert wrappers[0].health()["local_attempts"] == 3


def test_keys_and_query_are_not_logged_in_request_metadata(tmp_path):
    wrapper = durable(tmp_path)
    wrapper.search("sensitive-query-name")
    database_bytes = (tmp_path/"api.db").read_bytes()
    assert b"secret-not-for-db" not in database_bytes
    assert b"sensitive-query-name" not in database_bytes
    assert "account" not in wrapper.health()


def test_corrupt_cache_does_not_trigger_network(tmp_path):
    wrapper = durable(tmp_path)
    wrapper.search("Samsung")
    with sqlite3.connect(tmp_path/"api.db") as db:
        db.execute("UPDATE news_cache SET payload='broken'")
    wrapper.client.search.reset_mock()
    with pytest.raises(NewsError, match="CACHE_UNREADABLE"):
        wrapper.search("Samsung")
    wrapper.client.search.assert_not_called()


def test_memory_layer_does_not_extend_old_persistent_cache_ttl(tmp_path):
    clock = Clock()
    first = durable(tmp_path, clock=clock, ttl_seconds=3600)
    first.search("Samsung")
    clock.sleep(3590)
    fresh_client = raw_client()
    second = durable(tmp_path, fresh_client, clock, ttl_seconds=3600)
    service = NewsService(second, {"005930":["Samsung"]}, cache_seconds=3600,
                          clock=clock, timer=lambda:clock.now.timestamp())
    assert service.collect("005930")["cache_hit"]
    clock.sleep(11)
    assert not service.collect("005930")["cache_hit"]
    fresh_client.search.assert_called_once()
