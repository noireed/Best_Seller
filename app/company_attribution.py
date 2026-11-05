# 개발 담당: 최준혁
"""Conservative span-based subject attribution, without ownership inference."""
import re

# Names identify mentions only; no live ownership or competitor relationship is assumed.
KNOWN_NAMES = ("Samsung Electronics", "삼성전자", "SK hynix", "SK하이닉스", "SK 하이닉스",
               "NAVER", "네이버", "Apple", "애플", "TSMC", "삼성디스플레이", "Samsung Display",
               "Samsung SDI", "삼성SDI", "Samsung Biologics", "Samsung Heavy Industries", "Samsung Life", "Samsung Fire",
               "네이버클라우드", "NAVER Cloud")
RELATION = re.compile(r"^(?:\s|[,，:'’\"-]|의|와|과|및|s\b)*(?:협력사|협력업체|공급사|공급업체|경쟁사|경쟁업체|자회사|계열사|고객사|파트너|supplier\b|partner\b|rival\b|competitor\b|subsidiary\b|customer\b)", re.I)
BEFORE_RELATION = re.compile(r"(?:supplier|partner|rival|competitor|subsidiary|customer)\s+(?:of|to|for)\s*$", re.I)


def mentions(text, names):
    found = []
    for name in sorted(set(names), key=len, reverse=True):
        pattern = re.escape(name)
        if name.isascii():
            pattern = r"(?<![A-Za-z0-9])"+pattern+r"(?![A-Za-z0-9])"
        for match in re.finditer(pattern, text, re.I):
            if any(start < match.end() and match.start() < end for start, end, _ in found):
                continue
            found.append((match.start(), match.end(), name))
    return sorted(found)


def attribute_event(clause, aliases, event_start, event_end):
    targets = mentions(clause, aliases)
    result = {"status": "unresolved", "reason": "target_subject_not_identified", "actor": None,
              "target_mentions": [{"name": name, "span": [start, end]} for start, end, name in targets]}
    if not targets:
        return result
    # Explicit related entity suffixes must not be swallowed by a short parent alias.
    all_mentions = mentions(clause, (*KNOWN_NAMES, *aliases))
    target_names = {a.casefold() for a in aliases}
    for start, end, name in all_mentions:
        if name.casefold() not in target_names and end <= event_start and any(start <= a < end for a, _, _ in targets):
            return {**result, "status": "related_company", "reason": "longer_distinct_company_name", "actor": name}
    before = [m for m in targets if m[1] <= event_start]
    if not before:
        return {**result, "status": "counterparty", "reason": "target_appears_after_event_predicate"}
    start, end, actor = before[-1]
    gap = clause[end:event_start]
    if re.match(r"\s*(?:우(?:선주)?\b|preferred\b)", gap, re.I):
        return {**result, "status": "related_company", "reason": "preferred_share_subject"}
    if RELATION.search(gap) or BEFORE_RELATION.search(clause[:start]):
        return {**result, "status": "related_company", "reason": "explicit_relation_subject", "actor": clause[start:event_start].strip()}
    if re.search(r"다른\s*(?:기업|회사|업체)|[A-Z]사\b|\b(?:Inc|Corp|Corporation|Ltd)\b", gap):
        return {**result, "status": "ambiguous", "reason": "unresolved_other_company_subject"}
    # Korean recipient/object markers or English prepositions indicate the target is an object.
    if re.match(r"\s*(?:에게|으로부터|에\s|를|을)", gap) or re.search(r"\b(?:from|with|to|for)\s*$", clause[:start], re.I):
        return {**result, "status": "counterparty", "reason": "target_object_or_recipient"}
    others = [(a, b, n) for a, b, n in all_mentions if n.casefold() not in target_names and b <= event_start]
    if any(a >= end for a, _, _ in others):
        return {**result, "status": "ambiguous", "reason": "another_company_between_target_and_event"}
    if others and re.search(r"와|과|및|\band\b|&", clause[:event_start], re.I):
        return {**result, "status": "ambiguous", "reason": "multiple_possible_subjects"}
    return {**result, "status": "direct", "reason": "target_before_event_without_competing_subject", "actor": actor}
