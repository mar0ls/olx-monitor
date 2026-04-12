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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import olx_scraper
from miner_id import DEFAULT_CITIES, fetch_districts

SCRAPER_FILE = Path(__file__).parent / "olx_scraper.py"

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
    return 1


if __name__ == "__main__":
    sys.exit(main())
