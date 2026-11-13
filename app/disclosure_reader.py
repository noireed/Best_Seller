# 개발 담당: 최준혁
"""Bounded DART original-document retrieval parsing, with quoted source paragraphs."""
import hashlib
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser


class _Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.active, self.parts, self.length = [], None, [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"title", "subtitle", "p"} and self.active is None:
            self.active, self.parts, self.length = tag, [], 0

    def handle_data(self, data):
        if self.active and self.length <= 4000:
            self.parts.append(data)
            self.length += len(data)

    def handle_endtag(self, tag):
        if tag == self.active:
            self.blocks.append((tag.upper(), "".join(self.parts)))
            self.active, self.parts = None, []

TOPICS = {"business": ("사업의 내용", "주요 제품", "주요제품", "매출 및 수주"),
          "risk": ("위험관리", "위험 요인", "위험요인", "시장위험", "신용위험", "유동성위험"),
          "financial_review": ("재무상태", "경영진단", "영업실적", "현금흐름")}


def parse_document(content, receipt):
    if not re.fullmatch(r"\d{14}", receipt) or len(content) > 20_000_000:
        raise ValueError("invalid_or_oversized_document")
    excerpts, seen, topic_counts, parse_modes = [], set(), {topic: 0 for topic in TOPICS}, set()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        xml_files = [info for info in archive.infolist() if info.filename.lower().endswith(".xml")]
        if not xml_files or len(xml_files) > 30 or sum(info.file_size for info in xml_files) > 80_000_000:
            raise ValueError("unsupported_document_archive")
        for info in sorted(xml_files, key=lambda f: ("_001" not in f.filename, f.filename)):
            raw = archive.read(info)
            # No external entities or entity expansion from document payloads.
            if b"<!ENTITY" in raw.upper():
                raise ValueError("unsupported_xml_entities")
            try:
                root = ET.fromstring(raw)
                blocks = [(node.tag.rsplit("}", 1)[-1].upper(), "".join(node.itertext())) for node in root.iter()
                          if node.tag.rsplit("}", 1)[-1].upper() in {"TITLE", "SUBTITLE", "P"}]
                parse_modes.add("xml")
            except ET.ParseError:
                # Actual DART reports can contain bare '&' (e.g. R&D). Parse their
                # document markup without rewriting content or resolving external entities.
                parser = _Paragraphs()
                parser.feed(raw.decode("utf-8-sig"))
                blocks = parser.blocks
                parse_modes.add("tolerant_markup")
            heading = ""
            for tag, raw_text in blocks:
                text = re.sub(r"\s+", " ", raw_text).strip()
                if tag in {"TITLE", "SUBTITLE"}:
                    heading = text[:300]
                    continue
                # Avoid huge tables and mere section headings masquerading as analysis.
                if not 30 <= len(text) <= 4000:
                    continue
                topics = [topic for topic, terms in TOPICS.items() if topic_counts[topic] < 8 and any(term in text or term in heading for term in terms)]
                if not topics or text in seen:
                    continue
                seen.add(text)
                for topic in topics:
                    topic_counts[topic] += 1
                excerpts.append({"file": info.filename, "heading": heading, "topics": topics,
                                 "text": text[:1000], "truncated": len(text) > 1000,
                                 "content_sha256": hashlib.sha256(text.encode()).hexdigest()})
                if len(excerpts) >= 24:
                    break
            if len(excerpts) >= 24:
                break
    return {"status": "ok" if excerpts else "no_supported_paragraphs", "receipt": receipt,
            "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + receipt,
            "archive_sha256": hashlib.sha256(content).hexdigest(), "excerpts": excerpts,
            "parse_modes": sorted(parse_modes),
            "limitations": "Selected issuer-authored paragraphs, not independent verification or a sentiment score. Tables are calculated through structured financial data."}
