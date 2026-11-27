"""Company names used for GNews searches in the simulation watchlist."""
import json

from .news import load_aliases

COMPANIES = {
    "005930": "Samsung Electronics", "000660": "SK hynix", "035420": "NAVER",
    "035720": "Kakao Corp", "005380": "Hyundai Motor", "000270": "Kia Corp",
    "005490": "POSCO Holdings", "051910": "LG Chem", "006400": "Samsung SDI",
    "373220": "LG Energy Solution", "207940": "Samsung Biologics", "068270": "Celltrion",
    "105560": "KB Financial Group", "055550": "Shinhan Financial Group",
    "086790": "Hana Financial Group", "012330": "Hyundai Mobis",
    "028260": "Samsung C&T", "009150": "Samsung Electro-Mechanics",
    "034020": "Doosan Enerbility", "003550": "LG Corp", "017670": "SK Telecom",
    "030200": "KT Corp", "032830": "Samsung Life Insurance",
    "000810": "Samsung Fire & Marine Insurance", "010130": "Korea Zinc",
    "010140": "Samsung Heavy Industries", "011200": "HMM Co",
    "011170": "Lotte Chemical", "010950": "S-Oil", "096770": "SK Innovation",
    "000100": "Yuhan", "128940": "Hanmi Pharmaceutical", "033780": "KT&G",
    "090430": "Amorepacific", "004020": "Hyundai Steel",
    "006800": "Mirae Asset Securities", "039490": "Kiwoom Securities",
    "071050": "Korea Investment Holdings", "018260": "Samsung SDS", "021240": "Coway",
}

KOREAN_COMPANIES = dict(zip(COMPANIES, (
    "삼성전자", "SK하이닉스", "네이버", "카카오", "현대차", "기아", "포스코홀딩스", "LG화학",
    "삼성SDI", "LG에너지솔루션", "삼성바이오로직스", "셀트리온", "KB금융", "신한지주",
    "하나금융지주", "현대모비스", "삼성물산", "삼성전기", "두산에너빌리티", "LG",
    "SK텔레콤", "KT", "삼성생명", "삼성화재", "고려아연", "삼성중공업", "HMM",
    "롯데케미칼", "에쓰오일", "SK이노베이션", "유한양행", "한미약품", "KT&G",
    "아모레퍼시픽", "현대제철", "미래에셋증권", "키움증권", "한국금융지주", "삼성SDS", "코웨이")))


def with_research_aliases(settings):
    aliases = {symbol: [name] for symbol, name in COMPANIES.items()}
    aliases.update(load_aliases(settings.gnews_symbol_aliases))
    return settings.model_copy(update={"gnews_symbol_aliases": json.dumps(aliases)})
