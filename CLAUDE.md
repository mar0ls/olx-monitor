# Konwencje projektu

## Commity

NIE dodawaj trailera `Co-Authored-By` ani żadnej innej wzmianki o Claude/Anthropic
w komunikatach commitów. Właściciel repo nie życzy sobie Claude'a jako kontrybutora.

Styl komunikatów: krótki temat, opcjonalnie 1–2 linie treści. Bez wypunktowanych
bloków i wielolinijkowych opisów.

## Gałęzie

Wszystko wchodzi przez `dev`; `main` dostaje zmiany przez auto-PR (squash).
Po każdym landowaniu i po commicie crona `scripts/sync_dev_with_main.sh`
przestawia `dev` na `main` — nie scalaj tego ręcznie.

## Sieć

OLX jest za CloudFront/WAF blokującym nieprzeglądarkowe odciski TLS.
Ruch do OLX idzie przez `http_client.py` (curl_cffi z impersonate), nie przez
`requests`. Nie przekazuj tam ręcznych nagłówków odciskowych — profil ustawia je sam.
Otodom nie jest blokowany i zostaje na `requests`.
