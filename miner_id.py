#!/usr/bin/env python3
"""
miner_id.py
Narzędzie pomocnicze – pobiera aktualne district_id dzielnic z OLX.pl
i wypisuje je w formacie gotowym do wklejenia do CITY_DISTRICT_DISPLAY
w olx_scraper.py.

Wymagania:
    pip install requests beautifulsoup4 curl_cffi

Uruchomienie (z katalogu projektu):
    python miner_id.py

    # Wybrane miasta (można podać kilka):
    python miner_id.py warszawa gdansk krakow

Wynik: wydruk do stdout + podsumowanie miast bez dzielnic.
"""

import re
import sys
import time

import requests
from bs4 import BeautifulSoup

import http_client

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
        resp = http_client.get(url, timeout=15)
        resp.raise_for_status()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
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
