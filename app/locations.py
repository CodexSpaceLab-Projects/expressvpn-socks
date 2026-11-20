import json
import re
import unicodedata
from functools import cache
from pathlib import Path

SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)
ISO_DATA = Path("/usr/share/iso-codes/json/iso_3166-1.json")

# The continent filters follow the groups in ExpressVPN's location catalog.
AREAS = {
    "americas": "AR BS BM BO BR CA KY CL CO CR CU DO EC GT GL HN JM MX PA PE PR TT US UY VE",
    "europe": "AL AD AM AT AZ BY BE BA BG HR CY CZ DK EE FI FR GE DE GR HU IS IE IM IT JE LV LI LT LU MT MD MC ME NL MK NO PL PT RO RS SK SI ES SE CH TR UA GB",
    "asia-pacific": "AU BD BT BN KH CN GU HK IN ID JP KZ LA MO MY MN MM NP NZ PK PH SG KR LK TW TH UZ VN",
    "middle-east-africa": "DZ EG GH IL KE LB MA NG SA ZA QA AE",
}

ALIASES = {
    "bosnia": "BA", "burma": "MM", "czech-republic": "CZ", "hongkong": "HK",
    "republic-of-korea": "KR", "south-korea": "KR", "korea": "KR", "macau": "MO",
    "moldova": "MD", "north-macedonia": "MK", "taiwan": "TW", "turkey": "TR",
    "uae": "AE", "uk": "GB", "united-kingdom": "GB", "united-states": "US",
    "usa": "US", "viet-nam": "VN", "vietnam": "VN",
}


def slugify(name):
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return "-".join(SLUG.findall(ascii_name.casefold()))


@cache
def country_prefixes():
    catalog = json.loads(ISO_DATA.read_text())["3166-1"]
    prefixes = {}
    for country in catalog:
        code = country["alpha_2"]
        prefixes[code.casefold()] = code
        for field in ("name", "common_name", "official_name"):
            if field in country:
                prefixes[slugify(country[field])] = code
    prefixes.update(ALIASES)
    return sorted(prefixes.items(), key=lambda entry: -len(entry[0]))


def region_list(output):
    areas = {code: area for area, countries in AREAS.items() for code in countries.split()}
    regions = []
    for slug in dict.fromkeys(line.strip() for line in output.splitlines()):
        if not SLUG.fullmatch(slug) or slug in {"smart", "unknown"}:
            continue
        country = next((code for prefix, code in country_prefixes()
                        if slug == prefix or slug.startswith(prefix + "-")), None)
        regions.append({"id": slug, "country": country, "area": areas.get(country, "other")})
    return regions
