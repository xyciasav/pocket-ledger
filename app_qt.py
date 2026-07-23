"""Pocket Ledger Qt - modern PySide6 desktop shell.

This is intentionally separate from the Tkinter app while we migrate. It uses
the same local SQLite database in ~/PocketLedger/budget.db.
"""
from __future__ import annotations

import calendar
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


APP_VERSION = "0.2.3"
DEFAULT_UPDATE_REPO = "xyciasav/pocket-ledger"
RELEASES_API_URL = f"https://api.github.com/repos/{DEFAULT_UPDATE_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{DEFAULT_UPDATE_REPO}/releases/latest"
APP_DIR = Path.home() / "PocketLedger"
APP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = APP_DIR / "budget.db"

DEFAULT_LEDGER_NAME = "Personal"
DEFAULT_CASH_ACCOUNT_NAME = "Main checking"
BANK_ACCOUNT = "Bank account"
BANK_MANUAL = "Bank account / manual"
BANK_ACH = "Bank ACH / autopay"
PAID_ELSEWHERE = "Credit card / elsewhere"
PAYMENT_METHODS = (BANK_ACH, BANK_MANUAL, PAID_ELSEWHERE)
CATEGORIES = ("Fixed", "Utilities", "Other")
EXTRA_INCOME_CATEGORY = "Extra Income"
SPENDING_CATEGORIES = (
    "Groceries",
    "Dining",
    "Gas & Transport",
    "Shopping",
    "Health",
    "Entertainment",
    "Bills",
    "Credit Card Payment",
    EXTRA_INCOME_CATEGORY,
    "Other",
)


def money(value: float | int | None) -> str:
    return f"${float(value or 0):,.2f}"


def signed_money(value: float | int | None) -> str:
    amount = float(value or 0)
    sign = "+" if amount > 0 else "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def is_bank_paid(method: str | None) -> bool:
    return method in (BANK_ACCOUNT, BANK_MANUAL, BANK_ACH)


class Store:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ledgers (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, notes TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS cash_accounts (
                id INTEGER PRIMARY KEY, ledger_id INTEGER NOT NULL DEFAULT 1,
                name TEXT NOT NULL, starting_balance REAL NOT NULL DEFAULT 0,
                start_date TEXT NOT NULL DEFAULT '', notes TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, due_day INTEGER NOT NULL,
                amount REAL NOT NULL, category TEXT NOT NULL, notes TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS income (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount REAL NOT NULL,
                frequency TEXT NOT NULL DEFAULT 'Monthly', pay_day INTEGER, notes TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, balance REAL NOT NULL DEFAULT 0,
                credit_limit REAL NOT NULL DEFAULT 0, apr REAL NOT NULL DEFAULT 0,
                minimum_payment REAL NOT NULL DEFAULT 0, due_day INTEGER, notes TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS loans (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, lender TEXT DEFAULT '',
                balance REAL NOT NULL DEFAULT 0, apr REAL NOT NULL DEFAULT 0,
                payment REAL NOT NULL DEFAULT 0, due_day INTEGER, notes TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY, trans_date TEXT NOT NULL, description TEXT NOT NULL,
                amount REAL NOT NULL, category TEXT NOT NULL DEFAULT 'Other', source TEXT DEFAULT 'Manual');
            CREATE TABLE IF NOT EXISTS cc_spending (
                id INTEGER PRIMARY KEY, spend_date TEXT NOT NULL, card_id INTEGER,
                description TEXT NOT NULL, amount REAL NOT NULL, category TEXT NOT NULL DEFAULT 'Other',
                notes TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS paid_scheduled (
                id INTEGER PRIMARY KEY, event_date TEXT NOT NULL, event_type TEXT NOT NULL,
                event_name TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0,
                paid_date TEXT NOT NULL, notes TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS scheduled_overrides (
                id INTEGER PRIMARY KEY, event_date TEXT NOT NULL, event_type TEXT NOT NULL,
                event_name TEXT NOT NULL, amount REAL NOT NULL, notes TEXT DEFAULT '',
                UNIQUE(event_date,event_type,event_name));
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
            """
        )
        self.conn.execute("INSERT OR IGNORE INTO ledgers(id,name,notes) VALUES(1,?,?)", (DEFAULT_LEDGER_NAME, "Default ledger"))
        self.ensure_column("bills", "paid_from", f"TEXT NOT NULL DEFAULT '{BANK_MANUAL}'")
        self.ensure_column("bills", "related_card_id", "INTEGER")
        for table in ("bills", "income", "cards", "loans", "transactions", "cc_spending", "paid_scheduled", "scheduled_overrides"):
            self.ensure_column(table, "ledger_id", "INTEGER NOT NULL DEFAULT 1")
        self.ensure_column("transactions", "account_id", "INTEGER")
        self.ensure_column("transactions", "related_card_id", "INTEGER")
        self.ensure_default_cash_account()
        self.conn.commit()

    def ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = [row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")]
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def rows(self, query: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(query, args).fetchall()

    def one(self, query: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(query, args).fetchone()

    def execute(self, query: str, args: tuple = ()) -> None:
        self.conn.execute(query, args)
        self.conn.commit()

    def setting(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def active_ledger_id(self) -> int:
        try:
            ledger_id = int(self.setting("active_ledger_id", "1"))
        except ValueError:
            ledger_id = 1
        if not self.one("SELECT id FROM ledgers WHERE id=?", (ledger_id,)):
            ledger_id = 1
        active_count = self.ledger_activity_count(ledger_id)
        biggest = self.biggest_ledger()
        if biggest and biggest["ledger_id"] != ledger_id and active_count <= 1 and biggest["total"] > active_count:
            return int(biggest["ledger_id"])
        return ledger_id

    def biggest_ledger(self):
        return self.conn.execute(
            """
            SELECT ledger_id,SUM(c) total FROM (
                SELECT ledger_id,COUNT(*) c FROM bills GROUP BY ledger_id
                UNION ALL SELECT ledger_id,COUNT(*) c FROM income GROUP BY ledger_id
                UNION ALL SELECT ledger_id,COUNT(*) c FROM cards GROUP BY ledger_id
                UNION ALL SELECT ledger_id,COUNT(*) c FROM loans GROUP BY ledger_id
                UNION ALL SELECT ledger_id,COUNT(*) c FROM transactions GROUP BY ledger_id
                UNION ALL SELECT ledger_id,COUNT(*) c FROM cc_spending GROUP BY ledger_id
            ) GROUP BY ledger_id ORDER BY total DESC LIMIT 1
            """
        ).fetchone()

    def ledger_activity_count(self, ledger_id: int) -> int:
        total = 0
        for table in ("bills", "income", "cards", "loans", "transactions", "cc_spending"):
            total += self.conn.execute(f"SELECT COUNT(*) c FROM {table} WHERE ledger_id=?", (ledger_id,)).fetchone()["c"]
        return total

    def ensure_default_cash_account(self) -> None:
        for ledger in self.rows("SELECT id FROM ledgers"):
            if self.one("SELECT id FROM cash_accounts WHERE ledger_id=? LIMIT 1", (ledger["id"],)):
                continue
            self.conn.execute(
                "INSERT INTO cash_accounts(ledger_id,name,starting_balance,start_date,notes) VALUES(?,?,?,?,?)",
                (ledger["id"], DEFAULT_CASH_ACCOUNT_NAME, 0, date.today().isoformat(), "Default cashflow account"),
            )


@dataclass
class Metric:
    title: str
    value: str
    hint: str = ""
    tone: str = "teal"


class MetricCard(QFrame):
    def __init__(self, metric: Metric):
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        title = QLabel(metric.title.upper())
        title.setObjectName("eyebrow")
        value = QLabel(metric.value)
        value.setObjectName(f"metric_{metric.tone}")
        hint = QLabel(metric.hint)
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(value)
        if metric.hint:
            layout.addWidget(hint)


class BarsWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.rows: list[tuple[str, float, str]] = []
        self.setMinimumHeight(280)

    def set_rows(self, rows: list[tuple[str, float, str]]) -> None:
        self.rows = rows
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        if not self.rows:
            painter.setPen(QColor("#64748b"))
            painter.drawText(self.rect(), Qt.AlignCenter, "No insight data yet.")
            return
        max_value = max(value for _label, value, _color in self.rows) or 1
        y = 22
        for label, value, color in self.rows[:9]:
            painter.setPen(QColor("#0f172a"))
            painter.drawText(20, y, label)
            painter.drawText(self.width() - 140, y, money(value))
            y += 12
            width = int((self.width() - 190) * (value / max_value))
            painter.setBrush(QColor("#e2e8f0"))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(20, y, self.width() - 190, 14, 7, 7)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(20, y, width, 14, 7, 7)
            y += 38


class RowDialog(QDialog):
    def __init__(self, title: str, fields: list[tuple[str, str, object]], initial: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.widgets = {}
        self.setMinimumWidth(420)
        initial = initial or {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        for key, kind, options in fields:
            if kind == "text":
                widget = QLineEdit(str(initial.get(key, "")))
            elif kind == "money":
                widget = QDoubleSpinBox()
                widget.setMaximum(100_000_000)
                widget.setDecimals(2)
                widget.setPrefix("$")
                widget.setValue(float(initial.get(key, 0) or 0))
            elif kind == "percent":
                widget = QDoubleSpinBox()
                widget.setMaximum(200)
                widget.setDecimals(2)
                widget.setSuffix("%")
                widget.setValue(float(initial.get(key, 0) or 0))
            elif kind == "day":
                widget = QSpinBox()
                widget.setMinimum(1)
                widget.setMaximum(31)
                widget.setValue(int(initial.get(key, 1) or 1))
            elif kind == "date":
                widget = QDateEdit()
                widget.setCalendarPopup(True)
                parsed = datetime.strptime(str(initial.get(key, date.today().isoformat())), "%Y-%m-%d").date()
                widget.setDate(parsed)
            elif kind == "choice":
                widget = QComboBox()
                widget.addItems(list(options))
                value = str(initial.get(key, ""))
                if value and value in options:
                    widget.setCurrentText(value)
            else:
                raise ValueError(kind)
            self.widgets[key] = widget
            form.addRow(key, widget)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self) -> dict:
        out = {}
        for key, widget in self.widgets.items():
            if isinstance(widget, QLineEdit):
                out[key] = widget.text().strip()
            elif isinstance(widget, QDoubleSpinBox):
                out[key] = widget.value()
            elif isinstance(widget, QSpinBox):
                out[key] = widget.value()
            elif isinstance(widget, QDateEdit):
                out[key] = widget.date().toPython().isoformat()
            elif isinstance(widget, QComboBox):
                out[key] = widget.currentText()
        return out


class PocketLedgerQt(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.store = Store()
        self.ledger_id = self.store.active_ledger_id()
        self.setWindowTitle(f"Pocket Ledger Qt Preview {APP_VERSION}")
        self.resize(1480, 920)
        self.setMinimumSize(1120, 720)
        self.nav_buttons: list[QPushButton] = []
        self.pages = QStackedWidget()
        self.ledger_combo = QComboBox()
        self.tables: dict[str, QTableWidget] = {}
        self.insight_bars = BarsWidget()
        self.insight_source_bars = BarsWidget()
        self.insight_metric_grid = QGridLayout()
        self.insight_detail_table = None
        self.update_status = QLabel("Not checked yet.")
        self._build()
        self.refresh_all()

    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = self.sidebar()
        layout.addWidget(sidebar)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self.setStyleSheet(STYLE)
        self.add_page("Overview", self.overview_page())
        self.add_page("Cashflow", self.cashflow_page())
        self.add_page("Bills + Income", self.setup_page())
        self.add_page("Debt", self.debt_page())
        self.add_page("Spending", self.spending_page())
        self.add_page("Insights", self.insights_page())
        self.add_page("Settings", self.settings_page())

    def sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(240)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(18, 22, 18, 18)
        title = QLabel("Pocket Ledger")
        title.setObjectName("brand")
        subtitle = QLabel("Qt preview")
        subtitle.setObjectName("sideMuted")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(18)
        self.ledger_combo.currentIndexChanged.connect(self.switch_ledger)
        ledger_label = QLabel("Ledger")
        ledger_label.setObjectName("sideLabel")
        layout.addWidget(ledger_label)
        self.ledger_combo.setObjectName("ledgerCombo")
        layout.addWidget(self.ledger_combo)
        layout.addSpacing(18)
        for name in ("Overview", "Cashflow", "Bills + Income", "Debt", "Spending", "Insights", "Settings"):
            button = QPushButton(name)
            button.setObjectName("nav")
            button.clicked.connect(lambda _checked=False, n=name: self.go(n))
            self.nav_buttons.append(button)
            layout.addWidget(button)
        layout.addStretch()
        refresh = QPushButton("Refresh")
        refresh.setObjectName("primary")
        refresh.clicked.connect(self.refresh_all)
        layout.addWidget(refresh)
        return side

    def add_page(self, name: str, widget: QWidget) -> None:
        widget.setProperty("pageName", name)
        self.pages.addWidget(widget)

    def go(self, name: str) -> None:
        for idx in range(self.pages.count()):
            if self.pages.widget(idx).property("pageName") == name:
                self.pages.setCurrentIndex(idx)
                break
        for button in self.nav_buttons:
            button.setProperty("active", button.text() == name)
            button.style().unpolish(button)
            button.style().polish(button)

    def shell(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("page")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 24, 28, 28)
        hero = QFrame()
        hero.setObjectName("hero")
        hero_layout = QVBoxLayout(hero)
        hero_title = QLabel(title)
        hero_title.setObjectName("heroTitle")
        hero_subtitle = QLabel(subtitle)
        hero_subtitle.setObjectName("heroSubtitle")
        hero_subtitle.setWordWrap(True)
        hero_layout.addWidget(hero_title)
        hero_layout.addWidget(hero_subtitle)
        layout.addWidget(hero)
        scroll.setWidget(content)
        return scroll, layout

    def overview_page(self) -> QWidget:
        page, layout = self.shell("Overview", "A cleaner view of checking cash, card room, loan pressure, and upcoming cashflow.")
        self.metric_grid = QGridLayout()
        self.metric_grid.setSpacing(14)
        layout.addLayout(self.metric_grid)
        self.account_label = QLabel()
        self.account_label.setObjectName("cardText")
        self.account_label.setWordWrap(True)
        model_card = self.card_frame("Money model", self.account_label)
        layout.addWidget(model_card)
        self.room_table = self.table(("Period", "Starting", "Income", "Due/planned", "After bills", "Safe", "Per day"))
        layout.addWidget(self.card_frame("Spending room", self.room_table))
        layout.addStretch()
        return page

    def cashflow_page(self) -> QWidget:
        page, layout = self.shell("Cashflow", "Timeline of money landing and leaving your checking account.")
        self.cashflow_table = self.table(("Date", "Type", "Name", "Amount", "Running cash"))
        layout.addWidget(self.card_frame("Upcoming timeline", self.cashflow_table))
        return page

    def setup_page(self) -> QWidget:
        page, layout = self.shell("Bills + Income", "Recurring bills and paychecks. Debt lives on the Debt page.")
        row = QHBoxLayout()
        self.bills_table = self.table(("Name", "Due", "Category", "Paid from", "Card", "Amount"))
        self.income_table = self.table(("Name", "Frequency", "Pay day", "Amount"))
        row.addWidget(self.entity_card("Bills / monthly obligations", self.bills_table, self.add_bill, self.edit_bill, self.delete_bill), 3)
        row.addWidget(self.entity_card("Income / paychecks", self.income_table, self.add_income, self.edit_income, self.delete_income), 2)
        layout.addLayout(row)
        return page

    def debt_page(self) -> QWidget:
        page, layout = self.shell("Debt", "Cards track available room. Loans track fixed payoff payments from checking.")
        self.cards_table = self.table(("Card", "Tracked balance", "Available", "Limit", "APR", "Payment", "Due"))
        self.loans_table = self.table(("Loan", "Lender", "Balance", "APR", "Payment", "Due"))
        layout.addWidget(self.entity_card("Credit cards / spending cards", self.cards_table, self.add_card, self.edit_card, self.delete_card))
        layout.addWidget(self.entity_card("Loans / debt payoff", self.loans_table, self.add_loan, self.edit_loan, self.delete_loan))
        return page

    def spending_page(self) -> QWidget:
        page, layout = self.shell("Spending", "Checking transactions affect cashflow; card purchases track spend and available room.")
        row = QHBoxLayout()
        self.transactions_table = self.table(("Date", "Account", "Description", "Category", "Card paid", "Amount"))
        self.spending_table = self.table(("Date", "Account", "Description", "Category", "Amount", "Cashflow?"))
        row.addWidget(self.entity_card("Checking / cashflow transactions", self.transactions_table, self.add_transaction, self.edit_transaction, self.delete_transaction), 1)
        row.addWidget(self.entity_card("Purchase tracker", self.spending_table, self.add_spending, self.edit_spending, self.delete_spending), 1)
        layout.addLayout(row)
        return page

    def insights_page(self) -> QWidget:
        page, layout = self.shell("Insights", "Visual breakdown of spending, bills, cards, and debt payments.")
        self.insight_metric_grid.setSpacing(14)
        layout.addLayout(self.insight_metric_grid)
        chart_row = QHBoxLayout()
        chart_row.addWidget(self.card_frame("Category breakdown", self.insight_bars), 1)
        chart_row.addWidget(self.card_frame("Where it came from", self.insight_source_bars), 1)
        layout.addLayout(chart_row)
        self.insight_detail_table = self.table(("Description", "Category", "Source", "Total", "Count"))
        self.insight_detail_table.setMinimumHeight(320)
        layout.addWidget(self.card_frame("Top descriptions", self.insight_detail_table))
        return page

    def settings_page(self) -> QWidget:
        page, layout = self.shell("Settings", "Updates, version info, and app-level utilities for the Qt preview.")
        card = QFrame()
        card.setObjectName("card")
        body = QVBoxLayout(card)
        body.setContentsMargins(18, 16, 18, 18)
        title = QLabel("App updates")
        title.setObjectName("sectionTitle")
        current = QLabel(f"Current version: {APP_VERSION}")
        current.setObjectName("cardText")
        source = QLabel(f"Updates come from: {RELEASES_PAGE_URL}")
        source.setObjectName("cardText")
        source.setWordWrap(True)
        self.update_status.setObjectName("cardText")
        button_row = QHBoxLayout()
        check = QPushButton("Check for updates")
        check.setObjectName("primary")
        check.clicked.connect(lambda: self.check_for_updates(False))
        open_releases = QPushButton("Open releases")
        open_releases.clicked.connect(lambda: webbrowser.open(RELEASES_PAGE_URL))
        button_row.addWidget(check)
        button_row.addWidget(open_releases)
        button_row.addStretch()
        body.addWidget(title)
        body.addWidget(current)
        body.addWidget(source)
        body.addWidget(self.update_status)
        body.addLayout(button_row)
        layout.addWidget(card)
        layout.addStretch()
        return page

    def card_frame(self, title: str, child: QWidget) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 16, 18, 18)
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        layout.addWidget(label)
        layout.addWidget(child)
        return frame

    def entity_card(self, title: str, table: QTableWidget, add, edit, delete) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        header = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        add_btn = QPushButton("+ Add")
        add_btn.setObjectName("primary")
        edit_btn = QPushButton("Edit")
        delete_btn = QPushButton("Delete")
        add_btn.clicked.connect(add)
        edit_btn.clicked.connect(edit)
        delete_btn.clicked.connect(delete)
        header.addWidget(label)
        header.addStretch()
        header.addWidget(delete_btn)
        header.addWidget(edit_btn)
        header.addWidget(add_btn)
        layout.addLayout(header)
        layout.addWidget(table)
        return frame

    def table(self, headers: tuple[str, ...]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().setDefaultSectionSize(32)
        table.setMinimumHeight(240)
        table.setSortingEnabled(True)
        return table

    def set_table(self, table: QTableWidget, rows: list[sqlite3.Row | dict], values) -> None:
        table.setSortingEnabled(False)
        table.setRowCount(0)
        for row in rows:
            row_index = table.rowCount()
            table.insertRow(row_index)
            table.setVerticalHeaderItem(row_index, QTableWidgetItem(str(row["id"])))
            for col, value in enumerate(values(row)):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, row["id"])
                table.setItem(row_index, col, item)
        table.setSortingEnabled(True)

    def selected_id(self, table: QTableWidget) -> int | None:
        row = table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Select a row", "Select a row first.")
            return None
        return int(table.item(row, 0).data(Qt.UserRole))

    def refresh_ledgers(self) -> None:
        current = self.ledger_id
        self.ledger_combo.blockSignals(True)
        self.ledger_combo.clear()
        for row in self.store.rows("SELECT * FROM ledgers ORDER BY id"):
            self.ledger_combo.addItem(f"{row['id']} - {row['name']}", row["id"])
        index = self.ledger_combo.findData(current)
        self.ledger_combo.setCurrentIndex(max(0, index))
        self.ledger_combo.blockSignals(False)

    def switch_ledger(self) -> None:
        ledger_id = self.ledger_combo.currentData()
        if ledger_id:
            self.ledger_id = int(ledger_id)
            self.store.set_setting("active_ledger_id", str(self.ledger_id))
            self.refresh_all()

    def cash_account(self):
        if self.ledger_id == 0:
            rows = self.store.rows("SELECT * FROM cash_accounts ORDER BY ledger_id,id")
            if not rows:
                return {"id": 0, "name": "All checking accounts", "starting_balance": 0, "start_date": date.today().isoformat()}
            latest_start = max((row["start_date"] or date.today().isoformat()) for row in rows)
            return {
                "id": 0,
                "name": "All checking accounts",
                "starting_balance": sum(float(row["starting_balance"] or 0) for row in rows),
                "start_date": latest_start,
            }
        account = self.store.one("SELECT * FROM cash_accounts WHERE ledger_id=? ORDER BY id LIMIT 1", (self.ledger_id,))
        if account:
            return account
        self.store.execute(
            "INSERT INTO cash_accounts(ledger_id,name,starting_balance,start_date,notes) VALUES(?,?,?,?,?)",
            (self.ledger_id, DEFAULT_CASH_ACCOUNT_NAME, 0, date.today().isoformat(), "Default cashflow account"),
        )
        return self.store.one("SELECT * FROM cash_accounts WHERE ledger_id=? ORDER BY id LIMIT 1", (self.ledger_id,))

    def rows(self):
        cash = self.cash_account()
        if self.ledger_id == 0:
            bills = self.store.rows("SELECT * FROM bills ORDER BY ledger_id,due_day,name")
            income = self.store.rows("SELECT * FROM income ORDER BY ledger_id,name")
            cards = self.store.rows("SELECT * FROM cards ORDER BY ledger_id,name")
            loans = self.store.rows("SELECT * FROM loans ORDER BY ledger_id,due_day,name")
            transactions = self.store.rows(
                """
                SELECT t.*, COALESCE(a.name, 'Checking') account_name, COALESCE(c.name,'') related_card_name
                FROM transactions t
                LEFT JOIN cash_accounts a ON a.id=t.account_id
                LEFT JOIN cards c ON c.id=t.related_card_id
                ORDER BY t.ledger_id,t.trans_date DESC,t.id DESC
                """
            )
            spending = self.store.rows(
                """
                SELECT s.*, COALESCE(c.name,'Unknown card') card_name
                FROM cc_spending s LEFT JOIN cards c ON c.id=s.card_id
                ORDER BY s.ledger_id,s.spend_date DESC,s.id DESC
                """
            )
        else:
            bills = self.store.rows("SELECT * FROM bills WHERE ledger_id=? ORDER BY due_day,name", (self.ledger_id,))
            income = self.store.rows("SELECT * FROM income WHERE ledger_id=? ORDER BY name", (self.ledger_id,))
            cards = self.store.rows("SELECT * FROM cards WHERE ledger_id=? ORDER BY name", (self.ledger_id,))
            loans = self.store.rows("SELECT * FROM loans WHERE ledger_id=? ORDER BY due_day,name", (self.ledger_id,))
            transactions = self.store.rows(
                """
                SELECT t.*, COALESCE(a.name, ?) account_name, COALESCE(c.name,'') related_card_name
                FROM transactions t
                LEFT JOIN cash_accounts a ON a.id=t.account_id
                LEFT JOIN cards c ON c.id=t.related_card_id
                WHERE t.ledger_id=?
                ORDER BY t.trans_date DESC,t.id DESC
                """,
                (cash["name"], self.ledger_id),
            )
            spending = self.store.rows(
                """
                SELECT s.*, COALESCE(c.name,'Unknown card') card_name
                FROM cc_spending s LEFT JOIN cards c ON c.id=s.card_id
                WHERE s.ledger_id=?
                ORDER BY s.spend_date DESC,s.id DESC
                """,
                (self.ledger_id,),
            )
        return cash, bills, income, cards, loans, transactions, spending

    def refresh_all(self) -> None:
        self.refresh_ledgers()
        cash, bills, income, cards, loans, transactions, spending = self.rows()
        today = date.today()
        start_date = self.parse_date(cash["start_date"])
        change_start = start_date + timedelta(days=1)
        income_received = self.scheduled_income_between(income, change_start, today)
        due_outflow = self.scheduled_checking_outflow_between(bills, cards, loans, change_start, today)
        actual_spending = self.transaction_total_between(transactions, change_start, today)
        extra_income = sum(row["amount"] for row in transactions if row["category"] == EXTRA_INCOME_CATEGORY and change_start <= self.parse_date(row["trans_date"]) <= today)
        cash_today = float(cash["starting_balance"] or 0) + income_received + extra_income - due_outflow - actual_spending
        card_totals = {row["id"]: 0.0 for row in cards}
        for row in spending:
            if row["card_id"] in card_totals:
                card_totals[row["card_id"]] += float(row["amount"] or 0)
        payments = {row["id"]: 0.0 for row in cards}
        for row in transactions:
            if row["category"] == "Credit Card Payment" and row["related_card_id"] in payments:
                payments[row["related_card_id"]] += float(row["amount"] or 0)
        tracked = {card["id"]: float(card["balance"] or 0) + card_totals.get(card["id"], 0) - payments.get(card["id"], 0) for card in cards}
        card_debt = sum(tracked.values())
        card_room = sum(float(card["credit_limit"] or 0) - tracked.get(card["id"], 0) for card in cards)
        loan_debt = sum(float(row["balance"] or 0) for row in loans)

        for i in reversed(range(self.metric_grid.count())):
            widget = self.metric_grid.itemAt(i).widget()
            if widget:
                widget.setParent(None)
        metrics = (
            Metric("Checking today", money(cash_today), "Cash after baseline, landed income, checking outflows, and manual spending.", "teal"),
            Metric("Card debt / room", f"{money(card_debt)} / {money(card_room)}", "Card pressure and available room.", "blue"),
            Metric("Loan balance", money(loan_debt), "Personal loans tracked separately from card room.", "slate"),
            Metric("Income after baseline", money(income_received), "Only counted after its pay date.", "blue"),
            Metric("Due from checking", money(due_outflow), "ACH bills, card minimums, and loan payments.", "slate"),
            Metric("Checking spending", money(actual_spending), "Manual checking outflows since baseline.", "slate"),
        )
        for idx, metric in enumerate(metrics):
            self.metric_grid.addWidget(MetricCard(metric), idx // 3, idx % 3)
        self.account_label.setText(
            f"{cash['name']} baseline: {money(cash['starting_balance'])} as of {cash['start_date']}. "
            "Card purchases pressure card room first; checking changes when you pay the card. "
            "Loan payments are scheduled checking outflows."
        )
        card_names = {row["id"]: row["name"] for row in cards}
        self.set_table(self.bills_table, bills, lambda r: (r["name"], r["due_day"], r["category"], r["paid_from"], card_names.get(r["related_card_id"], "—"), money(r["amount"])))
        self.set_table(self.income_table, income, lambda r: (r["name"], r["frequency"], r["pay_day"] or "—", money(r["amount"])))
        self.set_table(self.cards_table, cards, lambda r: (r["name"], money(tracked.get(r["id"], 0)), money(float(r["credit_limit"] or 0)-tracked.get(r["id"], 0)), money(r["credit_limit"]), f"{r['apr']:.2f}%", money(r["minimum_payment"]), r["due_day"] or "—"))
        self.set_table(self.loans_table, loans, lambda r: (r["name"], r["lender"], money(r["balance"]), f"{r['apr']:.2f}%", money(r["payment"]), r["due_day"] or "—"))
        self.set_table(self.transactions_table, transactions, lambda r: (r["trans_date"], r["account_name"], r["description"], r["category"], r["related_card_name"] or "—", money(r["amount"])))
        self.set_table(self.spending_table, spending, lambda r: (r["spend_date"], r["card_name"], r["description"], r["category"], money(r["amount"]), "No"))

        timeline = self.upcoming_events(bills, income, cards, loans, transactions, cash_today, today - timedelta(days=14), today + timedelta(days=45))
        self.set_table(self.cashflow_table, timeline, lambda r: (r["date"], r["kind"], r["name"], signed_money(r["amount"]), money(r["running"])))
        room = self.spending_room_periods(bills, income, cards, loans, cash_today, today)
        self.set_table(self.room_table, room, lambda r: (r["period"], money(r["starting"]), money(r["income"]), money(r["due"]), money(r["after"]), money(r["safe"]), money(r["daily"])))
        self.refresh_insights(bills, cards, loans, transactions, spending)

    def refresh_insights(self, bills, cards, loans, transactions, spending) -> None:
        start = date.today().replace(day=1)
        end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
        records = []
        for row in transactions:
            d = self.parse_date(row["trans_date"])
            if start <= d <= end and row["category"] != EXTRA_INCOME_CATEGORY:
                records.append({"category": row["category"], "description": row["description"], "source": "Checking", "amount": float(row["amount"] or 0)})
        for row in spending:
            d = self.parse_date(row["spend_date"])
            if start <= d <= end:
                records.append({"category": row["category"], "description": row["description"], "source": f"Card: {row['card_name']}", "amount": float(row["amount"] or 0)})
        for bill in bills:
            for _d in self.dates_between(bill["due_day"], start, end):
                source = "Bill from checking" if is_bank_paid(bill["paid_from"]) else "Bill on card"
                records.append({"category": bill["category"], "description": bill["name"], "source": source, "amount": float(bill["amount"] or 0)})
        for card in cards:
            for _d in self.dates_between(card["due_day"], start, end):
                records.append({"category": "Debt", "description": card["name"], "source": "Card minimum", "amount": float(card["minimum_payment"] or 0)})
        for loan in loans:
            for _d in self.dates_between(loan["due_day"], start, end):
                records.append({"category": "Debt", "description": loan["name"], "source": "Loan payment", "amount": float(loan["payment"] or 0)})

        by_category: dict[str, float] = {}
        by_source: dict[str, float] = {}
        details: dict[tuple[str, str, str], dict] = {}
        for row in records:
            by_category[row["category"]] = by_category.get(row["category"], 0) + row["amount"]
            by_source[row["source"]] = by_source.get(row["source"], 0) + row["amount"]
            key = (row["description"], row["category"], row["source"])
            details.setdefault(key, {"id": len(details) + 1, "description": row["description"], "category": row["category"], "source": row["source"], "total": 0.0, "count": 0})
            details[key]["total"] += row["amount"]
            details[key]["count"] += 1

        category_rows = sorted(by_category.items(), key=lambda item: item[1], reverse=True)
        source_rows = sorted(by_source.items(), key=lambda item: item[1], reverse=True)
        detail_rows = sorted(details.values(), key=lambda item: item["total"], reverse=True)
        total = sum(value for _name, value in category_rows)
        count = len(records)
        top_category = category_rows[0][0] if category_rows else "—"
        avg = total / count if count else 0

        for i in reversed(range(self.insight_metric_grid.count())):
            widget = self.insight_metric_grid.itemAt(i).widget()
            if widget:
                widget.setParent(None)
        metrics = (
            Metric("Month total", money(total), f"{date.today():%B %Y}", "blue"),
            Metric("Items counted", str(count), "Transactions, scheduled bills, and debt payments.", "slate"),
            Metric("Top category", top_category, "Largest category in this view.", "teal"),
            Metric("Average item", money(avg), "Average across counted records.", "slate"),
        )
        for idx, metric in enumerate(metrics):
            self.insight_metric_grid.addWidget(MetricCard(metric), 0, idx)

        palette = ["#2563eb", "#0f766e", "#8b5cf6", "#f59e0b", "#06b6d4", "#ef4444", "#22c55e"]
        self.insight_bars.set_rows([(name, value, palette[idx % len(palette)]) for idx, (name, value) in enumerate(category_rows)])
        self.insight_source_bars.set_rows([(name, value, palette[idx % len(palette)]) for idx, (name, value) in enumerate(source_rows)])
        if self.insight_detail_table:
            self.set_table(self.insight_detail_table, detail_rows[:20], lambda r: (r["description"], r["category"], r["source"], money(r["total"]), r["count"]))

    def parse_date(self, value: str | None) -> date:
        try:
            return datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            return date.today()

    def dates_between(self, day: int | None, start: date, end: date) -> list[date]:
        if not day or start > end:
            return []
        out = []
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            candidate = date(year, month, min(day, calendar.monthrange(year, month)[1]))
            if start <= candidate <= end:
                out.append(candidate)
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return out

    def scheduled_income_between(self, income, start: date, end: date) -> float:
        return sum(float(row["amount"] or 0) for row in income for _d in self.dates_between(row["pay_day"], start, end))

    def scheduled_checking_outflow_between(self, bills, cards, loans, start: date, end: date) -> float:
        bill_total = sum(float(row["amount"] or 0) for row in bills if is_bank_paid(row["paid_from"]) for _d in self.dates_between(row["due_day"], start, end))
        card_total = sum(float(row["minimum_payment"] or 0) for row in cards for _d in self.dates_between(row["due_day"], start, end))
        loan_total = sum(float(row["payment"] or 0) for row in loans for _d in self.dates_between(row["due_day"], start, end))
        return bill_total + card_total + loan_total

    def transaction_total_between(self, transactions, start: date, end: date) -> float:
        total = 0.0
        for row in transactions:
            d = self.parse_date(row["trans_date"])
            if start <= d <= end:
                total += -float(row["amount"] or 0) if row["category"] == EXTRA_INCOME_CATEGORY else float(row["amount"] or 0)
        return total

    def upcoming_events(self, bills, income, cards, loans, transactions, start_cash: float, start: date, end: date) -> list[dict]:
        events = []
        for row in income:
            for d in self.dates_between(row["pay_day"], start, end):
                events.append((d, "Income", row["name"], float(row["amount"] or 0)))
        for row in bills:
            for d in self.dates_between(row["due_day"], start, end):
                amount = -float(row["amount"] or 0) if is_bank_paid(row["paid_from"]) else 0
                events.append((d, "Bill" if amount else "Bill on card", row["name"], amount))
        for row in cards:
            for d in self.dates_between(row["due_day"], start, end):
                events.append((d, "Card minimum", row["name"], -float(row["minimum_payment"] or 0)))
        for row in loans:
            for d in self.dates_between(row["due_day"], start, end):
                events.append((d, "Loan payment", row["name"], -float(row["payment"] or 0)))
        for row in transactions:
            d = self.parse_date(row["trans_date"])
            if start <= d <= end:
                amount = float(row["amount"] or 0) if row["category"] == EXTRA_INCOME_CATEGORY else -float(row["amount"] or 0)
                events.append((d, "Extra income" if amount > 0 else "Spending", row["description"], amount))
        events.sort(key=lambda item: (item[0], 0 if item[3] > 0 else 1, item[2]))
        running = start_cash
        rows = []
        for idx, (d, kind, name, amount) in enumerate(events):
            running += amount
            rows.append({"id": idx + 1, "date": d.isoformat(), "kind": kind, "name": name, "amount": amount, "running": running})
        return rows

    def spending_room_periods(self, bills, income, cards, loans, cash_today: float, today: date) -> list[dict]:
        pay_dates = []
        for row in income:
            if row["pay_day"]:
                this_month = date(today.year, today.month, min(row["pay_day"], calendar.monthrange(today.year, today.month)[1]))
                if this_month <= today:
                    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
                    this_month = date(year, month, min(row["pay_day"], calendar.monthrange(year, month)[1]))
                pay_dates.append((this_month, row))
        pay_dates = sorted(pay_dates)[:4]
        if not pay_dates:
            due = self.scheduled_checking_outflow_between(bills, cards, loans, today + timedelta(days=1), today + timedelta(days=30))
            return [{"id": 1, "period": "Next 30 days", "starting": cash_today, "income": 0, "due": due, "after": cash_today - due, "safe": max(0, cash_today - due), "daily": max(0, cash_today - due)/30}]
        rows = []
        opening = cash_today
        period_start = today + timedelta(days=1)
        income_at_start = 0.0
        for idx, (payday, source) in enumerate(pay_dates):
            period_end = payday - timedelta(days=1)
            due = self.scheduled_checking_outflow_between(bills, cards, loans, period_start, period_end)
            after = opening + income_at_start - due
            days = max(1, (period_end - period_start).days + 1)
            rows.append({"id": idx + 1, "period": f"{period_start:%b %d} - {period_end:%b %d}", "starting": opening, "income": income_at_start, "due": due, "after": after, "safe": max(0, after), "daily": max(0, after)/days})
            opening = after
            income_at_start = float(source["amount"] or 0)
            period_start = payday
        return rows

    def version_tuple(self, value: str) -> tuple[int, ...]:
        parts = []
        for piece in str(value).strip().lower().lstrip("v").replace("-", ".").split("."):
            digits = "".join(ch for ch in piece if ch.isdigit())
            if digits:
                parts.append(int(digits))
        return tuple(parts or [0])

    def update_download_dir(self) -> Path:
        downloads = Path.home() / "Downloads"
        folder = downloads if downloads.exists() else APP_DIR / "Updates"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def pick_release_asset(self, release: dict) -> dict | None:
        assets = release.get("assets") or []
        scored = []
        for asset in assets:
            name = str(asset.get("name", "")).lower()
            url = asset.get("browser_download_url")
            if not url:
                continue
            score = 0
            if "windows" in name or "win" in name:
                score += 4
            if name.endswith(".zip"):
                score += 3
            if "qt" in name:
                score += 3
            if "pocketledger" in name or "pocket-ledger" in name or "pocket ledger" in name:
                score += 2
            scored.append((score, asset))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    def download_update_asset(self, asset: dict, tag: str) -> Path:
        raw_name = str(asset.get("name") or f"PocketLedger-{tag}-windows.zip")
        safe_name = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in raw_name).strip()
        destination = self.update_download_dir() / (safe_name or f"PocketLedger-{tag}-windows.zip")
        self.update_status.setText(f"Downloading {tag}...")
        QApplication.processEvents()
        request = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "PocketLedgerQt"})
        with urllib.request.urlopen(request, timeout=60) as response:
            destination.write_bytes(response.read())
        return destination

    def prepare_update_asset(self, path: Path, tag: str) -> Path:
        if path.suffix.lower() != ".zip":
            return path
        extract_dir = self.update_download_dir() / f"PocketLedger-{tag}"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as archive:
            archive.extractall(extract_dir)
        candidates = sorted(extract_dir.rglob("*.exe"), key=lambda item: (0 if "qt" in item.name.lower() else 1, str(item)))
        return candidates[0] if candidates else extract_dir

    def check_for_updates(self, silent: bool = False) -> None:
        try:
            self.update_status.setText("Checking GitHub releases...")
            QApplication.processEvents()
            request = urllib.request.Request(RELEASES_API_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "PocketLedgerQt"})
            with urllib.request.urlopen(request, timeout=10) as response:
                release = json.loads(response.read().decode("utf-8"))
            tag = str(release.get("tag_name", "")).strip()
            if tag and self.version_tuple(tag) > self.version_tuple(APP_VERSION):
                asset = self.pick_release_asset(release)
                if not asset:
                    self.update_status.setText(f"{tag} is available, but no Windows download was found.")
                    return
                if QMessageBox.question(self, "Update available", f"Pocket Ledger {tag} is available.\n\nDownload {asset.get('name', 'the Windows update')} now?") == QMessageBox.Yes:
                    downloaded = self.download_update_asset(asset, tag)
                    prepared = self.prepare_update_asset(downloaded, tag)
                    self.update_status.setText(f"Update ready: {prepared}")
                    if prepared.suffix.lower() == ".exe" and QMessageBox.question(self, "Update ready", "Launch the updated Pocket Ledger now? This window will close.") == QMessageBox.Yes:
                        os.startfile(prepared)
                        self.close()
                    else:
                        try:
                            os.startfile(prepared)
                        except OSError:
                            webbrowser.open(RELEASES_PAGE_URL)
            else:
                self.update_status.setText(f"Pocket Ledger is up to date ({APP_VERSION}).")
                if not silent:
                    QMessageBox.information(self, "No update found", f"Pocket Ledger is up to date.\n\nCurrent version: {APP_VERSION}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as exc:
            self.update_status.setText("Could not check for updates.")
            if not silent:
                QMessageBox.critical(self, "Update check failed", f"Could not check GitHub releases.\n\n{exc}")

    def simple_dialog(self, title: str, fields, initial=None) -> dict | None:
        if not self.require_single_ledger():
            return None
        dialog = RowDialog(title, fields, initial, self)
        return dialog.values() if dialog.exec() == QDialog.Accepted else None

    def require_single_ledger(self) -> bool:
        if self.ledger_id != 0:
            return True
        QMessageBox.information(self, "Choose a ledger", "Choose Personal or a business ledger before adding or editing.")
        return False

    def add_bill(self): self.bill_dialog()
    def edit_bill(self):
        row_id = self.selected_id(self.bills_table)
        if row_id: self.bill_dialog(self.store.one("SELECT * FROM bills WHERE id=? AND ledger_id=?", (row_id, self.ledger_id)))
    def delete_bill(self): self.delete_selected("bills", self.bills_table)
    def bill_dialog(self, row=None):
        card_choices = [""] + [f"{r['id']} - {r['name']}" for r in self.store.rows("SELECT id,name FROM cards WHERE ledger_id=? ORDER BY name", (self.ledger_id,))]
        initial = {} if row is None else dict(row)
        if row and row["related_card_id"]:
            card = self.store.one("SELECT name FROM cards WHERE id=?", (row["related_card_id"],))
            initial["Card used"] = f"{row['related_card_id']} - {card['name'] if card else 'Unknown'}"
        values = self.simple_dialog("Bill", [("name","text",()),("due_day","day",()),("amount","money",()),("category","choice",CATEGORIES),("paid_from","choice",PAYMENT_METHODS),("Card used","choice",card_choices),("notes","text",())], initial)
        if not values: return
        related = int(values["Card used"].split(" - ", 1)[0]) if values["Card used"] else None
        if values["paid_from"] != PAID_ELSEWHERE: related = None
        if row:
            self.store.execute("UPDATE bills SET name=?,due_day=?,amount=?,category=?,paid_from=?,related_card_id=?,notes=? WHERE id=? AND ledger_id=?", (values["name"], values["due_day"], values["amount"], values["category"], values["paid_from"], related, values["notes"], row["id"], self.ledger_id))
        else:
            self.store.execute("INSERT INTO bills(name,due_day,amount,category,paid_from,related_card_id,notes,ledger_id) VALUES(?,?,?,?,?,?,?,?)", (values["name"], values["due_day"], values["amount"], values["category"], values["paid_from"], related, values["notes"], self.ledger_id))
        self.refresh_all()

    def add_income(self): self.income_dialog()
    def edit_income(self):
        row_id = self.selected_id(self.income_table)
        if row_id: self.income_dialog(self.store.one("SELECT * FROM income WHERE id=? AND ledger_id=?", (row_id, self.ledger_id)))
    def delete_income(self): self.delete_selected("income", self.income_table)
    def income_dialog(self, row=None):
        values = self.simple_dialog("Income", [("name","text",()),("amount","money",()),("frequency","choice",("Monthly","Biweekly","Weekly","Annual")),("pay_day","day",()),("notes","text",())], dict(row) if row else {})
        if not values: return
        if row: self.store.execute("UPDATE income SET name=?,amount=?,frequency=?,pay_day=?,notes=? WHERE id=? AND ledger_id=?", (values["name"], values["amount"], values["frequency"], values["pay_day"], values["notes"], row["id"], self.ledger_id))
        else: self.store.execute("INSERT INTO income(name,amount,frequency,pay_day,notes,ledger_id) VALUES(?,?,?,?,?,?)", (values["name"], values["amount"], values["frequency"], values["pay_day"], values["notes"], self.ledger_id))
        self.refresh_all()

    def add_card(self): self.card_dialog()
    def edit_card(self):
        row_id = self.selected_id(self.cards_table)
        if row_id: self.card_dialog(self.store.one("SELECT * FROM cards WHERE id=? AND ledger_id=?", (row_id, self.ledger_id)))
    def delete_card(self): self.delete_selected("cards", self.cards_table)
    def card_dialog(self, row=None):
        values = self.simple_dialog("Credit card", [("name","text",()),("balance","money",()),("credit_limit","money",()),("apr","percent",()),("minimum_payment","money",()),("due_day","day",()),("notes","text",())], dict(row) if row else {})
        if not values: return
        if row: self.store.execute("UPDATE cards SET name=?,balance=?,credit_limit=?,apr=?,minimum_payment=?,due_day=?,notes=? WHERE id=? AND ledger_id=?", (values["name"], values["balance"], values["credit_limit"], values["apr"], values["minimum_payment"], values["due_day"], values["notes"], row["id"], self.ledger_id))
        else: self.store.execute("INSERT INTO cards(name,balance,credit_limit,apr,minimum_payment,due_day,notes,ledger_id) VALUES(?,?,?,?,?,?,?,?)", (values["name"], values["balance"], values["credit_limit"], values["apr"], values["minimum_payment"], values["due_day"], values["notes"], self.ledger_id))
        self.refresh_all()

    def add_loan(self): self.loan_dialog()
    def edit_loan(self):
        row_id = self.selected_id(self.loans_table)
        if row_id: self.loan_dialog(self.store.one("SELECT * FROM loans WHERE id=? AND ledger_id=?", (row_id, self.ledger_id)))
    def delete_loan(self): self.delete_selected("loans", self.loans_table)
    def loan_dialog(self, row=None):
        values = self.simple_dialog("Loan", [("name","text",()),("lender","text",()),("balance","money",()),("apr","percent",()),("payment","money",()),("due_day","day",()),("notes","text",())], dict(row) if row else {})
        if not values: return
        if row: self.store.execute("UPDATE loans SET name=?,lender=?,balance=?,apr=?,payment=?,due_day=?,notes=? WHERE id=? AND ledger_id=?", (values["name"], values["lender"], values["balance"], values["apr"], values["payment"], values["due_day"], values["notes"], row["id"], self.ledger_id))
        else: self.store.execute("INSERT INTO loans(name,lender,balance,apr,payment,due_day,notes,ledger_id) VALUES(?,?,?,?,?,?,?,?)", (values["name"], values["lender"], values["balance"], values["apr"], values["payment"], values["due_day"], values["notes"], self.ledger_id))
        self.refresh_all()

    def add_transaction(self): self.transaction_dialog()
    def edit_transaction(self):
        row_id = self.selected_id(self.transactions_table)
        if row_id: self.transaction_dialog(self.store.one("SELECT * FROM transactions WHERE id=? AND ledger_id=?", (row_id, self.ledger_id)))
    def delete_transaction(self): self.delete_selected("transactions", self.transactions_table)
    def transaction_dialog(self, row=None):
        cash = self.cash_account()
        card_choices = [""] + [f"{r['id']} - {r['name']}" for r in self.store.rows("SELECT id,name FROM cards WHERE ledger_id=? ORDER BY name", (self.ledger_id,))]
        initial = dict(row) if row else {"trans_date": date.today().isoformat(), "category": "Other"}
        values = self.simple_dialog("Checking transaction", [("trans_date","date",()),("description","text",()),("amount","money",()),("category","choice",SPENDING_CATEGORIES),("Credit card paid","choice",card_choices)], initial)
        if not values: return
        related = int(values["Credit card paid"].split(" - ", 1)[0]) if values["Credit card paid"] else None
        if values["category"] != "Credit Card Payment": related = None
        if row: self.store.execute("UPDATE transactions SET trans_date=?,description=?,amount=?,category=?,related_card_id=? WHERE id=? AND ledger_id=?", (values["trans_date"], values["description"], values["amount"], values["category"], related, row["id"], self.ledger_id))
        else: self.store.execute("INSERT INTO transactions(trans_date,description,amount,category,related_card_id,source,account_id,ledger_id) VALUES(?,?,?,?,?,?,?,?)", (values["trans_date"], values["description"], values["amount"], values["category"], related, "Manual", cash["id"], self.ledger_id))
        self.refresh_all()

    def add_spending(self): self.spending_dialog()
    def edit_spending(self):
        row_id = self.selected_id(self.spending_table)
        if row_id: self.spending_dialog(self.store.one("SELECT * FROM cc_spending WHERE id=? AND ledger_id=?", (row_id, self.ledger_id)))
    def delete_spending(self): self.delete_selected("cc_spending", self.spending_table)
    def spending_dialog(self, row=None):
        card_choices = [f"{r['id']} - {r['name']}" for r in self.store.rows("SELECT id,name FROM cards WHERE ledger_id=? ORDER BY name", (self.ledger_id,))]
        if not card_choices:
            QMessageBox.information(self, "Add a card first", "Add a credit card on the Debt page first.")
            return
        initial = dict(row) if row else {"spend_date": date.today().isoformat(), "category": "Other"}
        if row:
            card = self.store.one("SELECT name FROM cards WHERE id=?", (row["card_id"],))
            initial["Card"] = f"{row['card_id']} - {card['name'] if card else 'Unknown'}"
        else:
            initial["Card"] = card_choices[0]
        values = self.simple_dialog("Purchase", [("spend_date","date",()),("Card","choice",card_choices),("description","text",()),("amount","money",()),("category","choice",SPENDING_CATEGORIES),("notes","text",())], initial)
        if not values: return
        card_id = int(values["Card"].split(" - ", 1)[0])
        if row: self.store.execute("UPDATE cc_spending SET spend_date=?,card_id=?,description=?,amount=?,category=?,notes=? WHERE id=? AND ledger_id=?", (values["spend_date"], card_id, values["description"], values["amount"], values["category"], values["notes"], row["id"], self.ledger_id))
        else: self.store.execute("INSERT INTO cc_spending(spend_date,card_id,description,amount,category,notes,ledger_id) VALUES(?,?,?,?,?,?,?)", (values["spend_date"], card_id, values["description"], values["amount"], values["category"], values["notes"], self.ledger_id))
        self.refresh_all()

    def delete_selected(self, table: str, widget: QTableWidget) -> None:
        if not self.require_single_ledger():
            return
        row_id = self.selected_id(widget)
        if not row_id:
            return
        if QMessageBox.question(self, "Delete", "Delete the selected item?") == QMessageBox.Yes:
            self.store.execute(f"DELETE FROM {table} WHERE id=? AND ledger_id=?", (row_id, self.ledger_id))
            self.refresh_all()


STYLE = """
#root, #page { background: #eef3f8; }
#sidebar { background: #0f172a; }
#brand { color: #ffffff; font: 700 22px 'Segoe UI'; }
#sideMuted { color: #94a3b8; }
#sideLabel { color: #cbd5e1; font: 700 9pt 'Segoe UI'; }
QLabel { color: #18243b; font: 10pt 'Segoe UI'; }
QComboBox, QLineEdit, QDateEdit, QSpinBox, QDoubleSpinBox {
    background: #ffffff; color: #0f172a; selection-background-color: #bfdbfe;
    selection-color: #0f172a; border: 1px solid #cbd5e1; border-radius: 8px; padding: 8px;
}
QComboBox#ledgerCombo { background: #ffffff; color: #0f172a; min-height: 22px; }
QComboBox QAbstractItemView {
    background: #ffffff; color: #0f172a; selection-background-color: #dbeafe;
    selection-color: #0f172a; border: 1px solid #cbd5e1; outline: 0;
}
QPushButton {
    background: #e2e8f0; border: none; border-radius: 9px; padding: 9px 14px;
    color: #334155; font: 600 9pt 'Segoe UI';
}
QPushButton:hover { background: #cbd5e1; }
QPushButton#primary {
    background: #2563eb; color: white; font-weight: 700;
}
QPushButton#primary:hover { background: #1d4ed8; }
QPushButton#nav {
    text-align: left; background: transparent; color: #cbd5e1; padding: 12px 14px;
}
QPushButton#nav:hover, QPushButton#nav[active="true"] {
    background: #1e293b; color: #ffffff;
}
#hero {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #dbeafe, stop:1 #ccfbf1);
    border-radius: 18px;
}
#heroTitle { color: #0f172a; font: 700 26px 'Segoe UI'; }
#heroSubtitle { color: #475569; font: 10pt 'Segoe UI'; }
#card {
    background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px;
}
#sectionTitle { color: #0f172a; font: 700 13pt 'Segoe UI'; }
#eyebrow { color: #64748b; font: 700 8pt 'Segoe UI'; letter-spacing: 1px; }
#metric_teal { color: #0f766e; font: 700 22pt 'Segoe UI'; }
#metric_blue { color: #2563eb; font: 700 22pt 'Segoe UI'; }
#metric_slate { color: #334155; font: 700 22pt 'Segoe UI'; }
#muted, #cardText { color: #64748b; }
QTableWidget {
    background: #ffffff; alternate-background-color: #f8fafc; gridline-color: #e2e8f0;
    border: 1px solid #e2e8f0; border-radius: 10px; color: #0f172a;
}
QTableWidget::item { color: #0f172a; padding: 8px; }
QHeaderView::section {
    background: #e2e8f0; color: #334155; padding: 8px; border: none; font-weight: 700;
}
QTableWidget::item:selected { background: #e0f2fe; color: #0f172a; }
"""


def main() -> int:
    if "--self-test-qt" in sys.argv:
        app = QApplication(sys.argv)
        window = PocketLedgerQt()
        window.refresh_all()
        print("qt ok")
        window.close()
        return 0
    app = QApplication(sys.argv)
    window = PocketLedgerQt()
    window.show()
    window.go("Overview")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
