#!/usr/bin/env python3
"""
update_districts.py
Porównuje aktualne district_id z OLX z wartościami w olx_scraper.py.
Jeśli są różnice, aktualizuje CITY_DISTRICT_DISPLAY i wychodzi z kodem 1.
Brak zmian → kod 0. Błąd sieci/parsowania → kod 2.

Używany przez GitHub Action check-districts.yml.
"""

import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import olx_scraper
from miner_id import DEFAULT_CITIES, fetch_districts

SCRAPER_FILE = Path(__file__).parent / "olx_scraper.py"
README_FILE  = Path(__file__).parent / "README.md"

CITY_DISPLAY_NAMES = {
    "warszawa":    "Warszawa",
    "krakow":      "Kraków",
    "wroclaw":     "Wrocław",
    "poznan":      "Poznań",
    "gdansk":      "Gdańsk",
    "gdynia":      "Gdynia",
    "sopot":       "Sopot",
    "lodz":        "Łódź",
    "katowice":    "Katowice",
    "szczecin":    "Szczecin",
    "bialystok":   "Białystok",
    "czestochowa": "Częstochowa",
}

# Miasta bez dzielnic na OLX — nie próbujemy ich scrapować
CITIES_WITHOUT_DISTRICTS = {
    "bydgoszcz", "lublin", "radom", "rzeszow", "olsztyn",
    "kielce", "opole", "torun", "zielona-gora",
}


def build_dict_block(all_districts: dict[str, dict[str, int]]) -> str:
    lines = ["CITY_DISTRICT_DISPLAY: dict[str, dict[str, int]] = {"]
    for city, districts in all_districts.items():
        lines.append(f'    "{city}": {{')
        for name, did in sorted(districts.items()):
            lines.append(f'        "{name}": {did},')
        lines.append("    },")
    lines.append("}")
    return "\n".join(lines)


def main() -> int:
    cities_to_check = [c for c in DEFAULT_CITIES if c not in CITIES_WITHOUT_DISTRICTS]

    fetched: dict[str, dict[str, int]] = {}
    errors: list[str] = []

    for city in cities_to_check:
        districts = fetch_districts(city)
        if districts:
            fetched[city] = districts
        else:
            errors.append(city)
        time.sleep(1.5)

    if errors:
        print(f"WARN: brak danych dla: {', '.join(errors)}", file=sys.stderr)

    # Jeśli OLX nic nie zwrócił (zablokowany) — nie ruszamy pliku
    if not fetched:
        print("ERR: OLX nie zwrócił żadnych danych — prawdopodobnie blokada IP.", file=sys.stderr)
        return 2

    current = olx_scraper.CITY_DISTRICT_DISPLAY
    changed: list[str] = []

    for city, districts in fetched.items():
        if districts != current.get(city, {}):
            changed.append(city)

    if not changed:
        print("Brak zmian w district_id.")
        return 0

    print(f"Zmiany w: {', '.join(changed)}")
    for city in changed:
        old = current.get(city, {})
        new = fetched[city]
        added = set(new) - set(old)
        removed = set(old) - set(new)
        modified = {n for n in new if n in old and new[n] != old[n]}
        if added:
            print(f"  {city}: +{len(added)} dzielnic")
        if removed:
            print(f"  {city}: -{len(removed)} dzielnic")
        if modified:
            print(f"  {city}: ~{len(modified)} zmienionych ID")

    # zachowaj miasta których nie scrapowaliśmy, nadpisz te z nowymi danymi
    new_districts = dict(current)
    for city, districts in fetched.items():
        new_districts[city] = districts

    new_block = build_dict_block(new_districts)

    source = SCRAPER_FILE.read_text(encoding="utf-8")
    patched = re.sub(
        r"^CITY_DISTRICT_DISPLAY:.*?^\}",
        new_block,
        source,
        flags=re.MULTILINE | re.DOTALL,
    )

    if patched == source:
        print("ERR: nie znaleziono bloku CITY_DISTRICT_DISPLAY w pliku.", file=sys.stderr)
        return 2

    SCRAPER_FILE.write_text(patched, encoding="utf-8")
    print("Zaktualizowano olx_scraper.py.")

    _update_readme(new_districts)
    return 1


def _update_readme(districts: dict[str, dict[str, int]]) -> None:
    if not README_FILE.exists():
        return

    readme = README_FILE.read_text(encoding="utf-8")
    month_pl = [
        "", "styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec",
        "lipiec", "sierpień", "wrzesień", "październik", "listopad", "grudzień",
    ]
    now = datetime.now()
    date_str = f"{month_pl[now.month]} {now.year}"

    # Zaktualizuj datę w nagłówku sekcji
    readme = re.sub(
        r"(Scraper zawiera wbudowaną mapę `district_id` dla \d+ polskich miast \()([^)]+)(\)\.)",
        lambda m: f"{m.group(1)}{date_str}{m.group(3)}",
        readme,
    )

    # Zaktualizuj liczby dzielnic w tabelce
    for city, name in CITY_DISPLAY_NAMES.items():
        count = len(districts.get(city, {}))
        if count:
            readme = re.sub(
                rf"(\| {re.escape(name)} \| `{re.escape(city)}` \| )\d+( \|)",
                rf"\g<1>{count}\2",
                readme,
            )

    README_FILE.write_text(readme, encoding="utf-8")
    print("Zaktualizowano README.md (liczby dzielnic i data).")


if __name__ == "__main__":
    sys.exit(main())
