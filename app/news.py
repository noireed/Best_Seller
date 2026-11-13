# 개발 담당: 최준혁
"""Read-only news provider adapters and an explicitly rule-based NLP baseline."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

import requests

UTC = timezone.utc


class NewsError(RuntimeError):
    def __init__(self, message, *, status=None, retry_after=None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def clean_text(value):
    if not isinstance(value, str):
        raise ValueError("text must be a string")
    parser = _Text()
    parser.feed(value)
    return re.sub(r"\s+", " ", unescape("".join(parser.parts))).strip()


def parse_time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        result = parsedate_to_datetime(value)
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result.astimezone(UTC)


@dataclass(frozen=True)
class Article:
    title: str
    description: str
    url: str
    published_at: datetime

    @classmethod
    def from_item(cls, item):
        url = item.get("originallink") or item.get("link", "")
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("invalid article URL")
        title = clean_text(item["title"])[:500]
        if not title:
            raise ValueError("empty title")
        return cls(title, clean_text(item.get("description", ""))[:2000],
                   urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")),
                   parse_time(item["pubDate"]))


class NaverNewsClient:
    provider = "naver"
    delay_hours = 0
    ENDPOINT = "https://naverapihub.apigw.ntruss.com/search/v1/news"

    def __init__(self, client_id, client_secret, *, session=None):
        self.client_id, self.client_secret = client_id, client_secret
        self.session = session or requests.Session()

    def search(self, query, display=20):
        if not self.client_id or not self.client_secret:
            raise NewsError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 미설정")
        if not query.strip() or not 1 <= display <= 100:
            raise NewsError("invalid query or display")
        try:
            response = self.session.get(
                self.ENDPOINT, params={"query": query, "display": display, "sort": "date"},
                headers={"X-NCP-APIGW-API-KEY-ID": self.client_id,
                         "X-NCP-APIGW-API-KEY": self.client_secret}, timeout=10)
            if response.status_code != 200:
                # Never include response bodies/headers containing credentials in logs.
                raise NewsError(f"뉴스 API HTTP {response.status_code}")
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise NewsError("invalid news response schema")
            return data["items"]
        except (requests.RequestException, ValueError) as exc:
            raise NewsError("뉴스 API 통신 또는 JSON 오류") from exc


class GNewsClient:
    provider = "gnews"
    ENDPOINT = "https://gnews.io/api/v4/search"

    def __init__(self, api_key, *, delay_hours=12, session=None, timer=None, sleep=None):
        self.api_key, self.delay_hours = api_key, delay_hours
        self.session = session or requests.Session()
        self.timer, self.sleep = timer or time.monotonic, sleep or time.sleep
        self.last_call = None

    def search(self, query, display=10):
        if not self.api_key:
            raise NewsError("GNEWS_API_KEY 미설정")
        if not query.strip() or not 1 <= display <= 100:
            raise NewsError("invalid query or display")
        if self.last_call is not None:
            wait = 2.1 - (self.timer() - self.last_call)
            if wait > 0:
                self.sleep(wait)
        self.last_call = self.timer()
        try:
            search_term = f'"{query}"' if "&" in query and '"' not in query else query
            response = self.session.get(self.ENDPOINT,
                params={"q": search_term, "lang": "en", "max": min(display, 10),
                        "sortby": "publishedAt", "apikey": self.api_key}, timeout=10)
            if response.status_code != 200:
                raise NewsError(f"GNews API HTTP {response.status_code}", status=response.status_code,
                                retry_after=response.headers.get("Retry-After"))
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("articles"), list):
                raise NewsError("invalid GNews response schema")
            # Normalize provider fields. A malformed row is filtered by NewsService.
            return [{"title": a.get("title"), "description": a.get("description") or "",
                     "originallink": a.get("url"), "pubDate": a.get("publishedAt")}
                    if isinstance(a, dict) else {} for a in data["articles"]]
        except (requests.RequestException, ValueError) as exc:
            # Exception strings can contain the API key in the request URL.
            raise NewsError("GNews API 통신 또는 JSON 오류") from exc


POSITIVE = ("실적 개선", "흑자 전환", "흑자전환", "사상 최대", "수주 확대", "배당 확대",
            "record profit", "profit growth", "dividend increase")
NEGATIVE = ("실적 악화", "적자 전환", "적자전환", "영업손실", "계약 해지", "리콜",
            "profit warning", "net loss", "recall", "contract termination")
EVENTS = {
    "earnings": ("실적", "영업이익", "영업손실", "흑자", "적자", "profit", "earnings", "net loss"),
    "contract": ("수주", "공급계약", "계약 해지", "contract"),
    "legal": ("소송", "과징금", "수사", "lawsuit", "antitrust"),
    "shareholder_return": ("배당", "자사주", "dividend", "buyback"),
    "product": ("출시", "신제품", "리콜", "launch", "recall"),
}


def analyze_article(article, aliases):
    """Require the company in the title. Ambiguous polarity abstains.

    This lexical baseline is NOT a trained sentiment model or causal attribution.
    All evidence is an exact substring of the title/search snippet.
    """
    if not any(alias.casefold() in article.title.casefold() for alias in aliases):
        return None
    if "Samsung Electronics" in aliases and re.search(r"Samsung\s+(Display|SDI|Biologics|Heavy|Life|Fire)|Samsung Electronics preferred", article.title, re.I):
        return None
    text = article.title + " " + article.description
    # Preserve exact source spelling in evidence, including English capitalization.
    positive = [m.group() for word in POSITIVE for m in re.finditer(re.escape(word), text, re.IGNORECASE)][:20]
    negative = [m.group() for word in NEGATIVE for m in re.finditer(re.escape(word), text, re.IGNORECASE)][:20]
    uncertain = any(word in text.casefold() for word in ("전망", "예상", "가능성", "루머", "부인", "아니다", "않", "없",
                                                         "forecast", "expected", "rumor", "denies", "not "))
    score = 0 if uncertain or bool(positive) == bool(negative) else (1 if positive else -1)
    return {
        "title": article.title, "description": article.description, "url": article.url,
        "published_at": article.published_at.isoformat(),
        "sentiment": "positive" if score > 0 else "negative" if score < 0 else "neutral",
        "score": score, "events": [key for key, terms in EVENTS.items() if any(t in text.casefold() for t in terms)],
        "evidence": list(dict.fromkeys(positive + negative)),
        "uncertain": uncertain, "method": "lexicon_baseline_v1",
    }


def load_aliases(raw):
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("NEWS_SYMBOL_ALIASES must be a JSON object")
    for symbol, aliases in data.items():
        if not re.fullmatch(r"\d{6}", symbol) or not isinstance(aliases, list) or not aliases:
            raise ValueError("each stock needs a six-digit symbol and aliases")
        if any(not isinstance(a, str) or len(a.strip()) < 2 for a in aliases):
            raise ValueError("aliases must be strings of at least two characters")
        data[symbol] = [a.strip() for a in aliases]
    return data


class NewsService:
    def __init__(self, client, aliases, *, max_age_hours=48, cache_seconds=900,
                 display=20, clock=None, timer=None):
        self.client, self.aliases = client, aliases
        self.max_age_hours, self.cache_seconds, self.display = max_age_hours, cache_seconds, display
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timer = timer or time.monotonic
        self.cache = {}

    def collect(self, symbol):
        now = self.clock().astimezone(UTC)
        result = {"symbol": symbol, "status": "unavailable", "as_of": now.isoformat(),
                  "articles": [], "score": 0.0, "method": "lexicon_baseline_v1",
                  "filtered": 0, "cache_hit": False, "received_count": 0, "filter_reasons": {}}
        result["provider"] = getattr(type(self.client), "provider", "custom")
        result["delay_hours"] = getattr(self.client, "delay_hours", 0)
        if isinstance(self.client, NaverNewsClient):
            result["reason"] = "NAVER_SEARCH_ANALYSIS_RESTRICTED: 현행 API HUB 약관상 분석 경로 제외"
            return result
        aliases = self.aliases.get(symbol)
        if not aliases:
            result["reason"] = "종목명 별칭 미등록"
            return result
        cached = self.cache.get(symbol)
        try:
            if cached and self.timer() - cached[0] < self.cache_seconds:
                items = cached[1]
                observed_at = cached[2]
                result["cache_hit"] = True
            else:
                items = self.client.search(aliases[0], self.display)
                observed_at = getattr(self.client, "last_observed_at", None) or self.clock().astimezone(UTC).isoformat()
                result["cache_hit"] = bool(getattr(self.client, "last_cache_hit", False))
                if result["cache_hit"]:
                    # Do not extend a nearly-expired persistent cache by another memory TTL.
                    self.cache.pop(symbol, None)
                else:
                    self.cache[symbol] = (self.timer(), items, observed_at)
        except NewsError as exc:
            result["reason"] = str(exc)
            return result
        seen_urls, seen_titles = set(), set()
        result["received_count"] = len(items)
        for item in items:
            try:
                article = Article.from_item(item)
                age = now - article.published_at
                if age < timedelta(0):
                    raise ValueError("future_news")
                if age > timedelta(hours=self.max_age_hours):
                    raise ValueError("stale_news")
                title_key = re.sub(r"\W", "", article.title).casefold()
                if article.url in seen_urls or title_key in seen_titles:
                    raise ValueError("duplicate")
                analyzed = analyze_article(article, aliases)
                if analyzed is None:
                    raise ValueError("company absent from title")
                seen_urls.add(article.url)
                seen_titles.add(title_key)
                analyzed["provider"] = result["provider"]
                analyzed["delay_hours"] = result["delay_hours"]
                analyzed["symbol"] = symbol
                analyzed["observed_at"] = observed_at
                result["articles"].append(analyzed)
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
                result["filtered"] += 1
                reason = str(exc) if str(exc) in ("future_news", "stale_news", "duplicate", "company absent from title") else "invalid_article"
                result["filter_reasons"][reason] = result["filter_reasons"].get(reason, 0) + 1
        articles = sorted(result["articles"], key=lambda a: a["published_at"], reverse=True)
        result["articles"] = articles
        result["status"] = "ok" if articles else "empty"
        result["score"] = sum(a["score"] for a in articles) / len(articles) if articles else 0.0
        return result


class MultiNewsService:
    """Independent provider caches; a failed provider does not erase successful news."""
    def __init__(self, services):
        self.services = services

    def collect(self, symbol):
        reports = [service.collect(symbol) for service in self.services]
        articles, urls, titles = [], set(), set()
        duplicates = 0
        for report in reports:
            for article in report["articles"]:
                title = re.sub(r"\W", "", article["title"]).casefold()
                if article["url"] in urls or title in titles:
                    duplicates += 1
                    continue
                urls.add(article["url"])
                titles.add(title)
                articles.append(article)
        failed = any(r["status"] == "unavailable" for r in reports)
        return {"symbol": symbol, "as_of": reports[0]["as_of"],
                "status": "ok" if articles else "unavailable" if failed else "empty",
                "partial_failure": failed and any(r["status"] != "unavailable" for r in reports),
                "articles": sorted(articles, key=lambda a: a["published_at"], reverse=True),
                "score": sum(a["score"] for a in articles) / len(articles) if articles else 0.0,
                "filtered": sum(r["filtered"] for r in reports) + duplicates,
                "cache_hit": all(r["cache_hit"] for r in reports), "method": "lexicon_baseline_v1",
                "providers": [{k: r[k] for k in ("provider", "status", "delay_hours", "cache_hit")}
                              | {"received_count": r.get("received_count", 0), "filtered": r.get("filtered", 0), "filter_reasons": r.get("filter_reasons", {})}
                              | ({"reason": r["reason"]} if "reason" in r else {}) for r in reports]}


def build_news_service(settings):
    from .news_transport import DurableNewsClient
    providers = list(dict.fromkeys(p.strip() for p in settings.news_providers.split(",") if p.strip()))
    if not providers or any(p not in ("naver", "gnews") for p in providers):
        raise ValueError("NEWS_PROVIDERS must be naver, gnews, or naver,gnews")
    services = []
    for provider in providers:
        client = (NaverNewsClient(settings.naver_client_id, settings.naver_client_secret) if provider == "naver"
                  else GNewsClient(settings.gnews_api_key, delay_hours=settings.gnews_delay_hours))
        if provider == "gnews":
            client = DurableNewsClient(client, settings.news_api_db_path,
                        daily_limit=settings.gnews_daily_budget, ttl_seconds=settings.gnews_cache_seconds)
        aliases = settings.news_symbol_aliases if provider == "naver" else settings.gnews_symbol_aliases
        services.append(NewsService(client, load_aliases(aliases),
                        max_age_hours=settings.news_max_age_hours,
                        cache_seconds=settings.news_cache_seconds if provider == "naver" else settings.gnews_cache_seconds,
                        display=settings.news_display))
    return MultiNewsService(services)
