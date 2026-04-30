#!/usr/bin/env python3
"""
OLX Apartment Rental Scraper
Notifications: terminal + iMessage (macOS)

Requirements:
    pip install requests beautifulsoup4

Usage:
    python olx_scraper.py                     # run once
    python olx_scraper.py --interval 86400    # run once per day
    python olx_scraper.py --reset             # clear memory and start fresh
"""

import argparse
import json
import logging
import re
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Constants
REQUEST_TIMEOUT = 15          # s — timeout for requests.get()
DELAY_BETWEEN_PAGES = 2       # s — pause between result pages
DELAY_BETWEEN_REQUESTS = 1    # s — pause between detail fetches
MAX_PAGES_UNLIMITED = 999     # pages — value used when max_pages is "all"
DEFAULT_SEEN_FILE = Path.home() / ".olx_scraper_seen.json"
DEFAULT_OPENAI_MODELS = ["gpt-4o-mini", "gpt-4.1-mini", "gpt-4o"]


class Listing(TypedDict, total=False):
    id: str
    title: str
    price: int | None
    metraz: float | None
    lokalizacja: str
    data: str
    url: str
    extra_koszt: int | None
    extra_pozycje: list[str]
    ai_score: int | None
    ai_verdict: str
    ai_summary: str
    ai_strengths: list[str]
    ai_risks: list[str]
    ai_hidden_cost_risk: str
    ai_source: str

# ─────────────────────────────────────────────────────────────
#  CONFIG — adjust to your needs
# ─────────────────────────────────────────────────────────────
CONFIG = {
    # City (as used in the OLX URL, e.g. "warszawa", "krakow", "wroclaw")
    "miasto": "warszawa",

    # District ID from OLX (optional; None = whole city)
    # Built-in maps available for: Warsaw, Kraków, Wrocław, Poznań, Gdańsk,
    # Gdynia, Sopot, Łódź, Katowice, Szczecin, Białystok, Częstochowa.
    # You can also use a district name via "dzielnica": "ursynow" — it will be
    # resolved to the correct district_id automatically.
    # To find an ID manually: open OLX, select a district and check the
    # search[district_id] parameter in the URL.
    "district_id": 373,  # Ursynów (Warsaw)

    # Price filters (currency/month)
    "cena_min": 0,
    "cena_max": 4000,

    # Area (m²) — filtered locally from title/description
    "metraz_min": 0,
    "metraz_max": 50,

    # Maximum TOTAL monthly cost (rent + additional fees from the description)
    # Set to None to disable this filter
    "budzet_lacznie": 4000,

    # Number of OLX result pages to scan (each has ~36 listings)
    # Integer or "all" to scan every available page
    "max_stron": 2,

    # Phone number for iMessage notifications (+XXXXXXXXXXX)
    "imessage_numer": "+48600000000",

    # Send iMessage? (requires macOS with the Messages app signed in)
    "wyslij_imessage": False,

    # File for persisting already-seen listing IDs
    "seen_file": str(DEFAULT_SEEN_FILE),

    # Country code — must match a file in countries/{code}.json
    # "pl" = OLX Poland (olx.pl), "ua" = OLX Ukraine (olx.ua)
    "country": "pl",
}
# ─────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── Name normalisation and Polish city district map ──────────

_POL_TRANS = str.maketrans({
    'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n',
    'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
    'Ą': 'a', 'Ć': 'c', 'Ę': 'e', 'Ł': 'l', 'Ń': 'n',
    'Ó': 'o', 'Ś': 's', 'Ź': 'z', 'Ż': 'z',
})


def _normalize_name(s: str) -> str:
    """Converts a name to an ASCII lookup key: lowercase, no diacritics, spaces → hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", s.lower().translate(_POL_TRANS)).strip("-")


def _ensure_parent_dir(path: str | Path) -> Path:
    """Creates the parent directory of a file if it does not yet exist."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


# ── Multi-country support ─────────────────────────────────────

_COUNTRIES_DIR = Path(__file__).parent / "countries"


def load_country_config(code: str) -> dict:
    """Loads country config from countries/{code}.json.

    Returns the parsed dict, or an empty dict if the file does not exist.
    """
    path = _COUNTRIES_DIR / f"{code}.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def get_cities_for_country(country_cfg: dict) -> dict[str, str]:
    """Returns {city_key: display_name} for cities defined in a country config.

    Useful for populating UI dropdowns for non-PL countries.
    """
    return {
        key: data.get("display", key)
        for key, data in country_cfg.get("cities", {}).items()
    }


def _extract_json_object(text: str) -> dict[str, Any]:
    """Extracts the first complete JSON object from text.

    Local models and some integrations can wrap the JSON in extra prose.
    Rather than relying on a fragile regex we walk the text character by character
    and track curly-brace nesting depth.
    """
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON object found in model response")

    depth = 0
    in_string = False
    escaped = False

    for idx, char in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:idx + 1])

    raise ValueError("Incomplete JSON object in model response")


# District map for Polish cities → district_id on OLX (April 2026)
# Outer key = city URL slug (ASCII, no diacritics).
# Inner key = Polish display name (used in the GUI and log messages).
# Smaller cities (e.g. Bydgoszcz, Lublin) have no district filters on OLX.
CITY_DISTRICT_DISPLAY: dict[str, dict[str, int]] = {
    "warszawa": {
        "Bemowo": 367,
        "Białołęka": 365,
        "Bielany": 369,
        "Mokotów": 353,
        "Ochota": 355,
        "Praga-Południe": 381,
        "Praga-Północ": 379,
        "Rembertów": 361,
        "Targówek": 377,
        "Ursus": 371,
        "Ursynów": 373,
        "Wawer": 383,
        "Wesoła": 533,
        "Wilanów": 375,
        "Wola": 359,
        "Włochy": 357,
        "Śródmieście": 351,
        "Żoliborz": 363,
    },
    "krakow": {
        "Bieńczyce": 285,
        "Bieżanów-Prokocim": 271,
        "Bronowice": 259,
        "Czyżyny": 283,
        "Dębniki": 261,
        "Grzegórzki": 279,
        "Krowodrza": 255,
        "Mistrzejowice": 463,
        "Nowa Huta": 287,
        "Podgórze": 263,
        "Podgórze Duchackie": 269,
        "Prądnik Biały": 275,
        "Prądnik Czerwony": 277,
        "Stare Miasto": 273,
        "Swoszowice": 267,
        "Wzgórza Krzesławickie": 281,
        "Zwierzyniec": 257,
        "Łagiewniki-Borek Fałęcki": 265,
    },
    "wroclaw": {
        "Fabryczna": 393,
        "Krzyki": 391,
        "Psie Pole": 389,
        "Stabłowice": 776,
        "Stare Miasto": 385,
        "Śródmieście": 387,
    },
    "poznan": {
        "Antoninek-Zieliniec-Kobylepole": 771,
        "Chartowo": 761,
        "Dębiec": 695,
        "Grunwald": 323,
        "Górczyn": 697,
        "Jeżyce": 325,
        "Junikowo": 699,
        "Komandoria": 763,
        "Naramowice": 705,
        "Ogrody": 707,
        "Piątkowo": 709,
        "Podolany": 711,
        "Rataje": 713,
        "Sołacz": 767,
        "Stare Miasto": 327,
        "Starołęka": 717,
        "Strzeszyn": 719,
        "Szczepankowo": 721,
        "Warszawskie": 723,
        "Wilda": 331,
        "Winiary": 725,
        "Winogrady": 727,
        "Zawady": 778,
        "Łacina": 765,
        "Ławica": 701,
        "Łazarz": 703,
        "Śródka": 769,
    },
    "gdansk": {
        "Aniołki": 97,
        "Brzeźno": 113,
        "Brętowo": 427,
        "Chełm z dzielnicą Gdańsk Południe": 93,
        "Jasień": 691,
        "Kokoszki": 423,
        "Letnica": 123,
        "Matarnia": 105,
        "Młyniska": 137,
        "Nowy Port": 125,
        "Oliwa": 109,
        "Orunia - Św. Wojciech - Lipce": 91,
        "Osowa": 429,
        "Piecki-Migowo": 103,
        "Przymorze Małe": 115,
        "Przymorze Wielkie": 119,
        "Siedlce": 95,
        "Stogi z Przeróbką": 127,
        "Strzyża": 101,
        "Suchanino": 425,
        "Ujeścisko - Łostowice": 770,
        "VII Dwór": 107,
        "Wrzeszcz": 99,
        "Wyspa Sobieszewska": 499,
        "Wzgórze Mickiewicza": 421,
        "Zaspa Młyniec": 121,
        "Zaspa Rozstaje": 117,
        "Śródmieście": 135,
        "Żabianka - Wejhera - Jelitkowo - Tysiąclecia": 111,
    },
    "gdynia": {
        "Chwarzno-Wiczlino": 147,
        "Chylonia": 139,
        "Cisowa": 141,
        "Działki Leśne": 157,
        "Dąbrowa": 175,
        "Grabówek": 155,
        "Kamienna Góra": 161,
        "Karwiny": 171,
        "Leszczynki": 153,
        "Mały Kack": 167,
        "Obłuże": 151,
        "Oksywie": 145,
        "Orłowo": 169,
        "Pogórze": 149,
        "Pustki Cisowskie-Demptowo": 143,
        "Redłowo": 165,
        "Wielki Kack": 173,
        "Witomino-Leśniczówka": 177,
        "Witomino-Radiostacja": 179,
        "Wzgórze Świętego Maksymiliana": 163,
        "Śródmieście": 159,
    },
    "sopot": {
        "Centrum": 337,
        "Dolny Sopot": 339,
        "Górny Sopot": 341,
    },
    "lodz": {
        "Bałuty": 301,
        "Górna": 303,
        "Polesie": 295,
        "Widzew": 297,
        "Śródmieście": 299,
    },
    "katowice": {
        "Bogucice": 221,
        "Brynów-cz. Wsch.-Osiedle Zgrzebioka": 231,
        "Dąb": 453,
        "Dąbrówka Mała": 225,
        "Giszowiec": 229,
        "Janów-Nikiszowiec": 223,
        "Kostuchna": 455,
        "Koszutka": 217,
        "Ligota-Panewniki": 237,
        "Osiedle Paderewskiego-Muchowiec": 215,
        "Osiedle Tysiąclecia": 247,
        "Osiedle Witosa": 243,
        "Piotrowice-Ochojec": 235,
        "Podlesie": 511,
        "Szopienice-Burowiec": 227,
        "Wełnowiec-Józefowiec": 219,
        "Zawodzie": 213,
        "Załęska Hałda-Brynów cz. Zach.": 233,
        "Załęże": 245,
        "Śródmieście": 211,
    },
    "szczecin": {
        "Bukowe": 755,
        "Bukowo": 729,
        "Centrum": 731,
        "Dąbie": 733,
        "Golęcino": 757,
        "Gumieńce": 735,
        "Kijewo": 737,
        "Krzekowo": 739,
        "Majowe": 741,
        "Niebuszewo": 743,
        "Pogodno": 745,
        "Pomorzany": 747,
        "Słoneczne": 749,
        "Warszewo": 751,
        "Zdroje": 753,
        "Świerczewo": 759,
    },
    "bialystok": {
        "Antoniuk": 61,
        "Bacieczki": 55,
        "Bema": 41,
        "Białostoczek": 17,
        "Bojary": 11,
        "Centrum": 15,
        "Dojlidy": 7,
        "Dziesięciny I": 59,
        "Dziesięciny II": 65,
        "Jaroszówka": 19,
        "Kawaleryjskie": 37,
        "Leśna Dolina": 399,
        "Mickiewicza": 27,
        "Młodych": 53,
        "Nowe Miasto": 39,
        "Piaski": 35,
        "Piasta I": 23,
        "Piasta II": 13,
        "Przydworcowe": 43,
        "Sienkiewicza": 9,
        "Skorupy": 25,
        "Starosielce": 49,
        "Słoneczny Stok": 63,
        "Wygoda": 21,
        "Wysoki Stoczek": 57,
        "Zawady": 45,
        "Zielone Wzgórza": 51,
    },
    "czestochowa": {
        "Błeszno": 415,
        "Dźbów": 89,
        "Grabówka": 81,
        "Kawodrza": 665,
        "Kiedrzyn": 407,
        "Lisiniec": 71,
        "Ostatni Grosz": 553,
        "Parkitka": 75,
        "Podjasnogórska": 419,
        "Północ": 87,
        "Raków": 409,
        "Stare Miasto": 493,
        "Stradom": 579,
        "Trzech Wieszczów": 85,
        "Tysiąclecie": 417,
        "Wrzosowiak": 77,
        "Wyczerpy - Aniołów": 73,
        "Zawodzie - Dąbie": 79,
        "Śródmieście": 83,
    },
}

# ASCII-normalised lookup dict — generated automatically.
# Used to resolve district names typed by the user (e.g. "ursynow", "Nowa Huta").
CITY_DISTRICT_IDS: dict[str, dict[str, int]] = {
    city: {_normalize_name(name): did for name, did in districts.items()}
    for city, districts in CITY_DISTRICT_DISPLAY.items()
}


def get_districts_for_city(miasto: str, country_cfg: dict | None = None) -> dict[str, int]:
    """Returns {display_name: district_id} for a given city.

    For non-PL countries, districts are read from the country config file.
    Falls back to the built-in CITY_DISTRICT_DISPLAY for Poland.

    Args:
        miasto: City name or URL key (e.g. ``'warszawa'``, ``'kiev'``).
        country_cfg: Parsed country config dict (from load_country_config).

    Returns:
        Sorted dict of district names → IDs. Empty dict if none defined.
    """
    if country_cfg and "cities" in country_cfg:
        city_data = country_cfg["cities"].get(_normalize_name(miasto), {})
        districts = city_data.get("districts", {})
        if districts:
            return dict(sorted(districts.items()))
    return dict(sorted(CITY_DISTRICT_DISPLAY.get(_normalize_name(miasto), {}).items()))


# ── URL building ─────────────────────────────────────────────

def build_url(config: dict, page: int = 1, country_cfg: dict | None = None) -> str:
    """Builds an OLX search URL from config, page number, and optional country config."""
    miasto = config["miasto"].lower()

    if country_cfg:
        domain = country_cfg["domain"]
        listing_path = country_cfg["listing_path"].format(city=miasto)
        base = f"https://{domain}{listing_path}"
    else:
        base = f"https://www.olx.pl/nieruchomosci/mieszkania/wynajem/{miasto}/"

    url = (
        f"{base}"
        f"?search%5Bfilter_float_price%3Afrom%5D={config['cena_min']}"
        f"&search%5Bfilter_float_price%3Ato%5D={config['cena_max']}"
        f"&search%5Bfilter_float_m%3Afrom%5D={config.get('metraz_min', 0) or 0}"
        f"&search%5Bfilter_float_m%3Ato%5D={config.get('metraz_max', 999) or 999}"
    )

    # District filter — numeric district_id takes priority over name lookup
    district_id = config.get("district_id")
    if not district_id and config.get("dzielnica"):
        dzielnica_norm = _normalize_name(config["dzielnica"])
        # Check country config districts first (for non-PL countries)
        if country_cfg and "cities" in country_cfg:
            city_data = country_cfg["cities"].get(miasto, {})
            country_districts = {
                _normalize_name(k): v
                for k, v in city_data.get("districts", {}).items()
            }
            district_id = country_districts.get(dzielnica_norm)
        # Fall back to built-in PL district map
        if not district_id:
            district_id = CITY_DISTRICT_IDS.get(miasto, {}).get(dzielnica_norm)
        if not district_id:
            logger.warning(
                "Unknown district '%s' for city '%s' — searching the whole city. "
                "Set district_id manually if needed.",
                config["dzielnica"], miasto,
            )
    if district_id:
        url += f"&search%5Bdistrict_id%5D={district_id}"

    if page > 1:
        url += f"&page={page}"
    return url


# ── Fetching and parsing ──────────────────────────────────────

def fetch_page(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.HTTPError as e:
        logger.error("HTTP error %s for %s", e.response.status_code if e.response else "?", url)
        return None
    except requests.RequestException as e:
        logger.error("Cannot fetch page %s: %s", url, e)
        return None


def extract_id_from_url(url: str) -> str:
    """Extracts the listing ID from a URL, e.g. 'ID19HnE4' from '.../oferta/...-CID3-ID19HnE4.html'."""
    m = re.search(r"-(ID[A-Za-z0-9]+)\.html", url)
    if m:
        return m.group(1)
    return url.strip("/").split("/")[-1][-12:]


def parse_price(text: str, currency_pattern: str | None = None) -> int | None:
    """Parses a price from text.

    Supports PLN by default: '3 000 zł', '3000 zl', '3 000 PLN', '4 500 złdo negocjacji'.
    Pass currency_pattern to override for other currencies, e.g. r'грн\\.?|uah' for UAH.
    Falls back to extracting the first digit sequence if no currency match is found.
    """
    t = text.lower().replace("\xa0", " ")
    pattern = currency_pattern or r"z[łl]|pln|z?[łl]otych"
    unit = re.search(rf"(\d[\d\s]*)(?:{pattern})", t)
    if unit:
        digits = re.sub(r"[^\d]", "", unit.group(1))
        return int(digits) if digits else None
    # Fallback: take the first digit sequence — guards against phone numbers
    digits = re.sub(r"[^\d]", "", t)
    if digits:
        value = int(digits)
        return value if value <= 999_999 else None
    return None


def parse_metraz(text: str) -> float | None:
    """'65 m²' or '65,5m2' → 65.0"""
    m = re.search(r"([\d]+[,\.]?[\d]*)\s*m", text)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    return None


def parse_listings(
    soup: BeautifulSoup,
    base_url: str = "https://www.olx.pl",
    currency_hints: list[str] | None = None,
) -> list[Listing]:
    """Parses listing cards from an OLX results page.

    Args:
        soup: Parsed HTML of the results page.
        base_url: Domain base used to fix relative listing URLs (e.g. 'https://www.olx.ua').
        currency_hints: Strings that indicate a price element in the fallback path.
                        Defaults to ['zł'] for Poland.

    Current card format (April 2026):
    - Cards: <div data-cy="l-card">
    - Title: <h4>/<h6>/<h3> inside the card
    - Price: data-testid="ad-price" element, or text containing a currency hint
    - Location+date: data-testid="location-date", format "City, District - date"
    - Area: text containing 'm²' or 'm2'
    """
    _currency_hints = currency_hints or ["zł"]
    listings = []
    cards = soup.find_all(attrs={"data-cy": "l-card"})

    for card in cards:
        try:
            # ── Link and ID ──
            link_tag = card.find("a", href=True)
            if not link_tag:
                continue
            url = link_tag["href"]
            if not url.startswith("http"):
                url = base_url + url
            ad_id = extract_id_from_url(url)

            # ── Title ──
            title_tag = card.find(["h4", "h6", "h3"])
            title = title_tag.get_text(strip=True) if title_tag else link_tag.get_text(strip=True)
            if not title:
                continue

            # ── Price ──
            price_tag = card.find(attrs={"data-testid": "ad-price"})
            price = None
            if price_tag:
                price = parse_price(price_tag.get_text(strip=True))
            else:
                # Fallback: find a string that contains a currency hint and digits
                for s in card.strings:
                    if any(h in s for h in _currency_hints) and re.search(r"\d", s):
                        price = parse_price(s)
                        break

            # ── Location and date ──
            loc_tag = card.find(attrs={"data-testid": "location-date"})
            location_text = loc_tag.get_text(strip=True) if loc_tag else ""
            lokalizacja = location_text.split(" - ")[0].strip()
            data_dodania = location_text.split(" - ")[1].strip() if " - " in location_text else ""

            # ── Area ──
            # OLX shows the area as a separate element, e.g. "65 m²"
            metraz = None
            for s in card.strings:
                s = s.strip()
                if "m²" in s or "m2" in s:
                    metraz = parse_metraz(s)
                    if metraz:
                        break
            # Fallback: look in the title
            if not metraz:
                metraz = parse_metraz(title)

            listings.append({
                "id": ad_id,
                "title": title,
                "price": price,
                "metraz": metraz,
                "lokalizacja": lokalizacja,
                "data": data_dodania,
                "url": url,
            })

        except Exception as e:
            logger.debug("Skipped listing card due to error: %s", e, exc_info=True)
            continue

    return listings


# ── Listing detail page – description and extra costs ────────

def fetch_detail(url: str) -> tuple[str, int]:
    """Fetches the description text from an OLX listing detail page.

    Returns:
        (description_text, extra_fee) — description as a lowercase string
        and the amount from the "Czynsz (dodatkowo)" OLX sidebar field (0 if absent).
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Active OLX description selector (April 2026)
        desc_tag = (
            soup.find(attrs={"data-cy": "ad_description"})
            or soup.find(attrs={"itemprop": "description"})
            or soup.find(class_=re.compile(r"description|opis", re.I))
        )
        desc_text = desc_tag.get_text(" ", strip=True).lower() if desc_tag else ""

        # Structured "Czynsz (dodatkowo): NNN zł" field from the OLX sidebar
        structured_extra = 0
        for el in soup.find_all(string=re.compile(r"czynsz.*dodatkowo", re.I)):
            parent = el.find_parent()
            if parent:
                sibling_text = parent.get_text(" ", strip=True)
                m = re.search(r"(\d[\d\s]*)\s*z[\u0142l]", sibling_text)
                if m:
                    structured_extra = int(re.sub(r"\s", "", m.group(1)))
                    break
        # Fallback: search the whole page for "Czynsz" elements with an adjacent amount
        if not structured_extra:
            for el in soup.find_all(string=re.compile(r"^Czynsz", re.I)):
                parent = el.find_parent()
                if parent and parent.find_next_sibling():
                    sib = parent.find_next_sibling()
                    sib_text = sib.get_text(strip=True)
                    m = re.search(r"(\d[\d\s]*)\s*z[\u0142l]", sib_text)
                    if m:
                        structured_extra = int(re.sub(r"\s", "", m.group(1)))
                        break

        return desc_text, structured_extra
    except requests.RequestException as e:
        logger.warning("Cannot fetch listing details %s: %s", url, e)
        return "", 0


# Phrases indicating that fees ARE included in the price (→ extra = 0)
WLICZONE_PATTERNS = [
    r"wszystk[oi][em]?\s+w\s+cen[ie]",       # "wszystko w cenie" — everything included
    r"w\s+tym\s+czynsz",                       # "w tym czynsz" — rent included
    r"media\s+wliczon[ea]",                    # "media wliczone" — utilities included
    r"rachunk[i]?\s+wliczon[ea]",             # "rachunki wliczone" — bills included
    r"op[łl]aty\s+wliczon[ea]",               # "opłaty wliczone" — fees included
    r"bez\s+dodatkowych\s+op[łl]at",          # "bez dodatkowych opłat" — no extra fees
    r"czynsz\s+do\s+sp[oó][łl]dzielni\s+wliczon", # admin rent included
    r"c\.?o\.?\s+wliczon",                    # "c.o. wliczone" — central heating included
    r"cena\s+zawiera",                         # "cena zawiera" — price includes
    r"cena\s+obejmuje",                        # "cena obejmuje" — price covers
]

# Patterns that extract additional cost amounts
# We look for amounts preceded or followed by cost-related keywords
KOSZT_PATTERNS = [
    # "czynsz administracyjny 1100-1150" or "czynsz administracji 400-500 zł" (range, zł optional)
    r"czynsz\s*(?:administracyjny|administracji|do\s+sp[oó][łl]dzielni|do\s+administracji|zarz[aą]dcy?)[\s:\-–]+(\d[\d\s]{1,5})\s*(?:-|–|do)\s*(\d[\d\s]{1,5})\s*(?:z[łl])?",
    # "czynsz administracyjny: 400 zł" or "czynsz do administracji: 920 zł/1 osoba"
    r"czynsz\s*(?:administracyjny|administracji|do\s+sp[oó][łl]dzielni|do\s+administracji|zarz[aą]dcy?)[\s:\-–]+(\d[\d\s]{1,5})\s*z[łl]",
    # "zaliczka na energię/prąd/gaz: 150 zł/1 osoba" (advance payment for energy/gas)
    r"zaliczka\s+(?:na\s+)?(?:energi[ęe]|pr[aą]d|gaz|media)[\s:\-–]+(\d[\d\s]{1,4})\s*z[łl]",
    # "opłaty eksploatacyjne 350 zł" (service charges)
    r"op[łl]at[ay]\s*(?:eksploatacyjn[ae]|administracyjn[ae]|za\s+mieszkanie)?[\s:\-–]+(\d[\d\s]{1,5})\s*z[łl]",
    # "media ok. 300 zł / media ~250 zł" (utilities ~300 PLN)
    r"media[\s:\-–~ok\.]+(\d[\d\s]{1,4})\s*z[łl]",
    # "+czynsz (obecnie ok 700zł)" / "+czynsz (opłata administracyjna): ok. 700 zł"
    r"\+\s*czynsz[^0-9]{0,60}(\d[\d\s]{1,4})\s*z[łl]",
    # "czynsz (opłata administracyjna): ok. 700 zł" (no plus sign, parenthetical description)
    r"czynsz\s*\([^)]{0,40}\)\s*:?\s*(?:ok\.?\s+)?(\d[\d\s]{1,4})\s*z[łl]",
    # "+ 400 zł czynsz" / "+ 300 zł opłaty"
    r"\+\s*(\d[\d\s]{1,4})\s*z[łl]\s*(?:czynsz|op[łl]at|media|rachunk)",
    # "NNN zł za opłaty/czynsz" (e.g. "645 zł za opłaty administracyjne")
    r"(\d[\d\s]{1,5})\s*z[łl]\s*za\s*(?:op[łl]at[yę]|czynsz|media|rachunk)\w*",
    # "2400 zł + 850 zł (opłaty administracyjne...)" — otodom format: price + fees in parentheses
    r"\d[\d\s]*\s*z[łl]\s*\+\s*(\d[\d\s]{1,5})\s*z[łl]\s*\(",
    # "rachunki około 200-300 zł" (bills approx. range) → take the higher value
    r"rachunk[i]?[\s:\-–~\w\.]+(\d[\d\s]{1,4})\s*(?:-|–|do)\s*(\d[\d\s]{1,4})\s*z[łl]",
    # "rachunki 250 zł" / "+ ok 250 rachunki za media" (bills 250 PLN)
    r"rachunk[i]?[\s:\-–~\w\.]+(\d[\d\s]{1,4})\s*z[łl]",
    r"\+\s*(?:ok\.?\s+)?(\d[\d\s]{1,3})\s+rachunki?\s+za\s+media",
    # "koszty eksploatacji 450 zł" (operating costs)
    r"koszty\s+eksploatacji[\s:\-–]+(\d[\d\s]{1,4})\s*z[łl]",
    # "do tego/dodatkowo 300 zł" (additionally 300 PLN)
    r"(?:do\s+tego|dodatkowo|plus|poza\s+tym)[\s:\-–]+(\d[\d\s]{1,4})\s*z[łl]",
    # "c.o. 150 zł" (central heating 150 PLN)
    r"c\.?o\.?[\s:\-–]+(\d[\d\s]{1,4})\s*z[łl]",
    # "wywóz śmieci 50 zł" (waste collection)
    r"(?:wywo[zź]\s+)?[śs]mieci[\s:\-–]+(\d[\d\s]{1,4})\s*z[łl]",
    # "ogrzewanie 150 zł / ogrzewanie ok. 200 zł" (heating)
    r"ogrzewanie[\s:\-–~ok\.]+(\d[\d\s]{1,4})\s*z[łl]",
    # "woda 50 zł / zimna/ciepła woda 80 zł" (water)
    r"(?:zimna\s+|ciep[łl]a\s+)?woda[\s:\-–~ok\.]+(\d[\d\s]{1,4})\s*z[łl]",
    # "prąd 120 zł / energia elektryczna 90 zł" (electricity)
    r"(?:pr[aą]d|energia\s+elektryczna)[\s:\-–~ok\.]+(\d[\d\s]{1,4})\s*z[łl]",
    # "gaz 80 zł" (gas)
    r"gaz[\s:\-–~ok\.]+(\d[\d\s]{1,4})\s*z[łl]",
    # "internet 70 zł / tv 50 zł"
    r"(?:internet|tv|telewizja)[\s:\-–~ok\.]+(\d[\d\s]{1,4})\s*z[łl]",
    # "miejsce parkingowe 250 zł / garaż 300 zł" (parking / garage)
    r"(?:miejsce\s+postojowe|miejsce\s+parkingowe|parking|gara[zż])[\s:\-–~ok\.]+(\d[\d\s]{1,4})\s*z[łl]",
]

# Cost mentions without a stated amount. They do not affect the total but are an
# important risk signal for filters and AI evaluation.
UNKNOWN_COST_PATTERNS = [
    (r"media\s+(?:wed[łl]ug|wg)\s+zu[zż]ycia", "utilities billed by usage — amount unknown"),
    (r"(?:pr[aą]d|energia\s+elektryczna)\s+(?:wed[łl]ug|wg)\s+zu[zż]ycia", "electricity billed by usage — amount unknown"),
    (r"gaz\s+(?:wed[łl]ug|wg)\s+zu[zż]ycia", "gas billed by usage — amount unknown"),
    (r"woda\s+(?:wed[łl]ug|wg)\s+zu[zż]ycia", "water billed by usage — amount unknown"),
    (r"ogrzewanie\s+(?:wed[łl]ug|wg)\s+zu[zż]ycia", "heating billed by usage — amount unknown"),
    (r"czynsz\s+administracyjny\s+do\s+ustalenia", "admin fee to be negotiated — amount unknown"),
    (r"op[łl]aty\s+eksploatacyjne\s+do\s+ustalenia", "service charges to be negotiated — amount unknown"),
]


def extract_extra_costs(
    description: str,
    structured_extra: int = 0,
) -> tuple[int, list[str]]:
    """Analyses a listing description and extracts the total additional costs.

    Args:
        description: Description text (lowercase).
        structured_extra: Amount from the "Czynsz (dodatkowo)" OLX sidebar field.

    Returns:
        (total_extra_amount, list_of_found_items)

    Logic:
    1. If the description contains "all inclusive" phrases → extra = 0
    2. Otherwise search for amounts next to cost keywords and sum them up
    3. If the OLX sidebar contains a "Czynsz (dodatkowo)" value — use whichever is higher
       (structured vs regex) as the final result
    4. For ranges (e.g. "200-400 PLN") → take the higher value (pessimistic approach)
    """
    if not description:
        return 0, ["(no description — costs unknown)"]

    # Normalise decimal amounts: "600,00 zł" → "600 zł"
    description = re.sub(r"(\d),\d{2}\s*z", r"\1 z", description)

    for pattern in WLICZONE_PATTERNS:
        if re.search(pattern, description, re.I):
            return 0, ["fees included in price"]

    found_items = []
    total_extra = 0
    used_spans = []  # dedup — the same text position may match multiple patterns

    for pattern in KOSZT_PATTERNS:
        for match in re.finditer(pattern, description, re.I):
            span = match.span()

            # Skip overlapping matches
            if any(s[0] < span[1] and span[0] < s[1] for s in used_spans):
                continue
            used_spans.append(span)

            groups = [g for g in match.groups() if g is not None]
            # For ranges (e.g. "200-400") take the higher value
            amounts = []
            for g in groups:
                try:
                    amounts.append(int(re.sub(r"\s", "", g)))
                except ValueError:
                    pass

            if amounts:
                amount = max(amounts)  # pessimistic: take the upper end of the range
                total_extra += amount
                found_items.append(f"{match.group(0).strip()} → {amount} PLN")

    unknown_items: list[str] = []
    for pattern, label in UNKNOWN_COST_PATTERNS:
        if re.search(pattern, description, re.I):
            unknown_items.append(label)

    if not found_items:
        if structured_extra > 0:
            return structured_extra, [f"Admin fee (OLX sidebar): {structured_extra} PLN"]
        if unknown_items:
            return 0, unknown_items
        return 0, ["(no additional cost mentions found)"]

    if structured_extra > total_extra:
        return structured_extra, [f"Admin fee (OLX sidebar): {structured_extra} PLN"]

    return total_extra, found_items + unknown_items


LLM_PROMPT = """Analyse the following apartment rental listing description and extract ONLY the additional monthly costs that are NOT included in the rent price (e.g. admin fee, utilities, service charges, central heating, water, waste collection, heating, bills, etc.).

Rules:
- If the description says everything is included in the price or utilities are included — return 0.
- If there are no mentions of additional costs — return 0.
- For ranges (e.g. "700-800 PLN") choose the higher value.
- If a cost is stated per person (e.g. "920 PLN/1 person; 1110 PLN/2 people; ...") — always take the 1-person value (the first one).
- Sum all separate cost items (admin fee + utilities + advance payments etc.).

Respond ONLY in JSON format (no markdown, no explanation):
{"extra_koszt": <integer amount>, "pozycje": ["description of item 1", "description of item 2"]}

Listing description:
"""

LISTING_ASSESSMENT_PROMPT = """Evaluate the attractiveness of an apartment rental listing from a tenant's perspective.

Rules:
- Assess price/area ratio, description quality, cost transparency, and red flags.
- Factor in additional cost information, hidden-fee risk, and missing data.
- If the user provided priorities, treat them as additional scoring context.
- Do not assume facts not present in the data. If something is missing, flag it as a risk.
- Respond ONLY in JSON.

Required format:
{
  "score": <integer 0-100>,
  "verdict": "contact" | "consider" | "skip",
  "summary": "brief justification in English",
  "strengths": ["short strength 1", "short strength 2"],
  "risks": ["short risk 1", "short risk 2"],
  "hidden_cost_risk": "low" | "medium" | "high"
}

Input data:
"""

LISTING_ASSESSMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "verdict": {"type": "string", "enum": ["contact", "consider", "skip"]},
        "summary": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "hidden_cost_risk": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["score", "verdict", "summary", "strengths", "risks", "hidden_cost_risk"],
    "additionalProperties": False,
}


def _build_listing_assessment_input(
    listing: Listing,
    description: str,
    preferences: str = "",
) -> str:
    """Serialises listing data into a compact JSON string passed to the model."""
    payload = {
        "title": listing.get("title", ""),
        "price": listing.get("price"),
        "area_m2": listing.get("metraz"),
        "location": listing.get("lokalizacja", ""),
        "posted_at": listing.get("data", ""),
        "url": listing.get("url", ""),
        "extra_cost_total": listing.get("extra_koszt"),
        "extra_cost_items": listing.get("extra_pozycje", []),
        "user_preferences": preferences.strip(),
        "description": description[:5000],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _normalize_listing_assessment(data: dict[str, Any], source: str) -> dict[str, Any]:
    """Cleans up the model result and guards against missing or invalid fields."""
    raw_score = data.get("score")
    try:
        score = max(0, min(100, int(raw_score)))
    except (TypeError, ValueError):
        score = None

    verdict = str(data.get("verdict", "consider")).strip().lower() or "consider"
    if verdict not in {"contact", "consider", "skip"}:
        verdict = "consider"

    hidden_cost_risk = str(data.get("hidden_cost_risk", "medium")).strip().lower() or "medium"
    if hidden_cost_risk not in {"low", "medium", "high"}:
        hidden_cost_risk = "medium"

    strengths = [str(item).strip() for item in data.get("strengths", []) if str(item).strip()]
    risks = [str(item).strip() for item in data.get("risks", []) if str(item).strip()]
    summary = str(data.get("summary", "")).strip()

    return {
        "ai_score": score,
        "ai_verdict": verdict,
        "ai_summary": summary,
        "ai_strengths": strengths,
        "ai_risks": risks,
        "ai_hidden_cost_risk": hidden_cost_risk,
        "ai_source": source,
    }


def _empty_listing_assessment(source: str, reason: str) -> dict[str, Any]:
    """Returns a neutral result when AI evaluation could not be performed."""
    return {
        "ai_score": None,
        "ai_verdict": "consider",
        "ai_summary": reason,
        "ai_strengths": [],
        "ai_risks": [],
        "ai_hidden_cost_risk": "medium",
        "ai_source": source,
    }


def _request_openai_json(
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    schema_name: str,
    schema: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    """Sends a Chat Completions request with `response_format=json_schema`."""
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": messages,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        },
        timeout=timeout,
    )
    resp.raise_for_status()

    message = resp.json()["choices"][0]["message"]
    if message.get("refusal"):
        raise ValueError(f"Model refused to respond: {message['refusal']}")

    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
        )

    if not isinstance(content, str) or not content.strip():
        raise ValueError("Empty JSON response from OpenAI")

    return json.loads(content)


def extract_extra_costs_llm(
    description: str,
    structured_extra: int = 0,
    llm_url: str = "http://localhost:11434",
    llm_model: str = "llama3",
    timeout: int = 60,
) -> tuple[int, list[str]]:
    """Analyses listing description via local Ollama LLM for additional costs.
    Falls back to regex if the LLM is unavailable or returns invalid JSON.
    """
    if not description:
        return 0, ["(no description — costs unknown)"]

    prompt = LLM_PROMPT + description[:3000]  # cap to avoid exceeding context window

    try:
        resp = requests.post(
            f"{llm_url.rstrip('/')}/api/generate",
            json={"model": llm_model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
        data = _extract_json_object(raw)
        extra_koszt = int(data.get("extra_koszt", 0))
        pozycje = [str(p) for p in data.get("pozycje", [])]

        # If the OLX sidebar reported a higher amount, use it
        if structured_extra > extra_koszt:
            return structured_extra, [f"Admin fee (OLX sidebar): {structured_extra} PLN"]

        if extra_koszt == 0:
            return 0, ["(LLM: no additional costs found in description)"]

        return extra_koszt, pozycje

    except requests.RequestException as e:
        logger.warning("LLM unavailable (%s) — falling back to regex", e)
        return extract_extra_costs(description, structured_extra)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        logger.warning("Failed to parse LLM response (%s) — falling back to regex", e)
        return extract_extra_costs(description, structured_extra)


def extract_extra_costs_openai(
    description: str,
    structured_extra: int = 0,
    api_key: str = "",
    openai_model: str = "gpt-4o-mini",
    timeout: int = 30,
) -> tuple[int, list[str]]:
    """Analyses listing description via OpenAI API for additional costs.
    Falls back to regex if the API is unavailable or returns invalid JSON.
    """
    if not description:
        return 0, ["(no description — costs unknown)"]
    if not api_key:
        logger.warning("Missing OpenAI API key — falling back to regex")
        return extract_extra_costs(description, structured_extra)

    prompt = LLM_PROMPT + description[:3000]

    try:
        data = _request_openai_json(
            api_key=api_key,
            model=openai_model,
            messages=[{"role": "user", "content": prompt}],
            schema_name="listing_extra_costs",
            schema={
                "type": "object",
                "properties": {
                    "extra_koszt": {"type": "integer", "minimum": 0},
                    "pozycje": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["extra_koszt", "pozycje"],
                "additionalProperties": False,
            },
            timeout=timeout,
        )
        extra_koszt = int(data.get("extra_koszt", 0))
        pozycje = [str(p) for p in data.get("pozycje", [])]

        if structured_extra > extra_koszt:
            return structured_extra, [f"Admin fee (OLX sidebar): {structured_extra} PLN"]

        if extra_koszt == 0:
            return 0, ["(OpenAI: no additional costs found in description)"]

        return extra_koszt, pozycje

    except requests.RequestException as e:
        logger.warning("OpenAI API unavailable (%s) — falling back to regex", e)
        return extract_extra_costs(description, structured_extra)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        logger.warning("Failed to parse OpenAI response (%s) — falling back to regex", e)
        return extract_extra_costs(description, structured_extra)


def analyze_listing_with_ai(
    listing: Listing,
    description: str,
    *,
    provider: str = "ollama",
    preferences: str = "",
    llm_url: str = "http://localhost:11434",
    llm_model: str = "llama3",
    api_key: str = "",
    openai_model: str = "gpt-4o-mini",
    timeout: int = 30,
) -> dict[str, Any]:
    """Enriches a listing with an AI evaluation score.

    Deliberately separated from cost extraction so that scoring can be
    enabled independently of the budget filter.
    """
    prompt = LISTING_ASSESSMENT_PROMPT + _build_listing_assessment_input(listing, description, preferences)

    if provider == "openai":
        if not api_key:
            logger.warning("Missing OpenAI API key — skipping AI evaluation")
            return _empty_listing_assessment("openai", "AI evaluation unavailable: missing OpenAI API key.")
        try:
            data = _request_openai_json(
                api_key=api_key,
                model=openai_model,
                messages=[
                    {"role": "system", "content": "You respond only with valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                schema_name="listing_assessment",
                schema=LISTING_ASSESSMENT_SCHEMA,
                timeout=timeout,
            )
            return _normalize_listing_assessment(data, f"OpenAI/{openai_model}")
        except requests.RequestException as e:
            logger.warning("OpenAI API unavailable (%s) — skipping AI evaluation", e)
            return _empty_listing_assessment("openai", "AI evaluation unavailable: cannot connect to OpenAI.")
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            logger.warning("Failed to parse AI evaluation from OpenAI (%s)", e)
            return _empty_listing_assessment("openai", "AI evaluation unavailable: invalid model response.")

    try:
        resp = requests.post(
            f"{llm_url.rstrip('/')}/api/generate",
            json={"model": llm_model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = _extract_json_object(resp.json().get("response", ""))
        return _normalize_listing_assessment(data, f"Ollama/{llm_model}")
    except requests.RequestException as e:
        logger.warning("LLM unavailable (%s) — skipping AI evaluation", e)
        return _empty_listing_assessment("ollama", "AI evaluation unavailable: cannot connect to Ollama.")
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        logger.warning("Failed to parse AI evaluation from LLM (%s)", e)
        return _empty_listing_assessment("ollama", "AI evaluation unavailable: invalid model response.")


def fetch_ollama_models(llm_url: str) -> list[str]:
    """Fetches the list of available models from Ollama. Returns [] if unavailable."""
    try:
        resp = requests.get(
            f"{llm_url.rstrip('/')}/api/tags",
            timeout=5,
        )
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return []


def has_next_page(soup: BeautifulSoup) -> bool:
    """Returns True if a next results page exists."""
    return bool(
        soup.find(attrs={"data-testid": "pagination-forward"})
        or soup.find(attrs={"data-cy": "pagination-forward"})
        or soup.find("a", attrs={"aria-label": re.compile(r"nast[eę]pna|next", re.I)})
    )


def load_seen(path: str) -> set[str]:
    """Loads the set of already-seen listing IDs from a JSON file."""
    p = Path(path)
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Cannot read seen file %s: %s", path, e)
    return set()


def save_seen(path: str, seen: set[str]) -> None:
    """Saves the set of already-seen listing IDs to a JSON file."""
    target = _ensure_parent_dir(path)
    try:
        target.write_text(
            json.dumps(sorted(seen), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except OSError as e:
        logger.warning("Cannot write seen file %s: %s", path, e)


def print_header(config: dict) -> None:
    """Prints the scan header to stdout (CLI mode)."""
    logger.info("='" * 28)
    logger.info("  OLX Scraper  [%s]", datetime.now().strftime("%d.%m.%Y %H:%M:%S"))
    district = f" / {config['dzielnica']}" if config.get("dzielnica") else ""
    logger.info("  Location: %s%s", config["miasto"], district)
    logger.info("  Price:  %s–%s /month", config["cena_min"], config["cena_max"])
    logger.info("  Area:   %s–%s m2", config["metraz_min"], config["metraz_max"])
    if config.get("budzet_lacznie"):
        logger.info("  Total budget: max %s", config["budzet_lacznie"])
    logger.info("='" * 28)


def print_listing(listing: dict) -> None:
    """Prints listing details to the logger (CLI mode)."""
    price = f"{listing['price']} /month" if listing["price"] else "price hidden"
    area = f"{listing['metraz']:.1f} m2" if listing["metraz"] else "? m2"
    logger.info("  [NEW] %s", "-" * 44)
    logger.info("  Title:  %s", listing["title"])
    logger.info("  Rent: %s   Area: %s", price, area)

    extra = listing.get("extra_koszt")
    if extra is not None and extra > 0:
        total = (listing["price"] or 0) + extra
        logger.info("  Extra fees: %s /month", extra)
        logger.info("  Total:      %s /month", total)
        for item in listing.get("extra_pozycje", []):
            logger.info("    - %s", item)
    elif listing.get("extra_pozycje"):
        for item in listing.get("extra_pozycje", []):
            logger.info("  Info: %s", item)

    if listing.get("ai_score") is not None:
        logger.info(
            "  AI: %s/100 (%s, hidden cost risk: %s)",
            listing["ai_score"],
            listing.get("ai_verdict", "consider"),
            listing.get("ai_hidden_cost_risk", "medium"),
        )
        if listing.get("ai_summary"):
            logger.info("  AI note: %s", listing["ai_summary"])

    if listing["lokalizacja"]:
        logger.info("  Location: %s", listing["lokalizacja"])
    if listing["data"]:
        logger.info("  Posted: %s", listing["data"])
    logger.info("  URL: %s", listing["url"])


# iMessage on macOS via AppleScript

def send_imessage(
    number: str,
    message: str,
    log_fn: Callable[[str], None] | None = None,
) -> None:
    """Sends a message via iMessage/Messages.app on macOS using AppleScript."""
    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)
        else:
            logger.info(msg)

    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    # Escape the number to prevent injection in the AppleScript context
    safe_number = number.replace("\\", "\\\\").replace('"', '\\"')

    script = f'''
    tell application "Messages"
        send "{safe_msg}" to buddy "{safe_number}"
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=15, text=True
        )
        if result.returncode == 0:
            _log(f"  [OK] iMessage -> {number}")
        else:
            err = result.stderr.strip()
            _log(f"  [WARN] iMessage blad: {err}")
    except FileNotFoundError:
        _log("  [ERR] 'osascript' niedostepny - wymagany macOS")
    except subprocess.SubprocessError as e:
        _log(f"  [ERR] Blad wysylania iMessage: {e}")
    except OSError as e:
        _log(f"  [ERR] Blad systemowy iMessage: {e}")


def format_imessage(listing: dict) -> str:
    """Formats a listing as an iMessage notification."""
    cena = f"{listing['price']} PLN" if listing["price"] else "price hidden"
    metraz = f"{listing['metraz']:.0f}m2" if listing["metraz"] else ""

    extra = listing.get("extra_koszt", 0) or 0
    if extra > 0:
        lacznie = (listing["price"] or 0) + extra
        koszt_info = f"+ {extra} PLN fees = {lacznie} PLN total"
    else:
        koszt_info = ""

    parts = [p for p in [cena, metraz, listing["lokalizacja"]] if p]
    msg = f"New on OLX:\n{listing['title']}\n{' | '.join(parts)}\n"
    if koszt_info:
        msg += f"{koszt_info}\n"
    msg += listing["url"]
    return msg


# Main scanning loop for CLI mode.
def scrape_once(config: dict, seen: set[str]) -> int:
    """Scans OLX and notifies about new listings. Returns the count of new listings."""
    country_cfg = load_country_config(config.get("country", "pl"))
    base_url = f"https://{country_cfg['domain']}" if country_cfg.get("domain") else "https://www.olx.pl"
    currency_hints = country_cfg.get("currency_symbols") or ["zł"]

    print_header(config)
    new_count = 0

    max_stron = config["max_stron"]
    wszystkie = max_stron == "all"
    limit = MAX_PAGES_UNLIMITED if wszystkie else int(max_stron)

    for page in range(1, limit + 1):
        label = f"{page}/{'all' if wszystkie else limit}"
        url = build_url(config, page, country_cfg)
        logger.info("  Page %s: %s", label, url)

        soup = fetch_page(url)
        if not soup:
            break

        listings = parse_listings(soup, base_url=base_url, currency_hints=currency_hints)
        logger.info("  Listings on page: %d", len(listings))

        if not listings:
            logger.info("  No results — stopping.")
            break

        for listing in listings:
            if listing["id"] in seen:
                continue

            # Area filter (local)
            if listing["metraz"] is not None:
                if not (config["metraz_min"] <= listing["metraz"] <= config["metraz_max"]):
                    seen.add(listing["id"])
                    continue

            # Fetch details only when needed for the budget filter or AI.
            budzet = config.get("budzet_lacznie")
            ai_enabled = config.get("ai_enabled", False)
            should_fetch_detail = (budzet and listing["price"] is not None) or ai_enabled
            opis = ""
            structured_extra = 0

            if should_fetch_detail:
                logger.info("    ↳ Checking details: %s...", listing["url"].split("/")[-1][:40])
                opis, structured_extra = fetch_detail(listing["url"])

            if budzet and listing["price"] is not None:
                use_llm = config.get("llm_enabled", False)
                provider = config.get("llm_provider", "ollama")
                if use_llm and provider == "openai":
                    extra_koszt, extra_pozycje = extract_extra_costs_openai(
                        opis,
                        structured_extra,
                        api_key=config.get("openai_key", ""),
                        openai_model=config.get("openai_model", "gpt-4o-mini"),
                        timeout=config.get("openai_timeout", 30),
                    )
                elif use_llm:
                    extra_koszt, extra_pozycje = extract_extra_costs_llm(
                        opis,
                        structured_extra,
                        llm_url=config.get("llm_url", "http://localhost:11434"),
                        llm_model=config.get("llm_model", "llama3"),
                        timeout=config.get("llm_timeout", 60),
                    )
                else:
                    extra_koszt, extra_pozycje = extract_extra_costs(opis, structured_extra)
                listing["extra_koszt"] = extra_koszt
                listing["extra_pozycje"] = extra_pozycje

                lacznie = listing["price"] + extra_koszt
                if lacznie > budzet:
                    logger.info(
                        "  rejected (%s + %s = %s PLN > limit %s PLN)",
                        listing["price"], extra_koszt, lacznie, budzet,
                    )
                    seen.add(listing["id"])
                    time.sleep(DELAY_BETWEEN_REQUESTS)
                    continue
                logger.info("  ok (%s PLN total, limit: %s PLN)", lacznie, budzet)
                time.sleep(DELAY_BETWEEN_REQUESTS)
            else:
                listing["extra_koszt"] = None
                listing["extra_pozycje"] = []

            if ai_enabled:
                listing.update(
                    analyze_listing_with_ai(
                        listing,
                        opis,
                        provider=config.get("llm_provider", "ollama"),
                        preferences=config.get("ai_preferences", ""),
                        llm_url=config.get("llm_url", "http://localhost:11434"),
                        llm_model=config.get("llm_model", "llama3"),
                        api_key=config.get("openai_key", ""),
                        openai_model=config.get("openai_model", "gpt-4o-mini"),
                        timeout=config.get(
                            "openai_timeout" if config.get("llm_provider") == "openai" else "llm_timeout",
                            30,
                        ),
                    )
                )

            seen.add(listing["id"])
            new_count += 1
            print_listing(listing)

            if config.get("wyslij_imessage") and config.get("imessage_numer"):
                msg = format_imessage(listing)
                send_imessage(config["imessage_numer"], msg)

        if not has_next_page(soup):
            logger.info("  Last page — done.")
            break

        time.sleep(DELAY_BETWEEN_PAGES)

    return new_count


def main() -> None:
    """CLI entry point — parses arguments and starts scanning."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        description="OLX scraper for apartment rentals with iMessage notifications"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable verbose logging (DEBUG)"
    )
    parser.add_argument(
        "--interval", type=int, default=0,
        help="Scan interval in seconds (0 = run once, 86400 = once a day)"
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Clear the seen-listings cache"
    )
    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    seen_path = CONFIG["seen_file"]

    if args.reset:
        Path(seen_path).unlink(missing_ok=True)
        logger.info("Cache cleared.")

    seen = load_seen(seen_path)
    logger.info("Seen listings loaded: %d", len(seen))

    if args.interval > 0:
        h = args.interval / 3600
        logger.info("Continuous mode — scanning every %.1fh (Ctrl+C to stop)", h)
        try:
            while True:
                nowe = scrape_once(CONFIG, seen)
                save_seen(seen_path, seen)
                nastepne = datetime.fromtimestamp(time.time() + args.interval)
                logger.info("New listings: %d | Next scan: %s",
                            nowe, nastepne.strftime("%d.%m.%Y %H:%M"))
                time.sleep(args.interval)
        except KeyboardInterrupt:
            save_seen(seen_path, seen)
            logger.info("Stopped. Cache saved.")
    else:
        try:
            nowe = scrape_once(CONFIG, seen)
        finally:
            save_seen(seen_path, seen)
        logger.info("Done. New: %d | Total seen: %d",
                    nowe, len(seen))


if __name__ == "__main__":
    main()
