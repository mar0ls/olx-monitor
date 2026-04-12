"""
test_olx_scraper.py
Testy jednostkowe dla olx_scraper.py

Uruchomienie:
    pip install pytest
    pytest test_olx_scraper.py -v
"""

import json
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

import olx_scraper as scraper

# ─────────────────────────────────────────────────────────────
#  Pomocnicze fabryki HTML
# ─────────────────────────────────────────────────────────────

def _make_card(
    title="Mieszkanie dwupokojowe Mokotów",
    price_text="3 000 zł",
    location_date="Warszawa, Mokotów - dzisiaj",
    area_text="50 m²",
    url="https://www.olx.pl/oferta/mieszkanie-CID3-IDabc123.html",
    data_cy_price=True,
) -> str:
    """Zwraca HTML-a pojedynczej karty ogłoszenia (format OLX kwiecień 2026)."""
    price_html = (
        f'<span data-testid="ad-price">{price_text}</span>'
        if data_cy_price
        else f'<span>{price_text}</span>'
    )
    return f"""
    <div data-cy="l-card">
        <a href="{url}">
            <h4>{title}</h4>
        </a>
        {price_html}
        <p data-testid="location-date">{location_date}</p>
        <span>{area_text}</span>
    </div>
    """


def _soup_from_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


# ─────────────────────────────────────────────────────────────
#  build_url
# ─────────────────────────────────────────────────────────────

class TestBuildUrl:
    BASE_CONFIG = {
        "miasto": "warszawa",
        "cena_min": 2000,
        "cena_max": 4500,
        "metraz_min": 35,
        "metraz_max": 70,
    }

    def test_page_1_no_page_param(self):
        url = scraper.build_url(self.BASE_CONFIG, page=1)
        assert "page=" not in url

    def test_page_2_has_page_param(self):
        url = scraper.build_url(self.BASE_CONFIG, page=2)
        assert "page=2" in url

    def test_miasto_in_url(self):
        url = scraper.build_url(self.BASE_CONFIG)
        assert "warszawa" in url

    def test_district_id_in_url_when_set(self):
        """Numeryczny district_id ląduje jako parametr zapytania."""
        cfg = {**self.BASE_CONFIG, "district_id": 373}
        url = scraper.build_url(cfg)
        assert "district_id%5D=373" in url

    def test_dzielnica_name_resolves_to_district_id(self):
        """Nazwa dzielnicy ('mokotow') jest zamieniana na district_id=353."""
        cfg = {**self.BASE_CONFIG, "dzielnica": "mokotow"}
        url = scraper.build_url(cfg)
        assert "district_id%5D=353" in url

    def test_dzielnica_with_polish_chars_resolves(self):
        """Nazwa z polskimi znakami ('Mokotów') też jest rozpoznawana."""
        cfg = {**self.BASE_CONFIG, "dzielnica": "Mokotów"}
        url = scraper.build_url(cfg)
        assert "district_id%5D=353" in url

    def test_no_district_id_when_not_set(self):
        """Brak district_id gdy nie jest skonfigurowany."""
        url = scraper.build_url(self.BASE_CONFIG)
        assert "district_id" not in url

    def test_price_filters_in_url(self):
        url = scraper.build_url(self.BASE_CONFIG)
        assert "2000" in url
        assert "4500" in url

    def test_metraz_filters_in_url(self):
        url = scraper.build_url(self.BASE_CONFIG)
        assert "35" in url
        assert "70" in url

    def test_olx_domain(self):
        url = scraper.build_url(self.BASE_CONFIG)
        assert url.startswith("https://www.olx.pl")


# ─────────────────────────────────────────────────────────────
#  get_districts_for_city
# ─────────────────────────────────────────────────────────────

class TestGetDistrictsForCity:
    def test_warszawa_contains_ursynow(self):
        d = scraper.get_districts_for_city("warszawa")
        assert "Ursynów" in d
        assert d["Ursynów"] == 373

    def test_warszawa_contains_mokotow(self):
        d = scraper.get_districts_for_city("warszawa")
        assert d.get("Mokotów") == 353

    def test_krakow_has_districts(self):
        d = scraper.get_districts_for_city("krakow")
        assert len(d) > 5

    def test_lodz_has_districts(self):
        d = scraper.get_districts_for_city("lodz")
        assert "Śródmieście" in d

    def test_unknown_city_returns_empty(self):
        d = scraper.get_districts_for_city("nieznane-miasto")
        assert d == {}

    def test_case_insensitive_lookup(self):
        d1 = scraper.get_districts_for_city("warszawa")
        d2 = scraper.get_districts_for_city("Warszawa")
        assert d1 == d2

    def test_result_is_sorted(self):
        d = scraper.get_districts_for_city("warszawa")
        keys = list(d.keys())
        assert keys == sorted(keys)


# ─────────────────────────────────────────────────────────────
#  extract_id_from_url
# ─────────────────────────────────────────────────────────────

class TestExtractIdFromUrl:
    def test_standard_olx_url(self):
        url = "https://www.olx.pl/oferta/mieszkanie-2-pokoje-CID3-ID19HnE4.html"
        assert scraper.extract_id_from_url(url) == "ID19HnE4"

    def test_url_without_id_pattern(self):
        url = "https://www.olx.pl/oferta/mieszkanie-bez-id/"
        result = scraper.extract_id_from_url(url)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_alphanumeric_id(self):
        url = "https://www.olx.pl/oferta/test-CID3-IDAbCd12.html"
        assert scraper.extract_id_from_url(url) == "IDAbCd12"


# ─────────────────────────────────────────────────────────────
#  parse_price
# ─────────────────────────────────────────────────────────────

class TestParsePrice:
    def test_basic_zloty(self):
        assert scraper.parse_price("3000 zł") == 3000

    def test_space_separator(self):
        assert scraper.parse_price("3 000 zł") == 3000

    def test_nbsp_separator(self):
        assert scraper.parse_price("3\xa0000 zł") == 3000

    def test_pln_currency(self):
        assert scraper.parse_price("4500 PLN") == 4500

    def test_zl_latin(self):
        assert scraper.parse_price("2500 zl") == 2500

    def test_zlotych(self):
        assert scraper.parse_price("3500 złotych") == 3500

    def test_with_trailing_text(self):
        # "4 500 złdo negocjacji" – część tekstu bez spacji przed "do"
        result = scraper.parse_price("4 500 złdo negocjacji")
        assert result == 4500

    def test_empty_string_returns_none(self):
        assert scraper.parse_price("") is None

    def test_no_digits_returns_none(self):
        assert scraper.parse_price("brak ceny") is None

    def test_price_1000(self):
        assert scraper.parse_price("1 000 zł") == 1000

    def test_price_10000(self):
        assert scraper.parse_price("10 000 zł") == 10000


# ─────────────────────────────────────────────────────────────
#  parse_metraz
# ─────────────────────────────────────────────────────────────

class TestParseMetraz:
    def test_basic_m2(self):
        assert scraper.parse_metraz("65 m²") == 65.0

    def test_m2_no_space(self):
        assert scraper.parse_metraz("65m²") == 65.0

    def test_decimal_comma(self):
        assert scraper.parse_metraz("65,5 m²") == 65.5

    def test_decimal_dot(self):
        assert scraper.parse_metraz("65.5m2") == 65.5

    def test_m2_suffix(self):
        assert scraper.parse_metraz("50 m2") == 50.0

    def test_empty_returns_none(self):
        assert scraper.parse_metraz("") is None

    def test_no_m2_returns_none(self):
        assert scraper.parse_metraz("mieszkanie 3-pokojowe") is None

    def test_large_area(self):
        assert scraper.parse_metraz("200 m²") == 200.0


# ─────────────────────────────────────────────────────────────
#  parse_listings
# ─────────────────────────────────────────────────────────────

class TestParseListings:
    def test_single_listing_parsed(self):
        html = "<html><body>" + _make_card() + "</body></html>"
        soup = _soup_from_html(html)
        listings = scraper.parse_listings(soup)
        assert len(listings) == 1

    def test_listing_has_required_keys(self):
        html = "<html><body>" + _make_card() + "</body></html>"
        soup = _soup_from_html(html)
        listing = scraper.parse_listings(soup)[0]
        for key in ("id", "title", "price", "metraz", "lokalizacja", "data", "url"):
            assert key in listing

    def test_listing_title(self):
        html = "<html><body>" + _make_card(title="Kawalerka centrum") + "</body></html>"
        soup = _soup_from_html(html)
        listing = scraper.parse_listings(soup)[0]
        assert listing["title"] == "Kawalerka centrum"

    def test_listing_price_parsed(self):
        html = "<html><body>" + _make_card(price_text="2 500 zł") + "</body></html>"
        soup = _soup_from_html(html)
        listing = scraper.parse_listings(soup)[0]
        assert listing["price"] == 2500

    def test_listing_metraz_parsed(self):
        html = "<html><body>" + _make_card(area_text="48 m²") + "</body></html>"
        soup = _soup_from_html(html)
        listing = scraper.parse_listings(soup)[0]
        assert listing["metraz"] == 48.0

    def test_listing_lokalizacja_parsed(self):
        html = "<html><body>" + _make_card(location_date="Kraków, Śródmieście - dzisiaj") + "</body></html>"
        soup = _soup_from_html(html)
        listing = scraper.parse_listings(soup)[0]
        assert "Kraków" in listing["lokalizacja"]

    def test_listing_url_absolute(self):
        html = "<html><body>" + _make_card() + "</body></html>"
        soup = _soup_from_html(html)
        listing = scraper.parse_listings(soup)[0]
        assert listing["url"].startswith("http")

    def test_relative_url_becomes_absolute(self):
        card_html = """
        <div data-cy="l-card">
            <a href="/oferta/test-CID3-IDxyz.html">
                <h4>Tytuł</h4>
            </a>
            <span data-testid="ad-price">2000 zł</span>
            <p data-testid="location-date">Warszawa - dzisiaj</p>
        </div>
        """
        soup = _soup_from_html(card_html)
        listing = scraper.parse_listings(soup)[0]
        assert listing["url"].startswith("https://www.olx.pl")

    def test_multiple_cards_parsed(self):
        html = "<html><body>" + _make_card() * 5 + "</body></html>"
        soup = _soup_from_html(html)
        listings = scraper.parse_listings(soup)
        assert len(listings) == 5

    def test_empty_page_returns_empty_list(self):
        soup = _soup_from_html("<html><body></body></html>")
        assert scraper.parse_listings(soup) == []

    def test_listing_id_extracted(self):
        html = "<html><body>" + _make_card(
            url="https://www.olx.pl/oferta/mieszkanie-CID3-IDtest99.html"
        ) + "</body></html>"
        soup = _soup_from_html(html)
        listing = scraper.parse_listings(soup)[0]
        assert listing["id"] == "IDtest99"


# ─────────────────────────────────────────────────────────────
#  extract_extra_costs
# ─────────────────────────────────────────────────────────────

class TestExtractExtraCosts:
    def test_empty_description(self):
        total, items = scraper.extract_extra_costs("")
        assert total == 0
        assert len(items) == 1  # komunikat "(brak opisu – koszty nieznane)"

    def test_all_included_phrase(self):
        desc = "wszystko w cenie, rachunki i media wliczone w czynsz"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 0
        assert "wliczon" in items[0].lower() or "wliczone" in desc.lower()

    def test_media_wliczone(self):
        total, items = scraper.extract_extra_costs("media wliczone w cenę najmu.")
        assert total == 0

    def test_rachunki_wliczone(self):
        total, items = scraper.extract_extra_costs("rachunki wliczone.")
        assert total == 0

    def test_bez_dodatkowych_oplat(self):
        total, items = scraper.extract_extra_costs("bez dodatkowych opłat")
        assert total == 0

    def test_czynsz_administracyjny(self):
        desc = "czynsz administracyjny: 400 zł miesięcznie"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 400

    def test_media_kwota(self):
        desc = "media ok. 250 zł miesięcznie"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 250

    def test_rachunki_zakres_bierz_wyzszą(self):
        desc = "rachunki około 200-400 zł"
        total, items = scraper.extract_extra_costs(desc)
        # Podejście pesymistyczne: bierz wyższą
        assert total == 400

    def test_multiple_costs_sumowane(self):
        desc = "czynsz administracyjny: 300 zł, media ok. 200 zł"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 500
        assert len(items) == 2

    def test_no_extra_costs_mentioned(self):
        desc = "Piękne mieszkanie w centrum, cicha okolica, 3 pokoje."
        total, items = scraper.extract_extra_costs(desc)
        assert total == 0
        assert len(items) == 1  # komunikat "(nie znaleziono...)"

    def test_co_oplata(self):
        desc = "c.o. 150 zł w sezonie"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 150

    def test_returns_tuple(self):
        result = scraper.extract_extra_costs("brak informacji")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_nnn_zl_za_oplaty_pattern(self):
        """Regex: '645 zł za opłaty administracyjne'."""
        desc = "całkowity koszt wynajmu wynosi 3100 zł (2455 zł za wynajem + 645 zł za opłaty administracyjne)"
        total, items = scraper.extract_extra_costs(desc)
        assert total >= 645

    def test_structured_extra_used_when_regex_finds_nothing(self):
        """Sidebar 'Czynsz (dodatkowo)' gdy opis nie ma kwot."""
        desc = "mieszkanie gotowe do wprowadzenia. do ceny doliczyć prąd."
        total, items = scraper.extract_extra_costs(desc, structured_extra=967)
        assert total == 967
        assert any("967" in i for i in items)

    def test_structured_extra_wins_when_higher(self):
        """Sidebar wyższy niż regex → używamy sidebara."""
        desc = "czynsz administracyjny: 300 zł"
        total, items = scraper.extract_extra_costs(desc, structured_extra=800)
        assert total == 800

    def test_regex_wins_when_higher_than_structured(self):
        """Regex wyższy niż sidebar → używamy regex."""
        desc = "czynsz administracyjny: 900 zł"
        total, items = scraper.extract_extra_costs(desc, structured_extra=500)
        assert total == 900

    def test_ogrzewanie_pattern(self):
        desc = "ogrzewanie ok. 150 zł miesięcznie"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 150

    def test_woda_pattern(self):
        desc = "zimna woda 50 zł, ciepła woda 80 zł"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 130

    def test_otodom_cena_plus_oplaty_pattern(self):
        """Format otodom: '2400 zł + 850 zł (opłaty administracyjne)'."""
        desc = "koszt najmu: 2400 zł + 850 zł (opłaty administracyjne)"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 850

    def test_plus_czynsz_w_nawiasie_pattern(self):
        """Format: '+czynsz (opłata administracyjna): ok. 700 zł'."""
        desc = "najem w wysokości 2500 zł +czynsz (opłata administracyjna): ok. 700 zł"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 700

    def test_czynsz_administracji_range(self):
        """Format: 'czynsz administracji 1100-1150' bez 'zł'."""
        desc = "czynsz administracji 1100-1150"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 1150

    def test_czynsz_do_administracji_per_person(self):
        """Format z OLX z wieloma stawkami per osoba — bierze pierwszą."""
        desc = "czynsz do administracji: 920 zł/1 osoba; 1 110 zł/2 osoby; 1 290 zł/3 osoby"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 920

    def test_zaliczka_na_energie(self):
        desc = "zaliczka na energię: 150 zł/1 osoba; 190 zł/2 osoby"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 150

    def test_czynsz_do_administracji_plus_zaliczka(self):
        desc = "czynsz do administracji: 920 zł/1 osoba; 1 110 zł/2 osoby zaliczka na energię: 150 zł/1 osoba"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 1070

    def test_czynsz_decimal_comma(self):
        desc = "+ czynsz 600,00 zł"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 600

    def test_rachunki_za_media_bez_zl(self):
        desc = "+ ok 250 rachunki za media"
        total, items = scraper.extract_extra_costs(desc)
        assert total == 250

    def test_czynsz_decimal_plus_rachunki_bez_zl(self):
        # format z ogłoszenia: "+ czynsz 600,00 zł\n+ ok 250 rachunki za media"
        desc = "+ czynsz 600,00 zł\n+ ok 250 rachunki za media."
        total, items = scraper.extract_extra_costs(desc)
        assert total == 850


# ─────────────────────────────────────────────────────────────
#  has_next_page
# ─────────────────────────────────────────────────────────────

class TestHasNextPage:
    def test_pagination_forward_exists(self):
        html = '<html><body><a data-testid="pagination-forward">Następna</a></body></html>'
        soup = _soup_from_html(html)
        assert scraper.has_next_page(soup) is True

    def test_pagination_cy_exists(self):
        html = '<html><body><a data-cy="pagination-forward">Następna</a></body></html>'
        soup = _soup_from_html(html)
        assert scraper.has_next_page(soup) is True

    def test_aria_label_nastepna(self):
        html = '<html><body><a aria-label="następna strona">→</a></body></html>'
        soup = _soup_from_html(html)
        assert scraper.has_next_page(soup) is True

    def test_aria_label_next(self):
        html = '<html><body><a aria-label="next page">→</a></body></html>'
        soup = _soup_from_html(html)
        assert scraper.has_next_page(soup) is True

    def test_no_pagination(self):
        html = "<html><body><p>Brak wyników</p></body></html>"
        soup = _soup_from_html(html)
        assert scraper.has_next_page(soup) is False

    def test_empty_page(self):
        soup = _soup_from_html("<html></html>")
        assert scraper.has_next_page(soup) is False


# ─────────────────────────────────────────────────────────────
#  load_seen / save_seen
# ─────────────────────────────────────────────────────────────

class TestSeenPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        path = str(tmp_path / "seen.json")
        seen = {"IDabc", "IDdef", "IDghi"}
        scraper.save_seen(path, seen)
        loaded = scraper.load_seen(path)
        assert loaded == seen

    def test_load_nonexistent_returns_empty_set(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        result = scraper.load_seen(path)
        assert result == set()

    def test_save_creates_file(self, tmp_path):
        path = str(tmp_path / "seen.json")
        scraper.save_seen(path, {"IDtest"})
        assert Path(path).exists()

    def test_load_corrupted_file_returns_empty_set(self, tmp_path):
        path = tmp_path / "seen.json"
        path.write_text("nie-json!{broken")
        result = scraper.load_seen(str(path))
        assert result == set()

    def test_save_empty_set(self, tmp_path):
        path = str(tmp_path / "seen.json")
        scraper.save_seen(path, set())
        loaded = scraper.load_seen(path)
        assert loaded == set()

    def test_saved_json_is_sorted(self, tmp_path):
        path = str(tmp_path / "seen.json")
        scraper.save_seen(path, {"IDzzz", "IDaaa", "IDmmm"})
        data = json.loads(Path(path).read_text())
        assert data == sorted(data)


# ─────────────────────────────────────────────────────────────
#  format_imessage
# ─────────────────────────────────────────────────────────────

class TestFormatImessage:
    BASE_LISTING = {
        "title": "Mieszkanie 2-pokojowe Mokotów",
        "price": 3000,
        "metraz": 55.0,
        "lokalizacja": "Warszawa, Mokotów",
        "url": "https://www.olx.pl/oferta/test-IDabc.html",
        "extra_koszt": 0,
        "extra_pozycje": [],
    }

    def test_contains_url(self):
        msg = scraper.format_imessage(self.BASE_LISTING)
        assert self.BASE_LISTING["url"] in msg

    def test_contains_title(self):
        msg = scraper.format_imessage(self.BASE_LISTING)
        assert self.BASE_LISTING["title"] in msg

    def test_contains_price(self):
        msg = scraper.format_imessage(self.BASE_LISTING)
        assert "3000" in msg

    def test_contains_metraz(self):
        msg = scraper.format_imessage(self.BASE_LISTING)
        assert "55" in msg

    def test_extra_koszt_shown(self):
        listing = {**self.BASE_LISTING, "extra_koszt": 500}
        msg = scraper.format_imessage(listing)
        assert "500" in msg
        # Powinien być też koszt łączny
        assert "3500" in msg

    def test_no_extra_koszt_no_sum(self):
        msg = scraper.format_imessage(self.BASE_LISTING)
        # Bez dodatkowych kosztów nie powinno być "lacznie"
        assert "lacznie" not in msg

    def test_hidden_price(self):
        listing = {**self.BASE_LISTING, "price": None}
        msg = scraper.format_imessage(listing)
        assert "ukryta" in msg

    def test_returns_string(self):
        msg = scraper.format_imessage(self.BASE_LISTING)
        assert isinstance(msg, str)

    def test_starts_with_nowe_na_olx(self):
        msg = scraper.format_imessage(self.BASE_LISTING)
        assert msg.startswith("Nowe na OLX:")


# ─────────────────────────────────────────────────────────────
#  fetch_page – mockowanie HTTP
# ─────────────────────────────────────────────────────────────

class TestFetchPage:
    def test_returns_beautifulsoup_on_success(self):
        html = "<html><body><p>Test</p></body></html>"
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status.return_value = None

        with patch("olx_scraper.requests.get", return_value=mock_resp):
            result = scraper.fetch_page("https://www.olx.pl/test")

        assert result is not None
        assert result.find("p").text == "Test"

    def test_returns_none_on_request_exception(self):
        import requests as req_lib
        with patch("olx_scraper.requests.get", side_effect=req_lib.RequestException("timeout")):
            result = scraper.fetch_page("https://www.olx.pl/test")

        assert result is None

    def test_returns_none_on_http_error(self):
        import requests as req_lib
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req_lib.HTTPError("404")

        with patch("olx_scraper.requests.get", return_value=mock_resp):
            result = scraper.fetch_page("https://www.olx.pl/test")

        assert result is None


# ─────────────────────────────────────────────────────────────
#  fetch_detail – mockowanie HTTP
# ─────────────────────────────────────────────────────────────

class TestFetchDetail:
    def test_returns_description_text(self):
        html = '<html><body><div data-cy="ad_description">Czynsz administracyjny 400 zł</div></body></html>'
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status.return_value = None

        with patch("olx_scraper.requests.get", return_value=mock_resp):
            desc, structured = scraper.fetch_detail("https://www.olx.pl/oferta/test.html")

        assert "400" in desc
        assert "czynsz" in desc
        assert structured == 0

    def test_returns_empty_on_error(self):
        import requests as req_lib
        with patch("olx_scraper.requests.get", side_effect=req_lib.RequestException):
            desc, structured = scraper.fetch_detail("https://www.olx.pl/oferta/test.html")

        assert desc == ""
        assert structured == 0

    def test_fallback_to_itemprop(self):
        html = '<html><body><div itemprop="description">media wliczone</div></body></html>'
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status.return_value = None

        with patch("olx_scraper.requests.get", return_value=mock_resp):
            desc, _ = scraper.fetch_detail("https://www.olx.pl/oferta/test.html")

        assert "media wliczone" in desc

    def test_parses_structured_czynsz_dodatkowy(self):
        html = '''
        <html><body>
            <div data-cy="ad_description">Mieszkanie do wynajęcia</div>
            <p>Czynsz (dodatkowo): <strong>967 zł</strong></p>
        </body></html>'''
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status.return_value = None

        with patch("olx_scraper.requests.get", return_value=mock_resp):
            desc, structured = scraper.fetch_detail("https://www.olx.pl/oferta/test.html")

        assert structured == 967
        assert "mieszkanie" in desc


# ─────────────────────────────────────────────────────────────
#  Testy integracyjne (bez sieciowych zależności)
# ─────────────────────────────────────────────────────────────

class TestIntegration:
    def test_full_listing_flow(self, tmp_path):
        seen_path = str(tmp_path / "seen.json")
        listing_html = _make_card(
            url="https://www.olx.pl/oferta/test-CID3-IDintegr01.html",
            price_text="3 000 zł",
            area_text="55 m²",
        )
        html = f"<html><body>{listing_html}</body></html>"
        soup = _soup_from_html(html)

        listings = scraper.parse_listings(soup)
        assert len(listings) == 1

        seen = scraper.load_seen(seen_path)
        assert listings[0]["id"] not in seen

        seen.add(listings[0]["id"])
        scraper.save_seen(seen_path, seen)

        seen2 = scraper.load_seen(seen_path)
        assert listings[0]["id"] in seen2

    def test_extra_costs_pipeline(self):
        desc = "czynsz administracyjny: 350 zł, media ok. 150 zł. mieszkanie gotowe do wprowadzenia."
        total, items = scraper.extract_extra_costs(desc)
        assert total == 500

        price = 3000
        budget = 4000
        assert price + total <= budget

    def test_wlaczone_koszty_not_added(self):
        desc = "wszystko w cenie, media, rachunki, internet."
        total, _ = scraper.extract_extra_costs(desc)
        assert total == 0

        price = 3500
        budget = 4000
        assert price + total <= budget


# ─────────────────────────────────────────────────────────────
#  Stałe modułu
# ─────────────────────────────────────────────────────────────

class TestConstants:
    def test_request_timeout_is_positive(self):
        assert scraper.REQUEST_TIMEOUT > 0

    def test_delay_constants_exist(self):
        assert scraper.DELAY_BETWEEN_PAGES >= 1
        assert scraper.DELAY_BETWEEN_REQUESTS >= 1

    def test_max_pages_unlimited(self):
        assert scraper.MAX_PAGES_UNLIMITED >= 100


# ─────────────────────────────────────────────────────────────
#  print_header / print_listing (logują, nie drukują)
# ─────────────────────────────────────────────────────────────

class TestCLIOutput:
    def test_print_header_no_exception(self):
        config = {
            "miasto": "warszawa", "cena_min": 0, "cena_max": 4000,
            "metraz_min": 0, "metraz_max": 50,
        }
        scraper.print_header(config)  # nie powinno rzucić wyjątku

    def test_print_header_with_budget(self):
        config = {
            "miasto": "krakow", "cena_min": 1000, "cena_max": 3000,
            "metraz_min": 25, "metraz_max": 60, "budzet_lacznie": 5000,
        }
        scraper.print_header(config)

    def test_print_listing_basic(self):
        listing = {
            "id": "IDabc", "title": "Test", "price": 2500,
            "metraz": 45.0, "lokalizacja": "Wawa", "data": "dzisiaj",
            "url": "https://olx.pl/test",
        }
        scraper.print_listing(listing)

    def test_print_listing_with_extras(self):
        listing = {
            "id": "IDxyz", "title": "Test2", "price": 2500,
            "metraz": None, "lokalizacja": "", "data": "",
            "url": "https://olx.pl/test2",
            "extra_koszt": 400,
            "extra_pozycje": ["czynsz admin 400 zł"],
        }
        scraper.print_listing(listing)


# ─────────────────────────────────────────────────────────────
#  send_imessage (mockowane)
# ─────────────────────────────────────────────────────────────

class TestSendImessage:
    @patch("olx_scraper.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        logs = []
        scraper.send_imessage("+48600000000", "Test msg", log_fn=logs.append)
        assert any("OK" in m for m in logs)

    @patch("olx_scraper.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="Some error")
        logs = []
        scraper.send_imessage("+48600000000", "Test msg", log_fn=logs.append)
        assert any("WARN" in m or "ERR" in m for m in logs)

    @patch("olx_scraper.subprocess.run", side_effect=FileNotFoundError)
    def test_no_osascript(self, mock_run):
        logs = []
        scraper.send_imessage("+48600000000", "Test", log_fn=logs.append)
        assert any("osascript" in m for m in logs)


# ─────────────────────────────────────────────────────────────
#  scrape_once (mockowane HTTP)
# ─────────────────────────────────────────────────────────────

class TestScrapeOnce:
    def _make_config(self):
        return {
            "miasto": "warszawa",
            "district_id": None,
            "cena_min": 0,
            "cena_max": 5000,
            "metraz_min": 0,
            "metraz_max": 100,
            "budzet_lacznie": None,
            "max_stron": 1,
            "wyslij_imessage": False,
            "imessage_numer": "",
        }

    @patch("olx_scraper.fetch_page")
    def test_no_results(self, mock_fetch):
        mock_fetch.return_value = BeautifulSoup("<html></html>", "html.parser")
        config = self._make_config()
        seen: set[str] = set()
        result = scraper.scrape_once(config, seen)
        assert result == 0

    @patch("olx_scraper.has_next_page", return_value=False)
    @patch("olx_scraper.fetch_page")
    def test_finds_new_listings(self, mock_fetch, mock_next):
        card = _make_card(
            url="https://www.olx.pl/oferta/test-CID3-IDscrape1.html",
            price_text="2 500 zł",
            area_text="40 m²",
        )
        html = f"<html><body>{card}</body></html>"
        mock_fetch.return_value = BeautifulSoup(html, "html.parser")

        config = self._make_config()
        seen: set[str] = set()
        result = scraper.scrape_once(config, seen)
        assert result == 1
        assert "IDscrape1" in seen

    @patch("olx_scraper.has_next_page", return_value=False)
    @patch("olx_scraper.fetch_page")
    def test_skips_already_seen(self, mock_fetch, mock_next):
        card = _make_card(
            url="https://www.olx.pl/oferta/test-CID3-IDscrape2.html",
        )
        html = f"<html><body>{card}</body></html>"
        mock_fetch.return_value = BeautifulSoup(html, "html.parser")

        config = self._make_config()
        seen = {"IDscrape2"}
        result = scraper.scrape_once(config, seen)
        assert result == 0

    @patch("olx_scraper.fetch_page", return_value=None)
    def test_fetch_failure(self, mock_fetch):
        config = self._make_config()
        result = scraper.scrape_once(config, set())
        assert result == 0


# ─────────────────────────────────────────────────────────────
#  fetch_ollama_models
# ─────────────────────────────────────────────────────────────

class TestFetchOllamaModels:
    def test_returns_model_names(self):
        payload = {"models": [{"name": "llama3"}, {"name": "mistral"}]}
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status.return_value = None

        with patch("olx_scraper.requests.get", return_value=mock_resp):
            result = scraper.fetch_ollama_models("http://localhost:11434")

        assert result == ["llama3", "mistral"]

    def test_returns_empty_on_connection_error(self):
        import requests as req_lib
        with patch("olx_scraper.requests.get", side_effect=req_lib.ConnectionError):
            result = scraper.fetch_ollama_models("http://localhost:11434")
        assert result == []

    def test_returns_empty_on_empty_models(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"models": []}
        mock_resp.raise_for_status.return_value = None

        with patch("olx_scraper.requests.get", return_value=mock_resp):
            result = scraper.fetch_ollama_models("http://localhost:11434")

        assert result == []


# ─────────────────────────────────────────────────────────────
#  extract_extra_costs_llm
# ─────────────────────────────────────────────────────────────

class TestExtractExtraCostsLlm:
    def _mock_llm_response(self, extra_koszt: int, pozycje: list[str]) -> MagicMock:
        payload = json.dumps({"extra_koszt": extra_koszt, "pozycje": pozycje})
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": payload}
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    def test_returns_llm_result(self):
        mock_resp = self._mock_llm_response(700, ["czynsz administracyjny 700 zł"])
        with patch("olx_scraper.requests.post", return_value=mock_resp):
            total, items = scraper.extract_extra_costs_llm("opis ogłoszenia z czynszem")
        assert total == 700
        assert items == ["czynsz administracyjny 700 zł"]

    def test_zero_extra_returns_zero(self):
        mock_resp = self._mock_llm_response(0, [])
        with patch("olx_scraper.requests.post", return_value=mock_resp):
            total, _ = scraper.extract_extra_costs_llm("media wliczone w cenę")
        assert total == 0

    def test_fallback_on_connection_error(self):
        import requests as req_lib
        desc = "czynsz administracyjny: 400 zł"
        with patch("olx_scraper.requests.post", side_effect=req_lib.ConnectionError("timeout")):
            total, _ = scraper.extract_extra_costs_llm(desc)
        # Fallback to regex — should find 400
        assert total == 400

    def test_fallback_on_invalid_json(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "nie potrafię odpowiedzieć"}
        mock_resp.raise_for_status.return_value = None
        desc = "czynsz administracyjny: 350 zł"
        with patch("olx_scraper.requests.post", return_value=mock_resp):
            total, _ = scraper.extract_extra_costs_llm(desc)
        assert total == 350

    def test_structured_extra_wins_over_llm(self):
        mock_resp = self._mock_llm_response(300, ["czynsz 300 zł"])
        with patch("olx_scraper.requests.post", return_value=mock_resp):
            total, _ = scraper.extract_extra_costs_llm("opis", structured_extra=900)
        assert total == 900

    def test_empty_description_returns_zero(self):
        total, _ = scraper.extract_extra_costs_llm("")
        assert total == 0


# ─────────────────────────────────────────────────────────────
#  otodom_scraper
# ─────────────────────────────────────────────────────────────

import otodom_scraper


class TestOtodomScraper:
    def _make_next_data(self, description: str = "", rent: int = 0) -> str:
        characteristics = []
        if rent:
            characteristics.append({"key": "rent", "value": str(rent)})
        data = {
            "props": {
                "pageProps": {
                    "ad": {
                        "description": description,
                        "characteristics": characteristics,
                        "topInformation": [],
                    }
                }
            }
        }
        return json.dumps(data)

    def _mock_response(self, next_data_json: str) -> MagicMock:
        html = f'<html><body><script id="__NEXT_DATA__">{next_data_json}</script></body></html>'
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    def test_returns_description_and_rent(self):
        mock_resp = self._mock_response(self._make_next_data("<p>Czynsz administracyjny 700 zł</p>", rent=700))
        with patch("otodom_scraper.requests.get", return_value=mock_resp):
            desc, rent = otodom_scraper.fetch_otodom_detail("https://www.otodom.pl/pl/oferta/test.html")
        assert "czynsz" in desc
        assert rent == 700

    def test_returns_empty_on_missing_next_data(self):
        mock_resp = MagicMock()
        mock_resp.text = "<html><body><p>Brak danych</p></body></html>"
        mock_resp.raise_for_status.return_value = None
        with patch("otodom_scraper.requests.get", return_value=mock_resp):
            desc, rent = otodom_scraper.fetch_otodom_detail("https://www.otodom.pl/pl/oferta/test.html")
        assert desc == ""
        assert rent == 0

    def test_returns_empty_on_request_error(self):
        import requests as req_lib
        with patch("otodom_scraper.requests.get", side_effect=req_lib.RequestException("timeout")):
            desc, rent = otodom_scraper.fetch_otodom_detail("https://www.otodom.pl/pl/oferta/test.html")
        assert desc == ""
        assert rent == 0

    def test_description_is_lowercase(self):
        mock_resp = self._mock_response(self._make_next_data("<p>Kawalerka Na Ursynowie</p>"))
        with patch("otodom_scraper.requests.get", return_value=mock_resp):
            desc, _ = otodom_scraper.fetch_otodom_detail("https://www.otodom.pl/pl/oferta/test.html")
        assert desc == desc.lower()

    def test_zero_rent_when_not_in_characteristics(self):
        mock_resp = self._mock_response(self._make_next_data("opis mieszkania"))
        with patch("otodom_scraper.requests.get", return_value=mock_resp):
            _, rent = otodom_scraper.fetch_otodom_detail("https://www.otodom.pl/pl/oferta/test.html")
        assert rent == 0

    def test_empty_description_field(self):
        # description jest pusty — desc_text powinien być ""
        mock_resp = self._mock_response(self._make_next_data("", rent=500))
        with patch("otodom_scraper.requests.get", return_value=mock_resp):
            desc, rent = otodom_scraper.fetch_otodom_detail("https://www.otodom.pl/pl/oferta/test.html")
        assert desc == ""
        assert rent == 500

    def test_invalid_rent_value_in_characteristics(self):
        # value nie da się sparsować jako int — rent powinien zostać 0
        data = {
            "props": {"pageProps": {"ad": {
                "description": "opis",
                "characteristics": [{"key": "rent", "value": "brak"}],
                "topInformation": [],
            }}}
        }
        html = f'<html><body><script id="__NEXT_DATA__">{json.dumps(data)}</script></body></html>'
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status.return_value = None
        with patch("otodom_scraper.requests.get", return_value=mock_resp):
            _, rent = otodom_scraper.fetch_otodom_detail("https://www.otodom.pl/pl/oferta/test.html")
        assert rent == 0

    def test_rent_from_top_information_fallback(self):
        # brak characteristics z rent → fallback na topInformation
        data = {
            "props": {"pageProps": {"ad": {
                "description": "opis",
                "characteristics": [],
                "topInformation": [{"label": "rent", "values": ["800 zł/miesiąc"]}],
            }}}
        }
        html = f'<html><body><script id="__NEXT_DATA__">{json.dumps(data)}</script></body></html>'
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status.return_value = None
        with patch("otodom_scraper.requests.get", return_value=mock_resp):
            _, rent = otodom_scraper.fetch_otodom_detail("https://www.otodom.pl/pl/oferta/test.html")
        assert rent == 800

    def test_returns_empty_on_http_error(self):
        import requests as req_lib
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req_lib.HTTPError("403")
        mock_resp.response = None
        with patch("otodom_scraper.requests.get", return_value=mock_resp):
            desc, rent = otodom_scraper.fetch_otodom_detail("https://www.otodom.pl/pl/oferta/test.html")
        assert desc == ""
        assert rent == 0

    def test_returns_empty_on_invalid_json_in_next_data(self):
        html = '<html><body><script id="__NEXT_DATA__">{invalid json!!}</script></body></html>'
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status.return_value = None
        with patch("otodom_scraper.requests.get", return_value=mock_resp):
            desc, rent = otodom_scraper.fetch_otodom_detail("https://www.otodom.pl/pl/oferta/test.html")
        assert desc == ""
        assert rent == 0
