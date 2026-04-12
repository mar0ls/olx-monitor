#!/usr/bin/env python3
"""
OLX.pl Scraper – wynajem mieszkań
Powiadomienia: terminal + iMessage (macOS)

Wymagania:
    pip install requests beautifulsoup4

Użycie:
    python olx_scraper.py                     # jednorazowo
    python olx_scraper.py --interval 86400    # raz na dobę
    python olx_scraper.py --reset             # wyczyść pamięć i zacznij od nowa
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
from typing import TypedDict

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Stałe
REQUEST_TIMEOUT = 15          # s – timeout dla requests.get()
DELAY_BETWEEN_PAGES = 2       # s – pauza między stronami wyników
DELAY_BETWEEN_REQUESTS = 1    # s – pauza między pobieraniem szczegółów
MAX_PAGES_UNLIMITED = 999     # strony – wartość "all" zamieniana na tę liczbę


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

# ─────────────────────────────────────────────────────────────
#  KONFIGURACJA – dostosuj do swoich potrzeb
# ─────────────────────────────────────────────────────────────
CONFIG = {
    # Miasto (tak jak w URL olx.pl, np. "warszawa", "krakow", "wroclaw")
    "miasto": "warszawa",

    # ID dzielnicy z OLX (opcjonalnie; None = całe miasto)
    # Skrypt zawiera gotowe mapy dla: Warszawa, Kraków, Wrocław, Poznań, Gdańsk,
    # Gdynia, Sopot, Łódź, Katowice, Szczecin, Białystok, Częstochowa.
    # Możesz też wpisać nazwę dzielnicy jako "dzielnica": "ursynow" – zostanie
    # automatycznie zamieniona na district_id dla bieżącego miasta.
    # Aby znaleźć ID ręcznie: otwórz OLX, wybierz dzielnicę i sprawdź
    # parametr search[district_id] w URL.
    "district_id": 373,  # Ursynów (Warszawa)

    # Filtry cenowe (PLN/miesiąc)
    "cena_min": 0,
    "cena_max": 4000,

    # Metraż (m²) – filtrowane lokalnie na podstawie tytułu/opisu
    "metraz_min": 0,
    "metraz_max": 50,

    # Maksymalny ŁĄCZNY koszt miesięczny (czynsz najmu + opłaty dodatkowe z opisu)
    # Ustaw None żeby wyłączyć ten filtr
    "budzet_lacznie": 4000,

    # Ile stron OLX przeszukać (każda ma ~36 ogłoszeń)
    # Liczba całkowita lub "all" żeby przejść wszystkie strony
    "max_stron": 2,

    # Numer telefonu do powiadomień iMessage (+48XXXXXXXXX)
    "imessage_numer": "+48600000000",

    # Czy wysyłać iMessage? (wymaga macOS z zalogowaną aplikacją Messages)
    "wyslij_imessage": False,

    # Plik do zapamiętywania już widzianych ogłoszeń
    "seen_file": "seen_listings.json",
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


# ── Normalizacja i mapa dzielnic polskich miast ───────────────

_POL_TRANS = str.maketrans({
    'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n',
    'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
    'Ą': 'a', 'Ć': 'c', 'Ę': 'e', 'Ł': 'l', 'Ń': 'n',
    'Ó': 'o', 'Ś': 's', 'Ź': 'z', 'Ż': 'z',
})


def _normalize_name(s: str) -> str:
    """Zamienia nazwę na klucz ASCII: małe litery, bez diakrytyków, spacje → myślniki."""
    return re.sub(r"[^a-z0-9]+", "-", s.lower().translate(_POL_TRANS)).strip("-")


# Mapa dzielnic polskich miast woj. → district_id na OLX (kwiecień 2026)
# Zewnętrzny klucz = slug URL miasta (bez polskich znaków).
# Wewnętrzny klucz = polska nazwa wyświetlana (dla GUI i komunikatów).
# Małe miasta (np. Bydgoszcz, Lublin) nie mają w OLX filtrów dzielnic.
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
        "Śródmieście": 351,
        "Targówek": 377,
        "Ursus": 371,
        "Ursynów": 373,
        "Wawer": 383,
        "Wesoła": 533,
        "Wilanów": 375,
        "Włochy": 357,
        "Wola": 359,
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
        "Łagiewniki-Borek Fałęcki": 265,
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
        "Górczyn": 697,
        "Grunwald": 323,
        "Jeżyce": 325,
        "Junikowo": 699,
        "Komandoria": 763,
        "Łacina": 765,
        "Ławica": 701,
        "Łazarz": 703,
        "Naramowice": 705,
        "Ogrody": 707,
        "Piątkowo": 709,
        "Podolany": 711,
        "Rataje": 713,
        "Smochowice": 715,
        "Sołacz": 767,
        "Stare Miasto": 327,
        "Starołęka": 717,
        "Strzeszyn": 719,
        "Szczepankowo": 721,
        "Śródka": 769,
        "Warszawskie": 723,
        "Wilda": 331,
        "Winiary": 725,
        "Winogrady": 727,
        "Zawady": 778,
    },
    "gdansk": {
        "Aniołki": 97,
        "Brętowo": 427,
        "Brzeźno": 113,
        "Chełm z dzielnicą Gdańsk Południe": 93,
        "Jasień": 691,
        "Kokoszki": 423,
        "Krakowiec - Górki Zachodnie": 129,
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
        "Śródmieście": 135,
        "Ujeścisko - Łostowice": 770,
        "VII Dwór": 107,
        "Wrzeszcz": 99,
        "Wyspa Sobieszewska": 499,
        "Wzgórze Mickiewicza": 421,
        "Żabianka - Wejhera - Jelitkowo - Tysiąclecia": 111,
        "Zaspa Młyniec": 121,
        "Zaspa Rozstaje": 117,
    },
    "gdynia": {
        "Babie Doły": 593,
        "Chwarzno-Wiczlino": 147,
        "Chylonia": 139,
        "Cisowa": 141,
        "Dąbrowa": 175,
        "Działki Leśne": 157,
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
        "Śródmieście": 159,
        "Wielki Kack": 173,
        "Witomino-Leśniczówka": 177,
        "Witomino-Radiostacja": 179,
        "Wzgórze Świętego Maksymiliana": 163,
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
        "Śródmieście": 299,
        "Widzew": 297,
    },
    "katowice": {
        "Bogucice": 221,
        "Brynów": 231,
        "Dąb": 453,
        "Dąbrówka Mała": 225,
        "Giszowiec": 229,
        "Janów-Nikiszowiec": 223,
        "Kostuchna": 455,
        "Koszutka": 217,
        "Ligota-Panewniki": 237,
        "Murcki": 241,
        "Osiedle Paderewskiego-Muchowiec": 215,
        "Osiedle Tysiąclecia": 247,
        "Osiedle Witosa": 243,
        "Piotrowice-Ochojec": 235,
        "Podlesie": 511,
        "Szopienice-Burowiec": 227,
        "Śródmieście": 211,
        "Wełnowiec-Józefowiec": 219,
        "Zawodzie": 213,
        "Załęska Hałda-Brynów": 233,
        "Załęże": 245,
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
        "Świerczewo": 759,
        "Warszewo": 751,
        "Zdroje": 753,
    },
    "bialystok": {
        "Antoniuk": 61,
        "Bacieczki": 55,
        "Bema": 41,
        "Białostoczek": 17,
        "Bojary": 11,
        "Centrum": 15,
        "Dojlidy": 7,
        "Dojlidy Górne": 47,
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
        "Słoneczny Stok": 63,
        "Starosielce": 49,
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
        "Śródmieście": 83,
        "Trzech Wieszczów": 85,
        "Tysiąclecie": 417,
        "Wrzosowiak": 77,
        "Wyczerpy-Aniołów": 73,
        "Zawodzie-Dąbie": 79,
    },
}

# Słownik z kluczami znormalizowanymi do ASCII – generowany automatycznie
# Służy do rozpoznawania nazw dzielnic wpisanych przez użytkownika (np. "ursynow", "Nowa Huta")
CITY_DISTRICT_IDS: dict[str, dict[str, int]] = {
    city: {_normalize_name(name): did for name, did in districts.items()}
    for city, districts in CITY_DISTRICT_DISPLAY.items()
}


def get_districts_for_city(miasto: str) -> dict[str, int]:
    """Zwraca słownik {nazwa_wyświetlana: district_id} dla podanego miasta.

    Args:
        miasto: Nazwa miasta (np. ``'warszawa'``, ``'Kraków'``).

    Returns:
        Słownik nazw dzielnic → district_id, posortowany alfabetycznie.
        Pusty słownik jeśli miasto nie jest obsługiwane lub nie ma podziału na dzielnice.
    """
    return dict(sorted(CITY_DISTRICT_DISPLAY.get(_normalize_name(miasto), {}).items()))


# ── Budowanie URL ─────────────────────────────────────────────

def build_url(config: dict, page: int = 1) -> str:
    """Buduje URL wyszukiwania OLX na podstawie konfiguracji i numeru strony."""
    miasto = config["miasto"].lower()

    url = (
        f"https://www.olx.pl/nieruchomosci/mieszkania/wynajem/{miasto}/"
        f"?search%5Bfilter_float_price%3Afrom%5D={config['cena_min']}"
        f"&search%5Bfilter_float_price%3Ato%5D={config['cena_max']}"
        f"&search%5Bfilter_float_m%3Afrom%5D={config.get('metraz_min', 0) or 0}"
        f"&search%5Bfilter_float_m%3Ato%5D={config.get('metraz_max', 999) or 999}"
    )

    # Filtr dzielnicy przez district_id (poprawna metoda OLX)
    district_id = config.get("district_id")
    if not district_id and config.get("dzielnica"):
        # obsługa nazwy dzielnicy jako alternatywy dla district_id
        dzielnica_norm = _normalize_name(config["dzielnica"])
        district_id = CITY_DISTRICT_IDS.get(miasto, {}).get(dzielnica_norm)
        if not district_id:
            logger.warning(
                "Nieznana dzielnica '%s' dla miasta '%s' – szukaj po całym mieście "
                "lub ustaw district_id ręcznie.",
                config["dzielnica"], miasto,
            )
    if district_id:
        url += f"&search%5Bdistrict_id%5D={district_id}"

    if page > 1:
        url += f"&page={page}"
    return url


# ── Pobieranie i parsowanie ───────────────────────────────────

def fetch_page(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.HTTPError as e:
        logger.error("Błąd HTTP %s dla %s", e.response.status_code if e.response else "?", url)
        return None
    except requests.RequestException as e:
        logger.error("Nie można pobrać strony %s: %s", url, e)
        return None


def extract_id_from_url(url: str) -> str:
    """Wyciąga ID z URL, np. 'ID19HnE4' z '.../oferta/...-CID3-ID19HnE4.html'"""
    m = re.search(r"-(ID[A-Za-z0-9]+)\.html", url)
    if m:
        return m.group(1)
    return url.strip("/").split("/")[-1][-12:]


def parse_price(text: str) -> int | None:
    """
    Parsuje cenę z tekstu. Obsługuje warianty:
      '3 000 zł', '3000 zl', '3 000 PLN', '3000 pln',
      '3000 zlotych', '3000 złotych', '4 500 złdo negocjacji'
    """
    # Ujednolicamy: małe litery, spacje nierozdzielające → spacja
    t = text.lower().replace("\xa0", " ")
    # Wytnij część przed jednostką walutową
    unit = re.search(r"(\d[\d\s]*)(?:z[łl]|pln|z?[łl]otych)", t)
    if unit:
        digits = re.sub(r"[^\d]", "", unit.group(1))
        return int(digits) if digits else None
    # Fallback: weź pierwszą sekwencję cyfr z tekstu
    digits = re.sub(r"[^\d]", "", t)
    if digits:
        value = int(digits)
        # sanity check – odrzuca numery tel. itp.
        return value if value <= 99_999 else None
    return None


def parse_metraz(text: str) -> float | None:
    """'65 m²' lub '65,5m2' → 65.0"""
    m = re.search(r"([\d]+[,\.]?[\d]*)\s*m", text)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    return None


def parse_listings(soup: BeautifulSoup) -> list[Listing]:
    """
    Parsuje ogłoszenia ze strony OLX.

    Aktualny format (kwiecień 2026):
    - Karty ogłoszeń: <div data-cy="l-card">
    - Tytuł: <h4> lub <h6> wewnątrz karty
    - Cena: element z data-testid="ad-price" lub tekst "X zł"
    - Lokalizacja+data: data-testid="location-date", format "Miasto, Dzielnica - data"
    - Metraż: tekst zawierający "m²" w karcie (np. "65 m²")
    """
    listings = []
    cards = soup.find_all(attrs={"data-cy": "l-card"})

    for card in cards:
        try:
            # ── Link i ID ──
            link_tag = card.find("a", href=True)
            if not link_tag:
                continue
            url = link_tag["href"]
            if not url.startswith("http"):
                url = "https://www.olx.pl" + url
            ad_id = extract_id_from_url(url)

            # ── Tytuł ──
            title_tag = card.find(["h4", "h6", "h3"])
            title = title_tag.get_text(strip=True) if title_tag else link_tag.get_text(strip=True)
            if not title:
                continue

            # ── Cena ──
            price_tag = card.find(attrs={"data-testid": "ad-price"})
            price = None
            if price_tag:
                price = parse_price(price_tag.get_text(strip=True))
            else:
                # Fallback: szukaj tekstu z "zł" w karcie
                for s in card.strings:
                    if "zł" in s and re.search(r"\d", s):
                        price = parse_price(s)
                        break

            # ── Lokalizacja i data ──
            loc_tag = card.find(attrs={"data-testid": "location-date"})
            location_text = loc_tag.get_text(strip=True) if loc_tag else ""
            lokalizacja = location_text.split(" - ")[0].strip()
            data_dodania = location_text.split(" - ")[1].strip() if " - " in location_text else ""

            # ── Metraż ──
            # OLX wyświetla metraż jako osobny element, np. "65 m²"
            metraz = None
            for s in card.strings:
                s = s.strip()
                if "m²" in s or "m2" in s:
                    metraz = parse_metraz(s)
                    if metraz:
                        break
            # Fallback: szukaj w tytule
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
            logger.debug("Pominięto kartę ogłoszenia z powodu błędu: %s", e, exc_info=True)
            continue

    return listings


# ── Podstrona ogłoszenia – opis i dodatkowe koszty ───────────

def fetch_detail(url: str) -> tuple[str, int]:
    """
    Pobiera treść opisu z podstrony ogłoszenia OLX.

    Zwraca:
        (opis_tekst, czynsz_dodatkowy) — opis jako lowercase string
        oraz kwotę z pola "Czynsz (dodatkowo)" z sidebara OLX (0 jeśli brak).
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Aktualny selektor opisu na OLX (kwiecień 2026)
        desc_tag = (
            soup.find(attrs={"data-cy": "ad_description"})
            or soup.find(attrs={"itemprop": "description"})
            or soup.find(class_=re.compile(r"description|opis", re.I))
        )
        desc_text = desc_tag.get_text(" ", strip=True).lower() if desc_tag else ""

        # Pole strukturalne "Czynsz (dodatkowo): NNN zł" z sidebara OLX
        structured_extra = 0
        for el in soup.find_all(string=re.compile(r"czynsz.*dodatkowo", re.I)):
            parent = el.find_parent()
            if parent:
                sibling_text = parent.get_text(" ", strip=True)
                m = re.search(r"(\d[\d\s]*)\s*z[\u0142l]", sibling_text)
                if m:
                    structured_extra = int(re.sub(r"\s", "", m.group(1)))
                    break
        # Fallback: szukaj w całej stronie elementów z "Czynsz" + kwotą obok
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
        logger.warning("Nie można pobrać szczegółów ogłoszenia %s: %s", url, e)
        return "", 0


# Wzorce fraz wskazujących że opłaty SĄ wliczone w cenę (→ ekstra = 0)
WLICZONE_PATTERNS = [
    r"wszystk[oi][em]?\s+w\s+cen[ie]",       # "wszystko w cenie", "wszystkim w cen"
    r"w\s+tym\s+czynsz",                       # "w tym czynsz"
    r"media\s+wliczon[ea]",                    # "media wliczone/a"
    r"rachunk[i]?\s+wliczon[ea]",             # "rachunki wliczone"
    r"op[łl]aty\s+wliczon[ea]",               # "opłaty wliczone"
    r"bez\s+dodatkowych\s+op[łl]at",          # "bez dodatkowych opłat"
    r"czynsz\s+do\s+sp[oó][łl]dzielni\s+wliczon", # "czynsz do spółdzielni wliczony"
    r"c\.?o\.?\s+wliczon",                    # "c.o. wliczone"
    r"cena\s+zawiera",                         # "cena zawiera"
    r"cena\s+obejmuje",                        # "cena obejmuje"
]

# Wzorce wyciągające kwoty dodatkowych kosztów
# Szukamy kwot poprzedzonych lub następujących po słowach kluczowych
KOSZT_PATTERNS = [
    # "czynsz administracyjny/administracji 1100-1150" lub "czynsz administracji 400-500 zł" (zakres, zł opcjonalne)
    r"czynsz\s*(?:administracyjny|administracji|do\s+sp[oó][łl]dzielni|do\s+administracji|zarz[aą]dcy?)[\s:\-–]+(\d[\d\s]{1,5})\s*(?:-|–|do)\s*(\d[\d\s]{1,5})\s*(?:z[łl])?",
    # "czynsz administracyjny/administracji: 400 zł" lub "czynsz do administracji: 920 zł/1 osoba"
    r"czynsz\s*(?:administracyjny|administracji|do\s+sp[oó][łl]dzielni|do\s+administracji|zarz[aą]dcy?)[\s:\-–]+(\d[\d\s]{1,5})\s*z[łl]",
    # "zaliczka na energię/prąd/gaz: 150 zł/1 osoba"
    r"zaliczka\s+(?:na\s+)?(?:energi[ęe]|pr[aą]d|gaz|media)[\s:\-–]+(\d[\d\s]{1,4})\s*z[łl]",
    # "opłata/opłaty eksploatacyjne 350 zł"
    r"op[łl]at[ay]\s*(?:eksploatacyjn[ae]|administracyjn[ae]|za\s+mieszkanie)?[\s:\-–]+(\d[\d\s]{1,5})\s*z[łl]",
    # "media ok. 300 zł / media ~250 zł"
    r"media[\s:\-–~ok\.]+(\d[\d\s]{1,4})\s*z[łl]",
    # "+czynsz (obecnie ok 700zł)" / "+czynsz (opłata administracyjna): ok. 700 zł"
    r"\+\s*czynsz[^0-9]{0,60}(\d[\d\s]{1,4})\s*z[łl]",
    # "czynsz (opłata administracyjna): ok. 700 zł" (bez plusa, z nawiasem opisującym)
    r"czynsz\s*\([^)]{0,40}\)\s*:?\s*(?:ok\.?\s+)?(\d[\d\s]{1,4})\s*z[łl]",
    # "+ 400 zł czynsz" / "+ 300 zł opłaty"
    r"\+\s*(\d[\d\s]{1,4})\s*z[łl]\s*(?:czynsz|op[łl]at|media|rachunk)",
    # "NNN zł za opłaty/czynsz" (np. "645 zł za opłaty administracyjne")
    r"(\d[\d\s]{1,5})\s*z[łl]\s*za\s*(?:op[łl]at[yę]|czynsz|media|rachunk)\w*",
    # "2400 zł + 850 zł (opłaty administracyjne...)" — format otodom: cena + opłaty w nawiasie
    r"\d[\d\s]*\s*z[łl]\s*\+\s*(\d[\d\s]{1,5})\s*z[łl]\s*\(",
    # "= 3250 zł/miesiąc" przy strukturze cena + opłaty = łącznie (bierz łączną i odejmuj cenę najmu)
    # obsługiwane przez wzorzec wyżej — łącznie wyciąga kwotę opłat
    # "rachunki około 200-300 zł" → bierz wyższą
    r"rachunk[i]?[\s:\-–~\w\.]+(\d[\d\s]{1,4})\s*(?:-|–|do)\s*(\d[\d\s]{1,4})\s*z[łl]",
    # "rachunki 250 zł" / "+ ok 250 rachunki za media"
    r"rachunk[i]?[\s:\-–~\w\.]+(\d[\d\s]{1,4})\s*z[łl]",
    r"\+\s*(?:ok\.?\s+)?(\d[\d\s]{1,3})\s+rachunki?\s+za\s+media",
    # "koszty eksploatacji 450 zł"
    r"koszty\s+eksploatacji[\s:\-–]+(\d[\d\s]{1,4})\s*z[łl]",
    # "do tego/dodatkowo 300 zł"
    r"(?:do\s+tego|dodatkowo|plus|poza\s+tym)[\s:\-–]+(\d[\d\s]{1,4})\s*z[łl]",
    # "c.o. 150 zł / c/o: 200 zł"
    r"c\.?o\.?[\s:\-–]+(\d[\d\s]{1,4})\s*z[łl]",
    # "wywóz śmieci 50 zł"
    r"(?:wywo[zź]\s+)?[śs]mieci[\s:\-–]+(\d[\d\s]{1,4})\s*z[łl]",
    # "ogrzewanie 150 zł / ogrzewanie ok. 200 zł"
    r"ogrzewanie[\s:\-–~ok\.]+(\d[\d\s]{1,4})\s*z[łl]",
    # "woda 50 zł / zimna/ciepła woda 80 zł"
    r"(?:zimna\s+|ciep[łl]a\s+)?woda[\s:\-–~ok\.]+(\d[\d\s]{1,4})\s*z[łl]",
]


def extract_extra_costs(
    description: str,
    structured_extra: int = 0,
) -> tuple[int, list[str]]:
    """
    Analizuje opis ogłoszenia i wyciąga sumę dodatkowych kosztów.

    Args:
        description: Tekst opisu (lowercase).
        structured_extra: Kwota z pola "Czynsz (dodatkowo)" z sidebara OLX.

    Zwraca:
        (suma_extra_zl, lista_znalezionych_pozycji)

    Logika:
    1. Jeśli opis zawiera frazy "wszystko w cenie" itp. → extra = 0
    2. W przeciwnym razie szukaj kwot przy słowach kluczowych i sumuj
    3. Jeśli sidebar OLX zawiera "Czynsz (dodatkowo)" — używa wyższej wartości
       (structured vs regex) jako ostateczny wynik
    4. Zakresy (np. "200-400 zł") → bierze wyższą wartość (pesymistyczne podejście)
    """
    if not description:
        return 0, ["(brak opisu – koszty nieznane)"]

    # Normalizuj kwoty z przecinkiem dziesiętnym: "600,00 zł" → "600 zł"
    description = re.sub(r"(\d),\d{2}\s*z", r"\1 z", description)

    for pattern in WLICZONE_PATTERNS:
        if re.search(pattern, description, re.I):
            return 0, ["oplaty wliczone w cene"]

    found_items = []
    total_extra = 0
    used_spans = []  # dedup — ta sama pozycja w tekście może pasować do kilku wzorców

    for pattern in KOSZT_PATTERNS:
        for match in re.finditer(pattern, description, re.I):
            span = match.span()

            # Pomiń nakładające się dopasowania
            if any(s[0] < span[1] and span[0] < s[1] for s in used_spans):
                continue
            used_spans.append(span)

            groups = [g for g in match.groups() if g is not None]
            # Jeśli zakres (np. "200-400"), bierz wyższą
            amounts = []
            for g in groups:
                try:
                    amounts.append(int(re.sub(r"\s", "", g)))
                except ValueError:
                    pass

            if amounts:
                kwota = max(amounts)  # pesymistycznie: bierze wyższy koniec zakresu
                total_extra += kwota
                found_items.append(f"{match.group(0).strip()} → {kwota} zł")

    if not found_items:
        if structured_extra > 0:
            return structured_extra, [f"Czynsz (dodatkowo) z OLX: {structured_extra} zł"]
        return 0, ["(nie znaleziono wzmianek o dodatkowych kosztach)"]

    if structured_extra > total_extra:
        return structured_extra, [f"Czynsz (dodatkowo) z OLX: {structured_extra} zł"]

    return total_extra, found_items


LLM_PROMPT = """Przeanalizuj poniższy opis ogłoszenia o wynajmie mieszkania i wyodrębnij TYLKO dodatkowe koszty miesięczne, które NIE są wliczone w cenę najmu (np. czynsz administracyjny, media, opłaty eksploatacyjne, c.o., woda, śmieci, ogrzewanie, rachunki itp.).

Zasady:
- Jeśli opis mówi że wszystko jest wliczone w cenę lub media są wliczone — zwróć 0.
- Jeśli nie ma żadnych wzmianek o kosztach dodatkowych — zwróć 0.
- Dla zakresów (np. "700-800 zł") wybierz wyższą wartość.
- Jeśli koszt podany jest jako stawka per osoba (np. "920 zł/1 osoba; 1 110 zł/2 osoby; ...") — zawsze bierz wartość dla 1 osoby (pierwszą).
- Zsumuj wszystkie osobne pozycje kosztów (czynsz + media + zaliczki itp.).

Odpowiedz WYŁĄCZNIE w formacie JSON (bez markdown, bez wyjaśnień):
{"extra_koszt": <liczba całkowita w zł>, "pozycje": ["opis pozycji 1", "opis pozycji 2"]}

Opis ogłoszenia:
"""


def extract_extra_costs_llm(
    description: str,
    structured_extra: int = 0,
    llm_url: str = "http://localhost:11434",
    llm_model: str = "llama3",
    timeout: int = 60,
) -> tuple[int, list[str]]:
    """
    Analizuje opis ogłoszenia przez lokalny LLM (Ollama) w poszukiwaniu dodatkowych kosztów.
    Jeśli LLM nie odpowiada lub zwróci błędny JSON — fallback na regex.
    """
    if not description:
        return 0, ["(brak opisu – koszty nieznane)"]

    prompt = LLM_PROMPT + description[:3000]  # limit żeby nie przekroczyć kontekstu

    try:
        resp = requests.post(
            f"{llm_url.rstrip('/')}/api/generate",
            json={"model": llm_model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")

        # LLM czasem owija JSON tekstem — wyciągamy sam obiekt
        json_match = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
        if not json_match:
            raise ValueError(f"Brak JSON w odpowiedzi LLM: {raw[:200]}")

        data = json.loads(json_match.group(0))
        extra_koszt = int(data.get("extra_koszt", 0))
        pozycje = [str(p) for p in data.get("pozycje", [])]

        # Jeśli sidebar OLX podał wyższą kwotę, użyj jej
        if structured_extra > extra_koszt:
            return structured_extra, [f"Czynsz (dodatkowo) z OLX: {structured_extra} zł"]

        if extra_koszt == 0:
            return 0, ["(LLM: brak dodatkowych kosztów w opisie)"]

        return extra_koszt, pozycje

    except requests.RequestException as e:
        logger.warning("LLM niedostępny (%s) – fallback na regex", e)
        return extract_extra_costs(description, structured_extra)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        logger.warning("Błąd parsowania odpowiedzi LLM (%s) – fallback na regex", e)
        return extract_extra_costs(description, structured_extra)


def fetch_ollama_models(llm_url: str) -> list[str]:
    """Pobiera listę dostępnych modeli z Ollamy. Zwraca [] jeśli niedostępna."""
    try:
        resp = requests.get(
            f"{llm_url.rstrip('/')}/api/tags",
            timeout=5,
        )
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        return []


def has_next_page(soup: BeautifulSoup) -> bool:
    """Sprawdza czy istnieje następna strona wyników."""
    return bool(
        soup.find(attrs={"data-testid": "pagination-forward"})
        or soup.find(attrs={"data-cy": "pagination-forward"})
        or soup.find("a", attrs={"aria-label": re.compile(r"nast[eę]pna|next", re.I)})
    )


def load_seen(path: str) -> set[str]:
    """Wczytuje zbiór widzianych ID ogłoszeń z pliku JSON."""
    p = Path(path)
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Nie można wczytać pliku seen %s: %s", path, e)
    return set()


def save_seen(path: str, seen: set[str]) -> None:
    """Zapisuje zbiór widzianych ID ogłoszeń do pliku JSON."""
    Path(path).write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def print_header(config: dict) -> None:
    """Wypisuje nagłówek skanowania do stdout (tryb CLI)."""
    logger.info("='" * 28)
    logger.info("  OLX Scraper  [%s]", datetime.now().strftime("%d.%m.%Y %H:%M:%S"))
    dziel = f" / {config['dzielnica']}" if config.get("dzielnica") else ""
    logger.info("  Lokalizacja: %s%s", config["miasto"], dziel)
    logger.info("  Cena:   %s–%s zl/mies.", config["cena_min"], config["cena_max"])
    logger.info("  Metraz: %s–%s m2", config["metraz_min"], config["metraz_max"])
    if config.get("budzet_lacznie"):
        logger.info("  Budzet lacznie: max %s zl", config["budzet_lacznie"])
    logger.info("='" * 28)


def print_listing(listing: dict) -> None:
    """Wypisuje szczegóły ogłoszenia do loggera (tryb CLI)."""
    cena = f"{listing['price']} zl/mies." if listing["price"] else "cena ukryta"
    metraz = f"{listing['metraz']:.1f} m2" if listing["metraz"] else "? m2"
    logger.info("  [NOWE] %s", "-" * 44)
    logger.info("  Tytul:  %s", listing["title"])
    logger.info("  Czynsz: %s   Metraz: %s", cena, metraz)

    extra = listing.get("extra_koszt")
    if extra is not None and extra > 0:
        lacznie = (listing["price"] or 0) + extra
        logger.info("  Dodatki: %s zl/mies.", extra)
        logger.info("  Lacznie: %s zl/mies.", lacznie)
        for item in listing.get("extra_pozycje", []):
            logger.info("    - %s", item)
    elif listing.get("extra_pozycje"):
        for item in listing.get("extra_pozycje", []):
            logger.info("  Info: %s", item)

    if listing["lokalizacja"]:
        logger.info("  Lokalizacja: %s", listing["lokalizacja"])
    if listing["data"]:
        logger.info("  Data: %s", listing["data"])
    logger.info("  URL: %s", listing["url"])


# iMessage na macOS via AppleScript

def send_imessage(
    number: str,
    message: str,
    log_fn: Callable[[str], None] | None = None,
) -> None:
    """Wysyła wiadomość przez iMessage/Messages.app na macOS via AppleScript."""
    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)
        else:
            logger.info(msg)

    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    # Escapuj numer żeby uniknąć injection w kontekście AppleScript
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
    """Formatuje ogłoszenie jako wiadomość iMessage."""
    cena = f"{listing['price']} zl" if listing["price"] else "cena ukryta"
    metraz = f"{listing['metraz']:.0f}m2" if listing["metraz"] else ""

    extra = listing.get("extra_koszt", 0) or 0
    if extra > 0:
        lacznie = (listing["price"] or 0) + extra
        koszt_info = f"+ {extra} zl oplaty = {lacznie} zl lacznie"
    else:
        koszt_info = ""

    parts = [p for p in [cena, metraz, listing["lokalizacja"]] if p]
    msg = f"Nowe na OLX:\n{listing['title']}\n{' | '.join(parts)}\n"
    if koszt_info:
        msg += f"{koszt_info}\n"
    msg += listing["url"]
    return msg


#Zasadnicza funkcja skanująca i powiadamiająca o nowych ogłoszeniach
def scrape_once(config: dict, seen: set[str]) -> int:
    """Skanuje OLX i powiadamia o nowych ogłoszeniach. Zwraca liczbę nowych."""
    print_header(config)
    new_count = 0

    max_stron = config["max_stron"]
    wszystkie = max_stron == "all"
    limit = MAX_PAGES_UNLIMITED if wszystkie else int(max_stron)

    for page in range(1, limit + 1):
        label = f"{page}/{'all' if wszystkie else limit}"
        url = build_url(config, page)
        logger.info("  Strona %s: %s", label, url)

        soup = fetch_page(url)
        if not soup:
            break

        listings = parse_listings(soup)
        logger.info("  Ogłoszeń na stronie: %d", len(listings))

        if not listings:
            logger.info("  Brak wyników – zatrzymuję.")
            break

        for listing in listings:
            if listing["id"] in seen:
                continue

            # Filtr metrażu (lokalny)
            if listing["metraz"] is not None:
                if not (config["metraz_min"] <= listing["metraz"] <= config["metraz_max"]):
                    seen.add(listing["id"])
                    continue

            # Pobierz opis i wyciągnij dodatkowe koszty
            budzet = config.get("budzet_lacznie")
            if budzet and listing["price"] is not None:
                logger.info("    ↳ Sprawdzam opis: %s...", listing["url"].split("/")[-1][:40])
                opis, structured_extra = fetch_detail(listing["url"])
                extra_koszt, extra_pozycje = extract_extra_costs(opis, structured_extra)
                listing["extra_koszt"] = extra_koszt
                listing["extra_pozycje"] = extra_pozycje

                lacznie = listing["price"] + extra_koszt
                if lacznie > budzet:
                    logger.info("  odrzucone (%s + %s = %s zl > limit %s zl)",
                                listing["price"], extra_koszt, lacznie, budzet)
                    seen.add(listing["id"])
                    time.sleep(DELAY_BETWEEN_REQUESTS)
                    continue
                else:
                    logger.info("  ok (%s zl lacznie, limit: %s zl)", lacznie, budzet)
                time.sleep(DELAY_BETWEEN_REQUESTS)
            else:
                listing["extra_koszt"] = None
                listing["extra_pozycje"] = []

            seen.add(listing["id"])
            new_count += 1
            print_listing(listing)

            if config.get("wyslij_imessage") and config.get("imessage_numer"):
                msg = format_imessage(listing)
                send_imessage(config["imessage_numer"], msg)

        if not has_next_page(soup):
            logger.info("  Ostatnia strona – koniec.")
            break

        time.sleep(DELAY_BETWEEN_PAGES)

    return new_count


def main() -> None:
    """Punkt wejścia CLI — parsuje argumenty i uruchamia skanowanie."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        description="OLX.pl scraper – wynajem mieszkań z powiadomieniami iMessage"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Włącz szczegółowe logi (DEBUG)"
    )
    parser.add_argument(
        "--interval", type=int, default=0,
        help="Interwał w sekundach (0=jednorazowo, 86400=raz na dobę)"
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Wyczyść pamięć widzianych ogłoszeń"
    )
    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    seen_path = CONFIG["seen_file"]

    if args.reset:
        Path(seen_path).unlink(missing_ok=True)
        logger.info("Pamiec wyczyszczona.")

    seen = load_seen(seen_path)
    logger.info("Zapamiętanych ogloszen: %d", len(seen))

    if args.interval > 0:
        h = args.interval / 3600
        logger.info("Tryb ciagly - sprawdzanie co %.1fh (Ctrl+C = stop)", h)
        try:
            while True:
                nowe = scrape_once(CONFIG, seen)
                save_seen(seen_path, seen)
                nastepne = datetime.fromtimestamp(time.time() + args.interval)
                logger.info("Nowych ogloszen: %d | Nastepne: %s",
                            nowe, nastepne.strftime("%d.%m.%Y %H:%M"))
                time.sleep(args.interval)
        except KeyboardInterrupt:
            save_seen(seen_path, seen)
            logger.info("Zatrzymano. Pamiec zapisana.")
    else:
        try:
            nowe = scrape_once(CONFIG, seen)
        finally:
            save_seen(seen_path, seen)
        logger.info("Gotowe. Nowych: %d | Lacznie zapamiętanych: %d",
                    nowe, len(seen))


if __name__ == "__main__":
    main()
