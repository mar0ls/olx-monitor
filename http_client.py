#!/usr/bin/env python3
"""
http_client.py
Wspólna warstwa HTTP dla scraperów OLX.

Od ~2026-09 OLX stoi za CloudFront/WAF, który blokuje (403 "Request blocked")
każdego klienta o nieprzeglądarkowym odcisku TLS — również `requests` i `curl`,
niezależnie od nagłówków. Dlatego zapytania idą przez `curl_cffi`, który
podszywa się pod odcisk TLS/JA3 prawdziwej przeglądarki.

Gdy `curl_cffi` nie jest dostępny, moduł po cichu wraca do `requests`
(zadziała dla serwisów bez WAF, np. otodom).

Wyjątki są normalizowane do typów z `requests`, żeby kod wywołujący mógł
łapać `requests.HTTPError` / `requests.RequestException` niezależnie od backendu.
"""

import random

import requests

try:
    from curl_cffi import requests as _curl
    from curl_cffi.requests import exceptions as _curl_exc
    HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover - zależy od środowiska
    _curl = None
    _curl_exc = None
    HAS_CURL_CFFI = False

# Profile przeglądarek do podszywania się pod odcisk TLS.
_IMPERSONATE_PROFILES = ["chrome", "safari", "firefox"]

# Zmienia język treści na polskiej stronie, nie odcisk klienta.
_EXTRA_HEADERS = {"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7"}

# Używane tylko w fallbacku na requests — profil impersonate ma własne, nowsze.
_FALLBACK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Te nagłówki ustawia profil impersonate, spójnie z odciskiem TLS. Wersje od
# wywołującego bywają starsze (np. bez br/zstd) i rozjeżdżają odcisk z UA.
_FINGERPRINT_HEADERS = {
    "user-agent", "accept", "accept-encoding", "connection", "dnt",
    "upgrade-insecure-requests", "priority",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
}


class _CurlResponseAdapter:
    """Odpowiedź curl_cffi w interfejsie requests.Response.

    `raise_for_status()` podnosi `requests.HTTPError` z ustawionym `.response`,
    nie wyjątek curl_cffi, żeby `except requests.HTTPError` w scraperach
    działało bez zmian.
    """

    def __init__(self, resp):
        self._resp = resp

    def __getattr__(self, name):
        return getattr(self._resp, name)

    def raise_for_status(self):
        if 400 <= self._resp.status_code < 600:
            err = requests.HTTPError(
                f"{self._resp.status_code} dla {self._resp.url}",
                response=self._resp,
            )
            raise err
        return None


def get(url: str, headers: dict | None = None, timeout: int = 15):
    """Pobiera URL, podszywając się pod przeglądarkę. Zwraca obiekt odpowiedzi.

    Nie wywołuje `raise_for_status()` — robi to kod wywołujący, tak jak przy
    zwykłym `requests.get`.
    """
    if not HAS_CURL_CFFI:
        # Bez podszywania się ręczne nagłówki pomagają, więc je zostawiamy.
        merged = {**_FALLBACK_HEADERS, **_EXTRA_HEADERS}
        if headers:
            merged.update(headers)
        return requests.get(url, headers=merged, timeout=timeout)

    merged = dict(_EXTRA_HEADERS)
    if headers:
        merged.update({
            k: v for k, v in headers.items()
            if k.lower() not in _FINGERPRINT_HEADERS
        })

    try:
        resp = _curl.get(
            url,
            headers=merged,
            timeout=timeout,
            impersonate=random.choice(_IMPERSONATE_PROFILES),
        )
    except _curl_exc.CurlError as e:
        # Kod wywołujący łapie wyjątki requests, nie curl_cffi.
        raise requests.RequestException(str(e)) from e

    return _CurlResponseAdapter(resp)
