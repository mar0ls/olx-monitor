#!/usr/bin/env python3
"""
miner_id.py
Narzędzie pomocnicze – pobiera aktualne district_id dzielnic z OLX.pl
i wypisuje je w formacie gotowym do wklejenia do CITY_DISTRICT_DISPLAY
w olx_scraper.py.

Wymagania:
    pip install requests beautifulsoup4

Uruchomienie (z katalogu projektu):
    python miner_id.py

    # Wybrane miasta (można podać kilka):
    python miner_id.py warszawa gdansk krakow

Wynik: wydruk do stdout + podsumowanie miast bez dzielnic.
"""

import random
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

_USER_AGENTS = [
    # Chrome – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Firefox – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    # Firefox – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Edge – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Safari – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]


def _make_headers() -> dict:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
    }

# Wszystkie miasta z OLX Polska (klucze URL używane przez serwis)
DEFAULT_CITIES = [
    "warszawa",
    "krakow",
    "wroclaw",
    "poznan",
    "gdansk",
    "gdynia",
    "sopot",
    "lodz",
    "katowice",
    "szczecin",
    "bialystok",
    "czestochowa",
    "bydgoszcz",
    "lublin",
    "rzeszow",
    "olsztyn",
    "kielce",
    "opole",
    "torun",
    "zielona-gora",
    "radom",
]

BASE = "https://www.olx.pl/nieruchomosci/mieszkania/wynajem/{city}/"


def fetch_districts(city: str) -> dict[str, int]:
    """Pobiera mapę {nazwa_dzielnicy: district_id} dla podanego miasta."""
    url = BASE.format(city=city)
    try:
        resp = requests.get(url, headers=_make_headers(), timeout=15)
        resp.raise_for_status()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response else "?"
        print(f"  [HTTP {code}] {url}", file=sys.stderr)
        return {}
    except requests.RequestException as e:
        print(f"  [ERR] {e}", file=sys.stderr)
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    ids_found: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"district_id(?:%5D|])\s*=\s*(\d+)", href)
        if m:
            did = int(m.group(1))
            label = a.get_text(strip=True)
            # OLX dołącza liczbę ogłoszeń w nawiasie, np. "Ursynów (49)" — odcinamy
            label = re.sub(r"\s*\(\d+\)\s*$", "", label).strip()
            if label:
                ids_found[label] = did
    return ids_found


def main() -> None:
    cities = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_CITIES
    no_districts: list[str] = []

    for city in cities:
        print(f"\n=== {city.upper()} ===")
        districts = fetch_districts(city)
        if not districts:
            print("  (brak filtrów dzielnic – OLX nie obsługuje podziału lub błąd HTTP)")
            no_districts.append(city)
        else:
            for name, did in sorted(districts.items()):
                print(f'        "{name}": {did},')
        time.sleep(1.5)

    if no_districts:
        print("\n--- Miasta bez dzielnic (tylko całe miasto): ---")
        for c in no_districts:
            print(f"  {c}")


if __name__ == "__main__":
    main()
