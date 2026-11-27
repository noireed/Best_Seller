from datetime import datetime,timedelta
from email.utils import format_datetime
import html
import json
from unittest.mock import Mock

from app.storage import Store
from app.config import Settings
from app.gemini_client import GeminiClient
from app.free_research import FreeResearch

NOW=datetime.fromisoformat("2026-11-27T02:00:00+00:00")
TITLE="유한양행 3분기 영업이익 50.8% 증가 공식 발표"


def item(title=TITLE,publisher="One",domain="one.example",published=NOW):
    return '<item><title>'+html.escape(title+' - '+publisher)+'</title><link>https://news.google.com/rss/articles/'+publisher+'</link><source url="https://'+domain+'">'+publisher+'</source><pubDate>'+format_datetime(published)+'</pubDate></item>'


def session_for(content):
    session=Mock();session.get.return_value.status_code=200
    session.get.return_value.content=('<rss><channel>'+content+'</channel></rss>').encode()
    return session


def test_free_rss_binds_original_headlines_to_distinct_publishers_and_caches(tmp_path):
    session=session_for(item()+item(publisher="Two",domain="two.example"))
    path=str(tmp_path/"db");store=Store(path)
    result=FreeResearch(store,session=session).search("000100",["유한양행"],NOW)
    assert result["status"] == "ok" and len(result["sources"]) == 2
    assert result["claims"][0]["text"] == TITLE and result["claims"][0]["source_ids"] == [0,1]
    store.db.close();store=Store(path)
    cached=FreeResearch(store,session=session).search("000100",["유한양행"],NOW+timedelta(minutes=1))
    assert cached["cached"] and cached["observed_at"] == NOW.isoformat()
    session.get.assert_called_once();store.db.close()


def test_free_rss_excludes_old_future_other_issuer_and_duplicate_publisher(tmp_path):
    feed=item()+item()+item(publisher="Old",published=NOW-timedelta(days=3))+item(publisher="Future",published=NOW+timedelta(days=1))+item(title="경쟁사 삼성전자 영업이익 50% 증가 공식 발표",publisher="Other")
    store=Store(str(tmp_path/"db"));r=FreeResearch(store,session=session_for(feed)).search("000100",["유한양행"],NOW)
    assert len(r["sources"]) == 1;store.db.close()


def test_short_parent_alias_does_not_capture_subsidiary(tmp_path):
    store=Store(str(tmp_path/"db"))
    feed=item(title="LG전자 최신 가전제품으로 해외 매출 30% 증가")
    r=FreeResearch(store,session=session_for(feed)).search("003550",["LG Corp"],NOW)
    assert r["status"] == "no_fresh_news";store.db.close()


def test_bad_feed_date_does_not_discard_valid_items(tmp_path):
    store=Store(str(tmp_path/"db"));feed=item().replace(format_datetime(NOW),"not a date")+item(publisher="Valid")
    r=FreeResearch(store,session=session_for(feed)).search("000100",["유한양행"],NOW)
    assert r["status"] == "ok" and len(r["sources"]) == 1;store.db.close()


def test_developer_backend_never_calls_search_tools_or_vertex(tmp_path):
    store=Store(str(tmp_path/"db"));session=Mock()
    client=GeminiClient(Settings(_env_file=None,gemini_api_key="test"),store,session=session)
    assert client.generate("discovery",{"tools":[{"googleSearch":{}}]},NOW)["status"] == "paid_tools_disabled"
    session.post.assert_not_called();store.db.close()


def test_free_backend_throttles_and_only_counts_attempted_calls_after_clean_completion(tmp_path):
    store=Store(str(tmp_path/"db"));session=Mock();session.post.return_value.status_code=200
    session.post.return_value.json.return_value={"candidates":[{"content":{"parts":[{"text":"OK"}]}}]}
    clock=Mock(side_effect=[100,100.5,112]);sleep=Mock()
    s=Settings(_env_file=None,gemini_api_key="test",gemini_min_interval_seconds=12)
    client=GeminiClient(s,store,session=session,clock=clock,sleep=sleep)
    client.generate("one",{},NOW);client.generate("two",{},NOW)
    assert session.post.call_count == 2
    assert all("generativelanguage.googleapis.com" in call.args[0] for call in session.post.call_args_list)
    sleep.assert_called_once_with(11.5)
    client.release_reservations()
    assert store.db.execute("SELECT used FROM gemini_usage").fetchone()[0] == 2
    store.db.close()


def test_old_vertex_reservations_do_not_exhaust_developer_budget(tmp_path):
    store=Store(str(tmp_path/"db"));session=Mock();session.post.return_value.status_code=200
    session.post.return_value.json.return_value={"candidates":[{"content":{"parts":[{"text":"OK"}]}}]}
    paid=GeminiClient(Settings(_env_file=None,gemini_api_key="test",gemini_backend="vertex",gemini_daily_budget=2),store,session=session)
    paid.generate("one",{},NOW)
    free=GeminiClient(Settings(_env_file=None,gemini_api_key="test",gemini_backend="developer",gemini_daily_budget=2),store,session=session)
    assert free.generate("one",{},NOW)["status"] == "ok"
    assert paid.account != free.account and session.post.call_count == 2
    store.db.close()
