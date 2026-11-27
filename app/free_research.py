# 개발 담당: 최준혁
"""Keyless public RSS metadata, with original titles and publisher attribution."""
from email.utils import parsedate_to_datetime
import html
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

import requests

from .news import parse_time
from .research_universe import COMPANIES, KOREAN_COMPANIES


def publisher_group(host):
    labels = host.lower().removeprefix("www.").split(".")
    return ".".join(labels[-3:] if ".".join(labels[-2:]) in ("co.kr", "or.kr", "go.kr", "co.uk", "co.jp") else labels[-2:])


class FreeResearch:
    def __init__(self, store, *, ttl=900, session=None):
        self.store, self.ttl = store, ttl
        self.session = session or requests.Session()
        store.db.execute("CREATE TABLE IF NOT EXISTS rss_research_cache(symbol TEXT PRIMARY KEY, at TEXT, report TEXT)")
        store.db.commit()

    def search(self, symbol, aliases, now):
        row = self.store.db.execute("SELECT at,report FROM rss_research_cache WHERE symbol=?", (symbol,)).fetchone()
        if row and 0 <= (now-parse_time(row[0])).total_seconds() < self.ttl:
            return {**json.loads(row[1]), "cached": True}
        names = list(dict.fromkeys([KOREAN_COMPANIES.get(symbol, symbol), COMPANIES.get(symbol, symbol)] + aliases))
        report = {"symbol": symbol, "status": "no_fresh_news", "news_status": "no_fresh_news",
                  "provider": "google_news_rss", "observed_at": now.isoformat(), "sources": [], "claims": [],
                  "aliases": names, "text": "", "limitations": "Public RSS titles and publisher metadata only; not full articles or Google Search grounding. Dates refer to feed publication, not verified event dates."}
        try:
            response = self.session.get("https://news.google.com/rss/search",
                params={"q": '"' + names[0] + '" when:2d', "hl": "ko", "gl": "KR", "ceid": "KR:ko"}, timeout=(3, 10))
            if response.status_code != 200:
                report.update(status="rss_http_error", news_status="rss_http_error", http_status=response.status_code)
            else:
                raw = response.content
                if len(raw) > 2_000_000 or b"<!ENTITY" in raw.upper():
                    raise ValueError("unsafe_feed")
                root = ET.fromstring(raw)
                seen, texts, claims_by_title = set(), [], {}
                for item in root.findall("./channel/item"):
                    title = html.unescape(re.sub(r"<[^>]+>", "", item.findtext("title", ""))).strip()
                    publisher = item.find("source")
                    domain = urlsplit(publisher.get("url", "") if publisher is not None else "").hostname
                    url = item.findtext("link", "")
                    try:
                        published = parsedate_to_datetime(item.findtext("pubDate", ""))
                    except (ValueError, TypeError, OverflowError):
                        continue
                    suffix = " - " + (publisher.text or "") if publisher is not None else ""
                    if suffix and title.endswith(suffix):
                        title = title[:-len(suffix)].strip()
                    relevant = any((re.search(r"(?<![\w&])" + re.escape(name) + r"(?![\w&])", title, re.I)
                                    if len(name) <= 2 else name.casefold() in title.casefold()) for name in names)
                    group = publisher_group(domain) if domain else ""
                    if (not published.tzinfo or not 0 <= (now-published).total_seconds() <= 172800
                            or not 20 <= len(title) <= 1500 or not domain
                            or not relevant or urlsplit(url).scheme != "https" or (title,group) in seen):
                        continue
                    seen.add((title,group))
                    source_id = len(report["sources"])
                    report["sources"].append({"id": source_id, "url": url, "domain": domain,
                        "source_group": publisher_group(domain), "title": title,
                        "published_at": published.isoformat(), "publisher_url": publisher.get("url"), "provider": "google_news_rss"})
                    if title in claims_by_title:
                        claims_by_title[title]["source_ids"].append(source_id)
                    else:
                        claim = {"id": len(report["claims"]), "text": title,
                                 "source_ids": [source_id], "published_at": published.isoformat()}
                        report["claims"].append(claim)
                        claims_by_title[title] = claim
                        texts.append(title)
                    if len(texts) >= 8 or len(report["sources"]) >= 16:
                        break
                report["text"] = "\n".join(texts)
                if texts:
                    report.update(status="ok", news_status="ok")
        except (requests.RequestException, ValueError, TypeError, ET.ParseError, OverflowError):
            report.update(status="rss_transport_or_format_error", news_status="rss_transport_or_format_error")
        self.store.db.execute("INSERT OR REPLACE INTO rss_research_cache VALUES(?,?,?)",
                             (symbol, now.isoformat(), json.dumps(report, ensure_ascii=False, allow_nan=False)))
        self.store.db.commit()
        return report
