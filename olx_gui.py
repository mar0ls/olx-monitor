#!/usr/bin/env python3
"""
olx_gui.py
Interfejs graficzny dla olx_scraper.py (wynajem mieszkan).

Wymagania:
    pip install PyQt6 requests beautifulsoup4

Uruchomienie:
    python olx_gui.py
"""

import json
import logging
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QSortFilterProxyModel, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTableView,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import olx_scraper as engine
import otodom_scraper

logger = logging.getLogger(__name__)

CONFIG_FILE = Path.home() / ".olx_scraper_gui.json"

# Stałe
MAX_PAGES_LIMIT = 999
REQUEST_DELAY_PAGE = 2        # sekundy między stronami
REQUEST_DELAY_DETAIL = 1      # sekundy między pobieraniem szczegółów
TABS_WIDTH = 290
LOG_MAX_HEIGHT = 170
PROGRESS_BAR_HEIGHT = 14


# Worker – skanowanie w osobnym wątku żeby nie zamrozić GUI

class ScrapeWorker(QThread):
    """Wątek roboczy skanujący OLX w tle. Emituje sygnały do głównego wątku GUI."""
    listing_found = pyqtSignal(dict)
    log_msg       = pyqtSignal(str)
    finished      = pyqtSignal(dict)   # {"count": int, "listings": list}

    def __init__(self, config: dict, seen: set, lock: threading.Lock) -> None:
        super().__init__()
        self.config = config
        self.seen   = seen
        self._lock  = lock
        self._stop  = threading.Event()
        self._new_listings: list[dict] = []

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        config  = self.config
        seen    = self.seen
        new_count = 0

        max_stron = config["max_stron"]
        wszystkie = max_stron == "all"
        limit     = MAX_PAGES_LIMIT if wszystkie else int(max_stron)

        district_id = config.get("district_id")
        dzielnica_label: str | None = None
        if district_id:
            city_districts = engine.get_districts_for_city(config["miasto"])
            dzielnica_label = next(
                (n for n, did in city_districts.items() if did == district_id),
                None,
            )
            self.log_msg.emit(f"Miasto: {config['miasto']} / {dzielnica_label or district_id}")
        else:
            self.log_msg.emit(f"Miasto: {config['miasto']}")
        self.log_msg.emit(f"Cena: {config['cena_min']}-{config['cena_max']} zl  "
                          f"Metraz: {config['metraz_min']}-{config['metraz_max']} m2  "
                          f"Budzet: {config.get('budzet_lacznie') or 'bez limitu'} zl")

        for page in range(1, limit + 1):
            if self._stop.is_set():
                self.log_msg.emit("Zatrzymano przez uzytkownika.")
                break

            label = f"{page}/{'all' if wszystkie else limit}"
            url   = engine.build_url(config, page)
            self.log_msg.emit(f"Strona {label}: {url}")

            soup = engine.fetch_page(url)
            if not soup:
                break

            listings = engine.parse_listings(soup)
            self.log_msg.emit(f"  Ogloszen na stronie: {len(listings)}")

            if not listings:
                self.log_msg.emit("  Brak wynikow.")
                break

            for listing in listings:
                if self._stop.is_set():
                    break
                if listing["id"] in seen:
                    continue

                # filtr dzielnicy – odrzuc ogloszenia z innych dzielnic niz wybrana
                if district_id and dzielnica_label:
                    lok = listing.get("lokalizacja", "")
                    if dzielnica_label.lower() not in lok.lower():
                        with self._lock:
                            seen.add(listing["id"])
                        self.log_msg.emit(f"  Poza dzielnica ({lok}): pominięto")
                        continue

                # filtr metrazu
                mmin = config.get("metraz_min") or 0
                mmax = config.get("metraz_max")
                if listing["metraz"] is not None and mmax is not None:
                    if not (mmin <= listing["metraz"] <= mmax):
                        with self._lock:
                            seen.add(listing["id"])
                        continue

                # analiza opisu + filtr budzetu lacznego
                budzet = config.get("budzet_lacznie")
                is_otodom = "otodom.pl" in listing["url"]
                if budzet and listing["price"] is not None:
                    short = listing["url"].split("/")[-1][:45]
                    if is_otodom:
                        self.log_msg.emit(f"  Sprawdzam opis [otodom]: {short}...")
                        opis, structured_extra = otodom_scraper.fetch_otodom_detail(listing["url"])
                        if not opis and structured_extra == 0:
                            extra_koszt, extra_pozycje = 0, ["⚠ opłaty niezweryfikowane (otodom.pl)"]
                        else:
                            extra_koszt, extra_pozycje = engine.extract_extra_costs(opis, structured_extra)
                    else:
                        use_llm = config.get("llm_enabled")
                        provider = config.get("llm_provider", "ollama")
                        method = f"LLM/{provider}" if use_llm else "regex"
                        self.log_msg.emit(f"  Sprawdzam opis [{method}]: {short}...")
                        opis, structured_extra = engine.fetch_detail(listing["url"])
                        if use_llm and provider == "openai":
                            extra_koszt, extra_pozycje = engine.extract_extra_costs_openai(
                                opis, structured_extra,
                                api_key=config.get("openai_key", ""),
                                openai_model=config.get("openai_model", "gpt-4o-mini"),
                                timeout=config.get("openai_timeout", 30),
                            )
                        elif use_llm:
                            extra_koszt, extra_pozycje = engine.extract_extra_costs_llm(
                                opis, structured_extra,
                                llm_url=config.get("llm_url", "http://localhost:11434"),
                                llm_model=config.get("llm_model", "llama3"),
                                timeout=config.get("llm_timeout", 60),
                            )
                        else:
                            extra_koszt, extra_pozycje = engine.extract_extra_costs(opis, structured_extra)
                    listing["extra_koszt"]   = extra_koszt
                    listing["extra_pozycje"] = extra_pozycje
                    lacznie = listing["price"] + extra_koszt
                    if lacznie > budzet:
                        self.log_msg.emit(
                            f"  Odrzucone: {listing['price']} + {extra_koszt}"
                            f" = {lacznie} zl > limit {budzet} zl"
                        )
                        with self._lock:
                            seen.add(listing["id"])
                        time.sleep(REQUEST_DELAY_DETAIL)
                        continue
                    else:
                        suffix = " (opłaty niezweryfikowane)" if is_otodom and extra_koszt == 0 else ""
                        self.log_msg.emit(f"  OK: {lacznie} zl lacznie{suffix}")
                    time.sleep(REQUEST_DELAY_DETAIL)
                else:
                    listing.setdefault("extra_koszt", None)
                    listing.setdefault("extra_pozycje", [])

                with self._lock:
                    seen.add(listing["id"])
                new_count += 1
                self._new_listings.append(listing)
                self.listing_found.emit(listing)

                # iMessage – osobne powiadomienie na każde ogłoszenie
                if config.get("wyslij_imessage") and config.get("imessage_numer"):
                    msg = engine.format_imessage(listing)
                    engine.send_imessage(
                        config["imessage_numer"], msg,
                        log_fn=self.log_msg.emit,
                    )

            if not engine.has_next_page(soup):
                self.log_msg.emit("  Ostatnia strona.")
                break

            time.sleep(REQUEST_DELAY_PAGE)

        self.finished.emit({"count": new_count, "listings": self._new_listings})




# E-mail – zbiorczy po zakończeniu skanowania

def _send_summary_email(
    listings: list[dict],
    smtp_cfg: dict,
    config: dict,
    log_fn: Callable[[str], None] | None = None,
) -> None:
    """Wysyła jeden zbiorczy e-mail z listą wszystkich nowych ogłoszeń."""
    import smtplib
    from datetime import datetime
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not listings:
        return

    miasto = config.get("miasto", "")
    lines = [
        f"OLX Scraper – {len(listings)} nowych ogłoszeń",
        f"Miasto: {miasto}   Data: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        "=" * 60,
    ]
    for i, listing in enumerate(listings, 1):
        price  = listing.get("price") or 0
        extra  = listing.get("extra_koszt") or 0
        metraz = f"{listing['metraz']:.1f} m2" if listing.get("metraz") else "? m2"
        lines.append(f"\n[{i}] {listing.get('title', '')}")
        lines.append(f"    Czynsz: {price} zl/mies.   Metraz: {metraz}")
        if extra:
            lines.append(f"    Dodatki: {extra} zl/mies.   Lacznie: {price + extra} zl/mies.")
            for item in listing.get("extra_pozycje", []):
                lines.append(f"      - {item}")
        if listing.get("lokalizacja"):
            lines.append(f"    Lokalizacja: {listing['lokalizacja']}")
        if listing.get("data"):
            lines.append(f"    Data: {listing['data']}")
        lines.append(f"    URL: {listing.get('url', '')}")

    body = "\n".join(lines)
    subject = f"OLX: {len(listings)} nowych ogłoszeń – {miasto} ({datetime.now().strftime('%d.%m.%Y')})"

    msg = MIMEMultipart()
    msg["From"]    = smtp_cfg["user"]
    msg["To"]      = smtp_cfg["to"]
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        port = int(smtp_cfg["port"])
        if port == 465:
            with smtplib.SMTP_SSL(smtp_cfg["host"], port, timeout=15) as s:
                s.login(smtp_cfg["user"], smtp_cfg["password"])
                s.sendmail(smtp_cfg["user"], smtp_cfg["to"], msg.as_string())
        else:
            with smtplib.SMTP(smtp_cfg["host"], port, timeout=15) as s:
                s.starttls()
                s.login(smtp_cfg["user"], smtp_cfg["password"])
                s.sendmail(smtp_cfg["user"], smtp_cfg["to"], msg.as_string())
        info = f"[OK] E-mail zbiorczy ({len(listings)} ogłoszeń) -> {smtp_cfg['to']}"
    except smtplib.SMTPAuthenticationError:
        info = "[ERR] Blad autoryzacji SMTP"
    except smtplib.SMTPException as e:
        info = f"[ERR] SMTP: {e}"
    except OSError as e:
        info = f"[ERR] E-mail (polaczenie): {e}"

    if log_fn:
        log_fn(info)


# Model tabeli wynikow

COLUMNS = ["Lp.", "Tytul", "Czynsz (zl)", "Lacznie (zl)", "Metraz (m2)",
           "Lokalizacja", "Data dodania", "URL"]


class ResultsModel(QStandardItemModel):
    """Model tabeli wyników — przechowuje ogłoszenia jako wiersze."""
    def __init__(self) -> None:
        super().__init__(0, len(COLUMNS))
        self.setHorizontalHeaderLabels(COLUMNS)

    def add_listing(self, listing: dict) -> None:
        """Dodaje ogłoszenie jako nowy wiersz tabeli."""
        price  = listing.get("price") or 0
        extra  = listing.get("extra_koszt") or 0
        lacznie = price + extra if price else None
        lp = self.rowCount() + 1

        def cell(val, is_num=False):
            it = QStandardItem()
            if val is None:
                it.setText("?")
            else:
                it.setText(str(val))
                if is_num:
                    it.setData(int(val), Qt.ItemDataRole.UserRole)
            it.setEditable(False)
            return it

        metraz_str = f"{listing['metraz']:.1f}" if listing.get("metraz") else None

        it_lp = QStandardItem(str(lp))
        it_lp.setData(lp, Qt.ItemDataRole.UserRole)
        it_lp.setEditable(False)
        it_lp.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        row = [
            it_lp,
            cell(listing.get("title", "")),
            cell(price or None, is_num=True),
            cell(lacznie, is_num=True),
            cell(metraz_str),
            cell(listing.get("lokalizacja", "")),
            cell(listing.get("data", "")),
            cell(listing.get("url", "")),
        ]

        extra_pozycje = listing.get("extra_pozycje") or []
        pozycje_str = " ".join(extra_pozycje)

        if extra > 0:
            # Żółte — wykryto i wliczono dodatkowe opłaty
            tooltip = (
                f"Wykryto dodatkowe opłaty: {extra} zł\n"
                f"Czynsz {price} zł + opłaty {extra} zł = łącznie {price + extra} zł"
            )
            bg, fg = "#fef3cd", "#5c4000"
        elif "otodom.pl" in pozycje_str:
            # Pomarańczowe — ogłoszenie z otodom.pl, opłaty nieweryfikowalne
            tooltip = (
                "⚠ Ogłoszenie pochodzi z otodom.pl\n"
                "Program nie może odczytać dodatkowych opłat z tego serwisu\n"
                "(czynsz administracyjny, media, itp.).\n"
                "Rzeczywisty koszt miesięczny może przekraczać podaną cenę.\n"
                "Sprawdź ogłoszenie ręcznie przed kontaktem."
            )
            bg, fg = "#ffe8d0", "#7a3300"
        elif extra_pozycje:
            # Niebieskie — OLX, przeszukano opis i nie znaleziono wzmianek o kosztach
            tooltip = (
                "ℹ Nie znaleziono wzmianek o dodatkowych kosztach w opisie.\n"
                "Opłaty mogą być nieujawnione w tekście\n"
                "(np. media, czynsz administracyjny do spółdzielni).\n"
                "Warto zapytać właściciela o pełny koszt miesięczny."
            )
            bg, fg = "#dceefb", "#1a4a6b"
        else:
            bg, fg = None, None

        if bg:
            for it in row:
                it.setBackground(QColor(bg))
                it.setForeground(QColor(fg))
                it.setToolTip(tooltip)

        self.appendRow(row)


class SortableProxyModel(QSortFilterProxyModel):
    """Sortuje numerycznie kolumny z liczbami (Lp., cena, metraz)."""
    NUM_COLS = {0, 2, 3, 4}  # Lp., Czynsz, Lacznie, Metraz

    def lessThan(self, left, right):  # noqa: N802
        if left.column() in self.NUM_COLS:
            lv = self.sourceModel().data(left, Qt.ItemDataRole.UserRole)
            rv = self.sourceModel().data(right, Qt.ItemDataRole.UserRole)
            try:
                return (lv or 0) < (rv or 0)
            except TypeError:
                pass
        return super().lessThan(left, right)



# Panel ustawien (lewa strona)

class SettingsPanel(QWidget):
    """Panel ustawień wyszukiwania (miasto, cena, metraż, strony)."""
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # Lokalizacja
        loc = QGroupBox("Lokalizacja")
        lf  = QFormLayout(loc)
        self.miasto = QLineEdit()
        self.miasto.setPlaceholderText("np. warszawa")
        self.dzielnica_combo = QComboBox()
        self.dzielnica_combo.addItem("– całe miasto –", None)
        self.miasto.editingFinished.connect(
            lambda: self._reload_districts(self.miasto.text().strip().lower())
        )
        lf.addRow("Miasto:", self.miasto)
        lf.addRow("Dzielnica:", self.dzielnica_combo)
        layout.addWidget(loc)

        # Cena
        cena = QGroupBox("Cena najmu (zl/mies.)")
        cf   = QFormLayout(cena)
        self.cena_min = QSpinBox()
        self.cena_min.setRange(0, 99999)
        self.cena_min.setSingleStep(100)
        self.cena_max = QSpinBox()
        self.cena_max.setRange(0, 99999)
        self.cena_max.setSingleStep(100)
        self.cena_max.setValue(9999)
        cf.addRow("Od:", self.cena_min)
        cf.addRow("Do:", self.cena_max)
        layout.addWidget(cena)

        # Metraz
        met = QGroupBox("Metraz (m2)")
        mf  = QFormLayout(met)
        self.metraz_min = QSpinBox()
        self.metraz_min.setRange(0, 9999)
        self.metraz_max = QSpinBox()
        self.metraz_max.setRange(0, 9999)
        self.metraz_max.setValue(999)
        mf.addRow("Od:", self.metraz_min)
        mf.addRow("Do:", self.metraz_max)
        layout.addWidget(met)

        # Budzet laczny
        bud = QGroupBox("Budzet laczny (czynsz + oplaty)")
        bf  = QFormLayout(bud)
        self.chk_budzet  = QCheckBox("Sprawdzaj koszty dodatkowe w opisie")
        self.chk_budzet.setChecked(True)
        self.budzet_max  = QSpinBox()
        self.budzet_max.setRange(0, 99999)
        self.budzet_max.setSingleStep(500)
        self.budzet_max.setValue(0)
        self.budzet_max.setSpecialValueText("bez limitu")
        bf.addRow(self.chk_budzet)
        bf.addRow("Max laczny koszt:", self.budzet_max)
        layout.addWidget(bud)

        # Strony
        pag = QGroupBox("Strony wynikow")
        pf  = QFormLayout(pag)
        self.max_stron = QComboBox()
        self.max_stron.addItems(["1", "2", "3", "5", "10", "all"])
        self.max_stron.setCurrentText("3")
        pf.addRow("Max stron:", self.max_stron)
        layout.addWidget(pag)

        layout.addStretch()

    def _reload_districts(self, city: str) -> None:
        """Uzupełnia combo dzielnic dla podanego miasta."""
        current_id = self.dzielnica_combo.currentData()
        self.dzielnica_combo.clear()
        self.dzielnica_combo.addItem("– całe miasto –", None)
        if city:
            for name, did in engine.get_districts_for_city(city).items():
                self.dzielnica_combo.addItem(name, did)
        # Przywróć poprzedni wybór jeśli jest na liście
        if current_id is not None:
            for i in range(self.dzielnica_combo.count()):
                if self.dzielnica_combo.itemData(i) == current_id:
                    self.dzielnica_combo.setCurrentIndex(i)
                    break

    def get_config(self) -> dict:
        ms = self.max_stron.currentText()
        bv = self.budzet_max.value()
        return {
            "miasto":         self.miasto.text().strip().lower(),
            "district_id":    self.dzielnica_combo.currentData(),
            "cena_min":       self.cena_min.value(),
            "cena_max":       self.cena_max.value(),
            "metraz_min":     self.metraz_min.value() or None,
            "metraz_max":     self.metraz_max.value() or None,
            "budzet_lacznie": bv if (self.chk_budzet.isChecked() and bv > 0) else None,
            "max_stron":      "all" if ms == "all" else int(ms),
        }

    def load(self, d: dict):
        city = d.get("miasto", "")
        self.miasto.setText(city)
        self._reload_districts(city)
        district_id = d.get("district_id")
        if district_id is not None:
            for i in range(self.dzielnica_combo.count()):
                if self.dzielnica_combo.itemData(i) == district_id:
                    self.dzielnica_combo.setCurrentIndex(i)
                    break
        self.cena_min.setValue(d.get("cena_min", 0))
        self.cena_max.setValue(d.get("cena_max", 9999))
        self.metraz_min.setValue(d.get("metraz_min") or 0)
        self.metraz_max.setValue(d.get("metraz_max") or 999)
        self.budzet_max.setValue(d.get("budzet_lacznie") or 0)
        self.chk_budzet.setChecked(d.get("budzet_lacznie") is not None)
        ms = str(d.get("max_stron", "3"))
        idx = self.max_stron.findText(ms)
        if idx >= 0:
            self.max_stron.setCurrentIndex(idx)



# Panel powiadomien

class NotifyPanel(QWidget):
    """Panel konfiguracji powiadomień (plik, iMessage, e-mail)."""
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # Plik
        plik = QGroupBox("Zapis do pliku")
        plf  = QFormLayout(plik)
        self.chk_plik = QCheckBox("Zapisuj nowe ogloszenia do pliku")
        row = QHBoxLayout()
        self.file_path = QLineEdit()
        self.file_path.setPlaceholderText("np. ~/wyniki_olx.txt")
        btn = QPushButton("...")
        btn.setFixedWidth(28)
        btn.clicked.connect(self._browse)
        row.addWidget(self.file_path)
        row.addWidget(btn)
        plf.addRow(self.chk_plik)
        plf.addRow("Sciezka:", row)
        layout.addWidget(plik)

        # SMS / iMessage
        sms = QGroupBox("SMS / iMessage  (macOS)")
        sf  = QFormLayout(sms)
        self.chk_sms   = QCheckBox("Wysylaj przez Messages.app")
        self.sms_nr    = QLineEdit()
        self.sms_nr.setPlaceholderText("+48600000000")
        sf.addRow(self.chk_sms)
        sf.addRow("Numer:", self.sms_nr)
        layout.addWidget(sms)

        # E-mail
        email = QGroupBox("E-mail (SMTP)")
        ef    = QFormLayout(email)
        self.chk_email  = QCheckBox("Wysylaj e-mail")
        self.smtp_host  = QLineEdit("smtp.gmail.com")
        self.smtp_port  = QSpinBox()
        self.smtp_port.setRange(1, 65535)
        self.smtp_port.setValue(587)
        self.smtp_user  = QLineEdit()
        self.smtp_user.setPlaceholderText("adres@gmail.com")
        self.smtp_pass  = QLineEdit()
        self.smtp_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.smtp_pass.setPlaceholderText("haslo aplikacji")
        self.smtp_to    = QLineEdit()
        self.smtp_to.setPlaceholderText("odbiorca@example.com")
        btn_test = QPushButton("Wyslij testowy e-mail")
        btn_test.clicked.connect(self._test_email)
        ef.addRow(self.chk_email)
        ef.addRow("Serwer:", self.smtp_host)
        ef.addRow("Port:", self.smtp_port)
        ef.addRow("Login:", self.smtp_user)
        ef.addRow("Haslo:", self.smtp_pass)
        ef.addRow("Do:", self.smtp_to)
        ef.addRow(btn_test)
        layout.addWidget(email)

        layout.addStretch()

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Wybierz plik wynikow", str(Path.home()),
            "Pliki tekstowe (*.txt);;Wszystkie pliki (*)"
        )
        if path:
            self.file_path.setText(path)

    def _test_email(self):
        smtp = self.get_smtp()
        if not smtp:
            QMessageBox.warning(self, "Blad", "Uzupelnij dane SMTP.")
            return
        dummy = {
            "title": "Test OLX Monitor", "price": 3000, "metraz": 50.0,
            "lokalizacja": "Warszawa", "data": "dzis",
            "url": "https://www.olx.pl",
            "extra_koszt": 0, "extra_pozycje": [],
        }
        _send_summary_email(
            [dummy], smtp, {"miasto": "test"},
            log_fn=lambda m: QMessageBox.information(self, "Wynik testu", m),
        )

    def get_smtp(self) -> dict | None:
        if not self.chk_email.isChecked():
            return None
        return {
            "host":     self.smtp_host.text().strip(),
            "port":     self.smtp_port.value(),
            "user":     self.smtp_user.text().strip(),
            "password": self.smtp_pass.text(),
            "to":       self.smtp_to.text().strip(),
        }

    def get_config(self) -> dict:
        return {
            "wyslij_plik":     self.chk_plik.isChecked(),
            "plik_sciezka":    self.file_path.text().strip(),
            "wyslij_imessage": self.chk_sms.isChecked(),
            "imessage_numer":  self.sms_nr.text().strip(),
            "wyslij_email":    self.chk_email.isChecked(),
            "smtp":            self.get_smtp(),
        }

    def load(self, d: dict):
        self.chk_plik.setChecked(d.get("wyslij_plik", False))
        self.file_path.setText(d.get("plik_sciezka", ""))
        self.chk_sms.setChecked(d.get("wyslij_imessage", False))
        self.sms_nr.setText(d.get("imessage_numer", ""))
        self.chk_email.setChecked(d.get("wyslij_email", False))
        smtp = d.get("smtp") or {}
        self.smtp_host.setText(smtp.get("host", "smtp.gmail.com"))
        self.smtp_port.setValue(smtp.get("port", 587))
        self.smtp_user.setText(smtp.get("user", ""))
        self.smtp_to.setText(smtp.get("to", ""))
        # Nie ładuj hasła do SMTP – użytkownik musi wpisać ręcznie !



# Panel LLM (Ollama / OpenAI)

class LlmPanel(QWidget):
    """Panel konfiguracji analizy kosztów przez LLM (Ollama lub OpenAI API)."""
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        grp = QGroupBox("Analiza kosztów przez LLM")
        gf  = QFormLayout(grp)

        self.chk_llm = QCheckBox("Używaj LLM zamiast wyrażeń regularnych")
        self.chk_llm.setToolTip(
            "Gdy włączone, program wysyła opis każdego ogłoszenia do LLM\n"
            "zamiast używać wbudowanych wzorców regex.\n"
            "LLM rozumie niestandarowe opisy, ale skanowanie jest wolniejsze.\n"
            "Jeśli LLM jest niedostępny, program automatycznie wraca do regex."
        )
        self.chk_llm.toggled.connect(self._toggle)

        self.provider = QComboBox()
        self.provider.addItems(["Ollama (lokalny)", "OpenAI API"])
        self.provider.setToolTip("Wybierz dostawcę LLM")
        self.provider.currentIndexChanged.connect(self._switch_provider)

        # ── Ollama ──────────────────────────────────────────────
        self.ollama_widget = QWidget()
        of = QFormLayout(self.ollama_widget)
        of.setContentsMargins(0, 0, 0, 0)

        self.llm_url = QLineEdit("http://localhost:11434")
        self.llm_url.setPlaceholderText("http://localhost:11434")
        self.llm_url.setToolTip("Adres HTTP serwera Ollama")

        model_row = QHBoxLayout()
        self.llm_model = QComboBox()
        self.llm_model.setEditable(True)
        self.llm_model.setPlaceholderText("np. llama3, mistral, bielik")
        self.llm_model.setToolTip("Wybierz model lub wpisz nazwę ręcznie")
        self.btn_refresh = QPushButton("Odśwież")
        self.btn_refresh.setFixedWidth(70)
        self.btn_refresh.setToolTip("Pobierz listę modeli z Ollamy")
        self.btn_refresh.clicked.connect(self._refresh_models)
        model_row.addWidget(self.llm_model)
        model_row.addWidget(self.btn_refresh)

        self.llm_timeout = QSpinBox()
        self.llm_timeout.setRange(5, 300)
        self.llm_timeout.setValue(60)
        self.llm_timeout.setSuffix(" s")
        self.llm_timeout.setToolTip("Limit czasu oczekiwania na odpowiedź LLM.")

        self.btn_test = QPushButton("Test połączenia")
        self.btn_test.clicked.connect(self._test_connection)

        of.addRow("URL Ollamy:", self.llm_url)
        of.addRow("Model:", model_row)
        of.addRow("Timeout:", self.llm_timeout)
        of.addRow(self.btn_test)

        # ── OpenAI ──────────────────────────────────────────────
        self.openai_widget = QWidget()
        af = QFormLayout(self.openai_widget)
        af.setContentsMargins(0, 0, 0, 0)

        self.openai_key = QLineEdit()
        self.openai_key.setPlaceholderText("sk-...")
        self.openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_key.setToolTip("Klucz API z platform.openai.com")

        self.openai_model = QComboBox()
        self.openai_model.setEditable(True)
        self.openai_model.addItems(["gpt-4o-mini", "gpt-4o", "gpt-3.5-turbo"])
        self.openai_model.setToolTip("Model OpenAI (gpt-4o-mini jest najtańszy)")

        self.openai_timeout = QSpinBox()
        self.openai_timeout.setRange(5, 120)
        self.openai_timeout.setValue(30)
        self.openai_timeout.setSuffix(" s")

        self.btn_test_openai = QPushButton("Test połączenia")
        self.btn_test_openai.clicked.connect(self._test_openai_connection)

        af.addRow("Klucz API:", self.openai_key)
        af.addRow("Model:", self.openai_model)
        af.addRow("Timeout:", self.openai_timeout)
        af.addRow(self.btn_test_openai)

        gf.addRow(self.chk_llm)
        gf.addRow("Dostawca:", self.provider)
        gf.addRow(self.ollama_widget)
        gf.addRow(self.openai_widget)

        layout.addWidget(grp)
        layout.addStretch()

        self._toggle(self.chk_llm.isChecked())
        self._switch_provider(self.provider.currentIndex())

    def _toggle(self, enabled: bool):
        self.provider.setEnabled(enabled)
        self.ollama_widget.setEnabled(enabled)
        self.openai_widget.setEnabled(enabled)

    def _switch_provider(self, index: int):
        self.ollama_widget.setVisible(index == 0)
        self.openai_widget.setVisible(index == 1)

    def _refresh_models(self):
        url = self.llm_url.text().strip()
        models = engine.fetch_ollama_models(url)
        current = self.llm_model.currentText()
        self.llm_model.clear()
        if models:
            self.llm_model.addItems(models)
            idx = self.llm_model.findText(current)
            if idx >= 0:
                self.llm_model.setCurrentIndex(idx)
            else:
                self.llm_model.setCurrentText(current)
            QMessageBox.information(self, "Ollama", f"Znaleziono {len(models)} modeli.")
        else:
            self.llm_model.setCurrentText(current)
            QMessageBox.warning(self, "Ollama", "Nie można połączyć się z Ollamą.\nSprawdź czy serwer działa i URL jest poprawny.")

    def _test_connection(self):
        url = self.llm_url.text().strip()
        models = engine.fetch_ollama_models(url)
        if models:
            QMessageBox.information(
                self, "Ollama – OK",
                f"Połączenie OK.\nDostępne modele ({len(models)}):\n" + "\n".join(f"  • {m}" for m in models)
            )
        else:
            QMessageBox.warning(self, "Ollama – błąd", "Brak połączenia z Ollamą.\nUpewnij się że serwer jest uruchomiony.")

    def _test_openai_connection(self):
        import requests as req_lib
        key = self.openai_key.text().strip()
        if not key:
            QMessageBox.warning(self, "OpenAI – błąd", "Wpisz klucz API.")
            return
        try:
            resp = req_lib.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                QMessageBox.information(self, "OpenAI – OK", "Klucz API poprawny. Połączenie działa.")
            elif resp.status_code == 401:
                QMessageBox.warning(self, "OpenAI – błąd", "Nieprawidłowy klucz API (401 Unauthorized).")
            else:
                QMessageBox.warning(self, "OpenAI – błąd", f"Błąd połączenia: HTTP {resp.status_code}.")
        except Exception as e:
            QMessageBox.warning(self, "OpenAI – błąd", f"Brak połączenia z OpenAI:\n{e}")

    def get_config(self) -> dict:
        return {
            "llm_enabled":    self.chk_llm.isChecked(),
            "llm_provider":   "openai" if self.provider.currentIndex() == 1 else "ollama",
            "llm_url":        self.llm_url.text().strip(),
            "llm_model":      self.llm_model.currentText().strip(),
            "llm_timeout":    self.llm_timeout.value(),
            "openai_key":     self.openai_key.text().strip(),
            "openai_model":   self.openai_model.currentText().strip(),
            "openai_timeout": self.openai_timeout.value(),
        }

    def load(self, d: dict):
        self.chk_llm.setChecked(d.get("llm_enabled", False))
        provider = d.get("llm_provider", "ollama")
        self.provider.setCurrentIndex(1 if provider == "openai" else 0)
        self.llm_url.setText(d.get("llm_url", "http://localhost:11434"))
        self.llm_timeout.setValue(d.get("llm_timeout", 60))
        model = d.get("llm_model", "")
        if model:
            idx = self.llm_model.findText(model)
            if idx >= 0:
                self.llm_model.setCurrentIndex(idx)
            else:
                self.llm_model.setCurrentText(model)
        self.openai_key.setText(d.get("openai_key", ""))
        openai_model = d.get("openai_model", "gpt-4o-mini")
        idx = self.openai_model.findText(openai_model)
        if idx >= 0:
            self.openai_model.setCurrentIndex(idx)
        else:
            self.openai_model.setCurrentText(openai_model)
        self.openai_timeout.setValue(d.get("openai_timeout", 30))
        self._toggle(self.chk_llm.isChecked())
        self._switch_provider(self.provider.currentIndex())


# Glowne okno

class MainWindow(QMainWindow):
    """Główne okno aplikacji OLX Monitor."""
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OLX Monitor – wynajem mieszkan")
        self.setMinimumSize(1000, 650)
        self.resize(1200, 750)

        self._seen: set[str]          = set()
        self._seen_lock               = threading.Lock()
        self._worker: ScrapeWorker | None = None

        self._load_seen()
        self._build_ui()
        self._load_config()

    # UI

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(6, 6, 6, 4)
        vbox.setSpacing(4)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Lewa strona: zakładki ustawien / powiadomien
        tabs = QTabWidget()
        tabs.setFixedWidth(TABS_WIDTH)
        self.settings = SettingsPanel()
        self.notify   = NotifyPanel()
        self.llm      = LlmPanel()
        tabs.addTab(self.settings, "Ustawienia")
        tabs.addTab(self.notify,   "Powiadomienia")
        tabs.addTab(self.llm,      "LLM")
        splitter.addWidget(tabs)

        # Prawa strona: tabela + log
        right = QWidget()
        rl    = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)

        # Tabela
        self.model = ResultsModel()
        self.proxy = SortableProxyModel()
        self.proxy.setSourceModel(self.model)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)  # Lp.
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)            # Tytul
        hh.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)            # URL
        for col, w in [(2, 95), (3, 100), (4, 85), (5, 160), (6, 130)]:
            self.table.setColumnWidth(col, w)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(self._open_url)
        self.table.setToolTip("Podwojne klikniecie otwiera ogloszenie w przegladarce")

        # Usuwanie zaznaczonych wierszy klawiszem Delete
        del_sc = QShortcut(QKeySequence.StandardKey.Delete, self.table)
        del_sc.activated.connect(self._delete_selected_rows)

        # Menu kontekstowe
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_context_menu)

        rl.addWidget(self.table, stretch=3)

        # Log
        log_label = QLabel("Log:")
        log_label.setFont(QFont("", -1, QFont.Weight.Bold))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(LOG_MAX_HEIGHT)
        self.log.setFont(QFont("Menlo", 10))
        rl.addWidget(log_label)
        rl.addWidget(self.log, stretch=1)

        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        vbox.addWidget(splitter, stretch=1)

        # Dolny pasek
        bar = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.progress.setMaximumHeight(PROGRESS_BAR_HEIGHT)

        self.btn_start      = QPushButton("Start")
        self.btn_stop       = QPushButton("Stop")
        self.btn_clear_tbl  = QPushButton("Wyczysc tabele")
        self.btn_reset_seen = QPushButton("Reset pamieci")

        self.btn_start.setFixedWidth(85)
        self.btn_stop.setFixedWidth(85)
        self.btn_clear_tbl.setFixedWidth(120)
        self.btn_reset_seen.setFixedWidth(115)

        self.btn_stop.setEnabled(False)
        self.btn_reset_seen.setToolTip(
            "Usuwa pamiec widzianych ogloszen.\n"
            "Nastepne skanowanie pobierze wszystko od nowa."
        )

        self.lbl_count = QLabel("Ogloszen w tabeli: 0")

        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_clear_tbl.clicked.connect(self._clear_table)
        self.btn_reset_seen.clicked.connect(self._reset_seen)

        bar.addWidget(self.progress)
        bar.addStretch()
        bar.addWidget(self.lbl_count)
        bar.addWidget(self.btn_clear_tbl)
        bar.addWidget(self.btn_reset_seen)
        bar.addWidget(self.btn_stop)
        bar.addWidget(self.btn_start)
        vbox.addLayout(bar)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Gotowy.")

    # Skanowanie

    def _start(self):
        s_cfg = self.settings.get_config()
        n_cfg = self.notify.get_config()

        if not s_cfg.get("miasto"):
            QMessageBox.warning(self, "Brak miasta", "Wpisz nazwe miasta.")
            return

        # Polacz konfiguracje
        llm_cfg = self.llm.get_config()
        config = {**s_cfg, **n_cfg, **llm_cfg, "seen_file": str(Path.home() / ".olx_scraper_seen.json")}

        self._log(f"\n--- Nowe skanowanie  {_now()} ---")

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setVisible(True)
        self.status.showMessage("Skanowanie...")

        self._worker = ScrapeWorker(config, self._seen, self._seen_lock)
        self._worker.listing_found.connect(self._on_listing)
        self._worker.log_msg.connect(self._log)
        self._worker.finished.connect(self._on_done)
        self._worker.start()

    def _stop(self):
        if self._worker:
            self._worker.stop()
        self.btn_stop.setEnabled(False)
        self.status.showMessage("Zatrzymywanie...")

    def _on_listing(self, listing: dict) -> None:
        """Sygnał z workera — nowe ogłoszenie znalezione."""
        self.model.add_listing(listing)
        self.lbl_count.setText(f"Ogloszen w tabeli: {self.model.rowCount()}")
        self._save_seen()

        # Zapis do pliku jesli wlaczony
        n = self.notify.get_config()
        if n.get("wyslij_plik") and n.get("plik_sciezka"):
            _append_to_file(listing, n["plik_sciezka"])

    def _on_done(self, result: dict) -> None:
        """Sygnał z workera — skanowanie zakończone."""
        count    = result["count"]
        listings = result["listings"]
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setVisible(False)
        self.status.showMessage(f"Gotowe. Nowych ogloszen: {count}")
        self._log(f"--- Koniec. Nowych: {count} ---")
        self._save_config()

        # Zbiorczy e-mail po zakończeniu skanowania
        if listings:
            n = self.notify.get_config()
            if n.get("wyslij_email") and n.get("smtp"):
                s_cfg = self.settings.get_config()
                _send_summary_email(
                    listings, n["smtp"], s_cfg,
                    log_fn=self._log,
                )

    # Tabela

    def _open_url(self, index):
        src = self.proxy.mapToSource(index)
        url_idx = self.model.index(src.row(), COLUMNS.index("URL"))
        url = self.model.data(url_idx)
        if url and url.startswith("http"):
            webbrowser.open(url)

    def _delete_selected_rows(self):
        """Usuwa zaznaczone wiersze z tabeli wyników."""
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            return
        # Mapuj na wiersze modelu źródłowego i usuń od końca, żeby indeksy się nie przesunęły
        source_rows = sorted(
            {self.proxy.mapToSource(idx).row() for idx in selected},
            reverse=True,
        )
        for row in source_rows:
            self.model.removeRow(row)
        self.lbl_count.setText(f"Ogloszen w tabeli: {self.model.rowCount()}")

    def _table_context_menu(self, pos):
        if not self.table.selectionModel().selectedRows():
            return
        menu = QMenu(self)
        act = menu.addAction("Usuń zaznaczone wiersze (Del)")
        act.triggered.connect(self._delete_selected_rows)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _clear_table(self):
        self.model.removeRows(0, self.model.rowCount())
        self.lbl_count.setText("Ogloszen w tabeli: 0")

    # Pamiec seen

    def _reset_seen(self):
        ans = QMessageBox.question(
            self, "Reset pamieci",
            "Nastepne skanowanie potraktuje wszystkie ogloszenia jako nowe.\nKontynuowac?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._seen.clear()
            (Path.home() / ".olx_scraper_seen.json").unlink(missing_ok=True)
            self._log("Pamiec wyczyszczona.")

    def _load_seen(self) -> None:
        """Wczytuje set widzianych ID z dysku."""
        p = Path.home() / ".olx_scraper_seen.json"
        if p.exists():
            try:
                self._seen = set(json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                self._seen = set()

    def _save_seen(self) -> None:
        """Zapisuje set widzianych ID na dysk (thread-safe)."""
        try:
            p = Path.home() / ".olx_scraper_seen.json"
            with self._seen_lock:
                data = sorted(self._seen)
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # Konfiguracja

    def _load_config(self) -> None:
        """Wczytuje ustawienia z pliku JSON."""
        if CONFIG_FILE.exists():
            try:
                d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                self.settings.load(d.get("search", {}))
                self.notify.load(d.get("notify", {}))
                self.llm.load(d.get("llm", {}))
            except (json.JSONDecodeError, OSError, KeyError):
                pass

    def _save_config(self):
        n = self.notify.get_config()
        # Nie zapisujemy hasla SMTP
        smtp_save = {k: v for k, v in (n.get("smtp") or {}).items() if k != "password"}
        n_save = {**n, "smtp": smtp_save or None}
        try:
            CONFIG_FILE.write_text(
                json.dumps({
                    "search": self.settings.get_config(),
                    "notify": n_save,
                    "llm":    self.llm.get_config(),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    # Log

    def _log(self, msg: str):
        self.log.append(msg)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    # Zamkniecie

    def closeEvent(self, event):  # noqa: N802
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(2000)
        self._save_config()
        self._save_seen()
        event.accept()


# Pomocnicze

def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def _append_to_file(listing: dict, path: str):
    price  = listing.get("price") or 0
    extra  = listing.get("extra_koszt") or 0
    metraz = f"{listing['metraz']:.1f} m2" if listing.get("metraz") else "? m2"
    lines  = [
        f"\n[{_now()}]",
        f"Tytul:  {listing.get('title','')}",
        f"Czynsz: {price} zl/mies.   Metraz: {metraz}",
    ]
    if extra:
        lines.append(f"Dodatki: {extra} zl/mies.  Lacznie: {price+extra} zl/mies.")
        for item in listing.get("extra_pozycje", []):
            lines.append(f"  - {item}")
    elif listing.get("extra_pozycje"):
        for item in listing.get("extra_pozycje", []):
            lines.append(f"  ! {item}")
    if listing.get("lokalizacja"):
        lines.append(f"Lokalizacja: {listing['lokalizacja']}")
    lines.append(f"URL: {listing.get('url','')}")
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        logger.error("Zapis do pliku %s: %s", path, e)



if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("OLX Monitor")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
