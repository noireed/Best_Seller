from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
import requests

from app.config import Settings
from app.news import GNewsClient, MultiNewsService, NewsError, NewsService, build_news_service

NOW = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)


def session_with(data, status=200):
    session = Mock()
    session.get.return_value.status_code = status
    session.get.return_value.json.return_value = data
    return session


def gnews_item(title="Samsung Electronics Record Profit", url="https://example.org/a"):
    return {"title": title, "description": None, "url": url, "publishedAt": "2026-11-02T12:00:00Z"}


def test_gnews_free_contract_and_normalization():
    session = session_with({"articles": [gnews_item()]})
    client = GNewsClient("test-secret", session=session)
    items = client.search("Samsung Electronics", 20)
    params = session.get.call_args.kwargs["params"]
    assert params == {"q": "Samsung Electronics", "lang": "en", "max": 10,
                      "sortby": "publishedAt", "apikey": "test-secret"}
    assert items[0]["description"] == ""
    assert items[0]["pubDate"].endswith("Z")
    assert client.delay_hours == 12


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_gnews_errors_hide_key(status):
    client = GNewsClient("test-secret", session=session_with({}, status))
    with pytest.raises(NewsError) as exc:
        client.search("Samsung Electronics")
    assert "test-secret" not in str(exc.value)
    assert str(status) in str(exc.value)


def test_gnews_missing_key_and_timeout():
    session = Mock()
    with pytest.raises(NewsError):
        GNewsClient("", session=session).search("Samsung")
    session.get.assert_not_called()
    session.get.side_effect = requests.Timeout("url?apikey=test-secret")
    with pytest.raises(NewsError) as exc:
        GNewsClient("test-secret", session=session).search("Samsung")
    assert "test-secret" not in str(exc.value)


def test_gnews_local_spacing():
    sleep = Mock()
    client = GNewsClient("key", session=session_with({"articles": []}), timer=lambda: 0, sleep=sleep)
    client.search("Samsung")
    client.search("Samsung")
    sleep.assert_called_once_with(2.1)


def test_gnews_analysis_metadata_and_english_evidence():
    client = GNewsClient("key", session=session_with({"articles": [gnews_item()]}))
    service = NewsService(client, {"005930": ["Samsung Electronics"]}, clock=lambda: NOW)
    report = service.collect("005930")
    assert report["status"] == "ok"
    assert report["provider"] == "gnews" and report["delay_hours"] == 12
    assert report["articles"][0]["evidence"] == ["Record Profit"]
    assert report["articles"][0]["score"] == 1
    assert report["articles"][0]["delay_hours"] == 12


def test_provider_failure_is_visible_without_erasing_success():
    good, bad = Mock(), Mock()
    good.collect.return_value = {"symbol": "005930", "as_of": NOW.isoformat(), "status": "ok",
        "articles": [{"title": "Samsung record profit", "url": "https://example.org/1", "score": 1,
                      "published_at": NOW.isoformat()}], "provider": "gnews", "delay_hours": 12,
        "filtered": 0, "cache_hit": False}
    bad.collect.return_value = {"symbol": "005930", "as_of": NOW.isoformat(), "status": "unavailable",
        "articles": [], "provider": "naver", "delay_hours": 0,
        "filtered": 0, "cache_hit": False, "reason": "key missing"}
    report = MultiNewsService([bad, good]).collect("005930")
    assert report["status"] == "ok" and report["partial_failure"]
    assert len(report["articles"]) == 1
    assert report["providers"][0]["reason"] == "key missing"
    report = MultiNewsService([good, good]).collect("005930")
    assert len(report["articles"]) == 1 and report["filtered"] == 1


def test_provider_factory_separates_aliases_and_cache():
    settings = Settings(_env_file=None, news_providers="naver,gnews")
    service = build_news_service(settings)
    assert [s.client.provider for s in service.services] == ["naver", "gnews"]
    assert service.services[1].aliases["005930"] == ["Samsung Electronics", "Samsung"]
    assert service.services[1].cache_seconds == 3600
    with pytest.raises(ValueError):
        build_news_service(settings.model_copy(update={"news_providers": "bad"}))


def test_samsung_short_name_accepted_but_other_entities_excluded():
    client = GNewsClient("key", session=session_with({"articles": [
        gnews_item("Samsung chairman visits Japan"),
        gnews_item("Samsung Display Record Profit", "https://example.org/b")]}))
    svc = NewsService(client, {"005930": ["Samsung Electronics", "Samsung"]}, clock=lambda: NOW)
    r = svc.collect("005930")
    assert r["received_count"] == 2
    assert len(r["articles"]) == 1 and r["filtered"] == 1
