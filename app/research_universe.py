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


def with_research_aliases(settings):
    aliases = {symbol: [name] for symbol, name in COMPANIES.items()}
    aliases.update(load_aliases(settings.gnews_symbol_aliases))
    return settings.model_copy(update={"gnews_symbol_aliases": json.dumps(aliases)})
