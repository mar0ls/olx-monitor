#!/usr/bin/env python3
"""
otodom_scraper.py
Parser szczegółów ogłoszenia z otodom.pl.

Otodom to aplikacja Next.js — dane ogłoszenia są osadzone w tagu
<script id="__NEXT_DATA__"> jako JSON, co pozwala niezawodnie odczytać
czynsz administracyjny (klucz 'rent'), opis i inne parametry
bez kruchego parsowania HTML.
"""

import json
import logging
import re

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_TIMEOUT = 15


def fetch_otodom_detail(url: str) -> tuple[str, int]:
    """
    Pobiera opis i czynsz administracyjny z ogłoszenia otodom.pl.

    Zwraca:
        (opis_tekst, czynsz_administracyjny) — opis jako lowercase string,
        czynsz_administracyjny jako int (0 jeśli brak).

    Dane wyciągane z __NEXT_DATA__ (JSON osadzony w stronie Next.js):
        - ad.description       → opis ogłoszenia (HTML, stripowany do tekstu)
        - ad.characteristics   → lista parametrów, w tym rent (czynsz adm.)
        - ad.topInformation    → skrócona lista parametrów wyświetlana w nagłówku
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        next_data_tag = soup.find("script", id="__NEXT_DATA__")
        if not next_data_tag:
            logger.warning("Brak __NEXT_DATA__ w ogłoszeniu otodom: %s", url)
            return "", 0

        data = json.loads(next_data_tag.string)
        ad = data.get("props", {}).get("pageProps", {}).get("ad", {})

        # Opis — może zawierać HTML (<p>, <br> itp.)
        raw_desc = ad.get("description", "")
        if raw_desc:
            desc_soup = BeautifulSoup(raw_desc, "html.parser")
            desc_text = desc_soup.get_text(" ", strip=True).lower()
        else:
            desc_text = ""

        # Czynsz administracyjny z characteristics (wartość numeryczna)
        rent = 0
        for ch in ad.get("characteristics", []):
            if ch.get("key") == "rent":
                try:
                    rent = int(re.sub(r"\s", "", ch.get("value", "0")))
                except (ValueError, TypeError):
                    pass
                break

        # Fallback: topInformation (wartość tekstowa "800 zł/miesiąc")
        if rent == 0:
            for info in ad.get("topInformation", []):
                if info.get("label") == "rent":
                    values = info.get("values", [])
                    if values:
                        m = re.search(r"(\d[\d\s]*)", values[0])
                        if m:
                            try:
                                rent = int(re.sub(r"\s", "", m.group(1)))
                            except ValueError:
                                pass
                    break

        return desc_text, rent

    except requests.HTTPError as e:
        logger.error("Błąd HTTP %s dla %s",
                     e.response.status_code if e.response else "?", url)
        return "", 0
    except requests.RequestException as e:
        logger.warning("Nie można pobrać ogłoszenia otodom %s: %s", url, e)
        return "", 0
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Błąd parsowania __NEXT_DATA__ otodom %s: %s", url, e)
        return "", 0
