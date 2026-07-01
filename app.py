"""Pocket Ledger - a local-first personal budget desktop application."""
from __future__ import annotations

import calendar
import csv
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


if "--self-test-tk" in sys.argv:
    print(f"tkinter ok: Tk {tk.TkVersion}")
    raise SystemExit(0)

APP_DIR = Path.home() / "PocketLedger"
APP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = APP_DIR / "budget.db"
APP_VERSION = "0.1.6"
DEFAULT_UPDATE_REPO = "xyciasav/pocket-ledger"
RELEASES_API_URL = f"https://api.github.com/repos/{DEFAULT_UPDATE_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{DEFAULT_UPDATE_REPO}/releases/latest"
WHATS_NEW = {
    "0.1.6": [
        "Restored full-height layout for Spending and Insights so tables, panes, and charts use the available screen space again.",
        "Kept scrolling on Cashflow, Setup, and Settings where page-style scrolling is useful.",
    ],
    "0.1.5": [
        "Added vertical scrolling to Cashflow, Setup, Spending, Insights, and Settings tabs.",
        "Cashflow now shows a timeline with the last 14 days, today, and the next 45 days.",
        "The Cashflow Timeline running balance now starts from the beginning of the lookback window so recent past activity connects to today's cash.",
    ],
    "0.1.4": [
        "Insights now has view filters for full monthly obligations, spending transactions, scheduled bills, credit-card spending, and cashflow-account spending.",
        "Added an Insights category filter so you can focus on one category without losing the visual breakdown.",
        "The default Insights view now ties scheduled bills, card minimums, cashflow spending, and credit-card spending together.",
    ],
    "0.1.3": [
        "Automatic updates now use the hardcoded Pocket Ledger GitHub release feed.",
        "When a newer release is found, Pocket Ledger can download the Windows ZIP directly instead of sending you to GitHub.",
        "Added a once-per-version What's New popup so new features and fixes are easier to spot.",
    ],
    "0.1.2": [
        "Added multiple ledgers for personal and business views.",
        "Added account-based spending so cashflow-account spending and credit-card spending are tracked differently.",
        "Fixed ledger dropdown order and numeric due-date sorting.",
    ],
}
DEFAULT_LEDGER_NAME = "Personal"
DEFAULT_CASH_ACCOUNT_NAME = "Main checking"
CATEGORIES = ("Fixed", "Utilities", "Other")
EXTRA_INCOME_CATEGORY = "Extra Income"
SPENDING_CATEGORIES = ("Groceries", "Dining", "Gas & Transport", "Shopping", "Health", "Entertainment", "Bills", "Credit Card Payment", EXTRA_INCOME_CATEGORY, "Other")
BANK_ACCOUNT = "Bank account"  # Legacy value kept for old databases/imports.
BANK_MANUAL = "Bank account / manual"
BANK_ACH = "Bank ACH / autopay"
PAID_ELSEWHERE = "Credit card / elsewhere"
PAYMENT_METHODS = (BANK_ACH, BANK_MANUAL, PAID_ELSEWHERE)


def money(value: float | int | None) -> str:
    return f"${float(value or 0):,.2f}"


def signed_money(value: float | int | None) -> str:
    amount = float(value or 0)
    sign = "+" if amount > 0 else "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def is_bank_paid(method: str | None) -> bool:
    return method in (BANK_ACCOUNT, BANK_MANUAL, BANK_ACH)


class Database:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
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
        """)
        self.conn.execute("INSERT OR IGNORE INTO ledgers(id,name,notes) VALUES(1,?,?)", (DEFAULT_LEDGER_NAME, "Default ledger"))
        self.ensure_column("bills", "paid_from", f"TEXT NOT NULL DEFAULT '{BANK_MANUAL}'")
        for table in ("bills", "income", "cards", "transactions", "cc_spending", "paid_scheduled", "scheduled_overrides"):
            self.ensure_column(table, "ledger_id", "INTEGER NOT NULL DEFAULT 1")
        self.ensure_column("transactions", "account_id", "INTEGER")
        self.ensure_ledger_scoped_overrides()
        self.conn.execute("UPDATE bills SET paid_from=? WHERE paid_from=?", (BANK_MANUAL, BANK_ACCOUNT))
        self.ensure_default_cash_account()
        self.conn.commit()

    def ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = [row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")]
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def ensure_ledger_scoped_overrides(self) -> None:
        indexes = self.conn.execute("PRAGMA index_list(scheduled_overrides)").fetchall()
        needs_rebuild = False
        for row in indexes:
            if not row["unique"]:
                continue
            columns = [info["name"] for info in self.conn.execute(f"PRAGMA index_info({row['name']})")]
            if columns == ["ledger_id", "event_date", "event_type", "event_name"]:
                return
            if columns == ["event_date", "event_type", "event_name"]:
                needs_rebuild = True
        if not needs_rebuild:
            return
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS scheduled_overrides_new (
                id INTEGER PRIMARY KEY, event_date TEXT NOT NULL, event_type TEXT NOT NULL,
                event_name TEXT NOT NULL, amount REAL NOT NULL, notes TEXT DEFAULT '',
                ledger_id INTEGER NOT NULL DEFAULT 1,
                UNIQUE(ledger_id,event_date,event_type,event_name));
            INSERT OR IGNORE INTO scheduled_overrides_new(id,event_date,event_type,event_name,amount,notes,ledger_id)
                SELECT id,event_date,event_type,event_name,amount,notes,ledger_id FROM scheduled_overrides;
            DROP TABLE scheduled_overrides;
            ALTER TABLE scheduled_overrides_new RENAME TO scheduled_overrides;
        """)

    def rows(self, query: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(query, args).fetchall()

    def one(self, query: str, args: tuple = ()) -> sqlite3.Row:
        return self.conn.execute(query, args).fetchone()

    def execute(self, query: str, args: tuple = ()) -> None:
        self.conn.execute(query, args)
        self.conn.commit()

    def ensure_default_cash_account(self) -> None:
        ledgers = self.conn.execute("SELECT id FROM ledgers").fetchall()
        for ledger in ledgers:
            existing = self.conn.execute("SELECT id FROM cash_accounts WHERE ledger_id=? LIMIT 1", (ledger["id"],)).fetchone()
            if existing:
                continue
            start_balance = self.setting("bank_start_balance", "0") if ledger["id"] == 1 else "0"
            start_date = self.setting("bank_start_date", date.today().isoformat()) if ledger["id"] == 1 else date.today().isoformat()
            self.conn.execute(
                "INSERT INTO cash_accounts(ledger_id,name,starting_balance,start_date,notes) VALUES(?,?,?,?,?)",
                (ledger["id"], DEFAULT_CASH_ACCOUNT_NAME, float(start_balance or 0), start_date, "Default cashflow account"),
            )

    def active_ledger_id(self) -> int:
        try:
            ledger_id = int(self.setting("active_ledger_id", "1"))
        except ValueError:
            ledger_id = 1
        if not self.conn.execute("SELECT id FROM ledgers WHERE id=?", (ledger_id,)).fetchone():
            ledger_id = 1
            self.set_setting("active_ledger_id", "1")
        return ledger_id

    def setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()


class LedgerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.db = Database()
        self.ledger_id = self.db.active_ledger_id()
        self.ledger_var = tk.StringVar()
        self.title("Pocket Ledger")
        self.geometry("1500x920")
        self.minsize(1180, 760)
        self.configure(bg="#f5f7fb")
        self._style()
        self._build_header()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=24, pady=(0, 24))
        self.dashboard_tab = ttk.Frame(self.notebook, padding=18)
        self.cashflow_tab_outer = ttk.Frame(self.notebook)
        self.setup_tab_outer = ttk.Frame(self.notebook)
        self.spending_tab = ttk.Frame(self.notebook, padding=18)
        self.data_tab = ttk.Frame(self.notebook, padding=18)
        self.settings_tab_outer = ttk.Frame(self.notebook)
        self.notebook.add(self.dashboard_tab, text="  Dashboard  ")
        self.notebook.add(self.cashflow_tab_outer, text="  Cashflow  ")
        self.notebook.add(self.setup_tab_outer, text="  Setup  ")
        self.notebook.add(self.spending_tab, text="  Spending  ")
        self.notebook.add(self.data_tab, text="  Insights  ")
        self.notebook.add(self.settings_tab_outer, text="  Settings  ")
        self.cashflow_tab = self.scrollable_tab(self.cashflow_tab_outer)
        self.setup_tab = self.scrollable_tab(self.setup_tab_outer)
        self.settings_tab = self.scrollable_tab(self.settings_tab_outer)
        self.build_dashboard()
        self.build_cashflow()
        self.build_setup()
        self.build_spending()
        self.build_insights()
        self.build_settings()
        self.refresh_ledger_choice()
        self.refresh_all()
        self.after(700, self.show_whats_new_once)
        self.after(1500, self.auto_check_updates)

    def _style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f5f7fb")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f5f7fb", foreground="#25324a", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 22), foreground="#18243b")
        style.configure("Subtitle.TLabel", foreground="#6b7890")
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#637087", font=("Segoe UI", 9))
        style.configure("Amount.TLabel", background="#ffffff", foreground="#1c6e68", font=("Segoe UI Semibold", 20))
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", rowheight=31, font=("Segoe UI", 10), borderwidth=0)
        style.configure("Treeview.Heading", background="#eaf0f7", foreground="#40516d", font=("Segoe UI Semibold", 9), relief="flat")
        style.map("Treeview", background=[("selected", "#d8eeec")], foreground=[("selected", "#15283a")])
        style.configure("TButton", font=("Segoe UI Semibold", 9), padding=(12, 7))
        style.configure("Accent.TButton", background="#147d78", foreground="white")
        style.map("Accent.TButton", background=[("active", "#0f6964")])
        style.configure("TNotebook", background="#f5f7fb", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 9), background="#eaf0f7", foreground="#53627a")
        style.map("TNotebook.Tab", background=[("selected", "#ffffff")], foreground=[("selected", "#147d78")])

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(24, 20, 24, 12))
        header.pack(fill="x")
        ttk.Label(header, text="Pocket Ledger", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="Your money, clearly organized.", style="Subtitle.TLabel").pack(side="left", padx=14, pady=7)
        ttk.Button(header, text="Add ledger", command=self.ledger_dialog).pack(side="right", padx=(8, 0))
        self.ledger_choice = ttk.Combobox(header, textvariable=self.ledger_var, state="readonly", width=22)
        self.ledger_choice.pack(side="right", padx=(8, 0))
        self.ledger_choice.bind("<<ComboboxSelected>>", lambda _: self.switch_ledger())
        ttk.Label(header, text="Ledger:").pack(side="right", padx=(12, 0))
        ttk.Button(header, text="Import full data", command=self.import_full_data).pack(side="right", padx=(8, 0))
        ttk.Button(header, text="Export full data", command=self.export_full_data).pack(side="right")
        ttk.Button(header, text="Back up data", command=self.backup_data).pack(side="right", padx=(8, 0))
        ttk.Button(header, text="Export spending CSV", command=self.export_transactions).pack(side="right")
        ttk.Button(header, text="Refresh", command=self.refresh_all).pack(side="right")

    def scrollable_tab(self, outer: ttk.Frame) -> ttk.Frame:
        canvas = tk.Canvas(outer, background="#f5f7fb", highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        inner = ttk.Frame(canvas, padding=18)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        def wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"
        canvas.bind("<MouseWheel>", wheel)
        inner.bind("<MouseWheel>", wheel)
        return inner

    def ledger_rows(self):
        return self.db.rows("SELECT * FROM ledgers ORDER BY id")

    def refresh_ledger_choice(self) -> None:
        ledgers = self.ledger_rows()
        values = tuple(f"{row['id']} - {row['name']}" for row in ledgers)
        self.ledger_choice["values"] = values
        current = next((value for value in values if value.startswith(f"{self.ledger_id} - ")), values[0] if values else "")
        self.ledger_var.set(current)

    def switch_ledger(self) -> None:
        value = self.ledger_var.get()
        if not value:
            return
        self.ledger_id = int(value.split(" - ", 1)[0])
        self.db.set_setting("active_ledger_id", str(self.ledger_id))
        self.db.ensure_default_cash_account()
        self.refresh_all()

    def ledger_dialog(self):
        def save(v):
            name = v["Ledger name"].strip()
            if not name:
                raise ValueError("Ledger name is required.")
            with self.db.conn:
                cursor = self.db.conn.execute("INSERT INTO ledgers(name,notes) VALUES(?,?)", (name, v["Notes"]))
                ledger_id = cursor.lastrowid
                self.db.conn.execute(
                    "INSERT INTO cash_accounts(ledger_id,name,starting_balance,start_date,notes) VALUES(?,?,?,?,?)",
                    (ledger_id, DEFAULT_CASH_ACCOUNT_NAME, 0, date.today().isoformat(), "Default cashflow account"),
                )
            self.ledger_id = ledger_id
            self.db.set_setting("active_ledger_id", str(ledger_id))
        self.form("Add ledger", [("Ledger name","text",()),("Notes","text",())], {"Ledger name":"Business"}, save)

    def card(self, parent: ttk.Frame, title: str, variable: tk.StringVar, col: int) -> None:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=16)
        frame.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 10, 0))
        ttk.Label(frame, text=title.upper(), style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(frame, textvariable=variable, style="Amount.TLabel").pack(anchor="w", pady=(7, 0))

    def metric_card(self, parent: ttk.Frame, title: str, variable: tk.StringVar, row: int, col: int) -> None:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=16)
        frame.grid(row=row, column=col, sticky="nsew", padx=(0 if col == 0 else 10, 0), pady=(0 if row == 0 else 10, 0))
        ttk.Label(frame, text=title.upper(), style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(frame, textvariable=variable, style="Amount.TLabel").pack(anchor="w", pady=(7, 0))

    def build_dashboard(self) -> None:
        canvas = tk.Canvas(self.dashboard_tab, background="#f5f7fb", highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.dashboard_tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        dashboard = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=dashboard, anchor="nw")
        dashboard.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-1 * (event.delta / 120)), "units"))
        dashboard.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-1 * (event.delta / 120)), "units"))

        hero = ttk.Frame(dashboard)
        hero.pack(fill="x", pady=(0, 14))
        ttk.Label(hero, text="Dashboard", style="Title.TLabel").pack(anchor="w")
        ttk.Label(hero, text="Cash today, paycheck pressure, flexible spending room, and upcoming obligations.", style="Subtitle.TLabel").pack(anchor="w", pady=(4, 0))

        self.bank_start_var, self.income_var, self.outflow_var, self.spent_var, self.projected_var, self.card_var = (tk.StringVar() for _ in range(6))
        cards = ttk.Frame(dashboard)
        cards.pack(fill="x", pady=(0, 18))
        for i in range(3): cards.columnconfigure(i, weight=1)
        self.metric_card(cards, "Cash today", self.projected_var, 0, 0)
        self.metric_card(cards, "Spending since start", self.spent_var, 0, 1)
        self.metric_card(cards, "Card debt", self.card_var, 0, 2)
        self.metric_card(cards, "Cashflow start", self.bank_start_var, 1, 0)
        self.metric_card(cards, "Income received", self.income_var, 1, 1)
        self.metric_card(cards, "Due bills + cards", self.outflow_var, 1, 2)

        account_row = ttk.Frame(dashboard, style="Card.TFrame", padding=(16, 12))
        account_row.pack(fill="x", pady=(0, 14))
        self.account_math_var = tk.StringVar()
        account_header = ttk.Frame(account_row, style="Card.TFrame")
        account_header.pack(fill="x")
        ttk.Label(account_header, text="ACCOUNT MATH", style="CardTitle.TLabel").pack(side="left")
        ttk.Button(account_header, text="Set cashflow account", command=self.account_dialog).pack(side="right")
        ttk.Label(account_row, textvariable=self.account_math_var, style="Subtitle.TLabel", wraplength=900, justify="left").pack(anchor="w", pady=(7, 0))

        self.cashflow_var = tk.StringVar()
        cashflow = ttk.Frame(dashboard, style="Card.TFrame", padding=(16, 12))
        cashflow.pack(fill="x", pady=(0, 14))
        ttk.Label(cashflow, text="NEXT PAYDAY PLAN", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(cashflow, textvariable=self.cashflow_var, style="Subtitle.TLabel", wraplength=980, justify="left").pack(anchor="w", pady=(7, 0))

        spending_room = ttk.Frame(dashboard, style="Card.TFrame", padding=14)
        spending_room.pack(fill="x", pady=(0, 14))
        ttk.Label(spending_room, text="SPENDING ROOM", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(spending_room, text="Conservative spending room. It protects the lowest upcoming cash point, so rows are not meant to be added together.", style="Subtitle.TLabel").pack(anchor="w", pady=(5, 10))
        self.spending_room_tree = self.tree(spending_room, ("Period", "Starting cash", "Income", "Bills/planned", "After bills", "Safe now", "Per day"), (175, 105, 95, 110, 105, 105, 85))
        self.spending_room_tree.pack(fill="x")
        self.spending_room_tree.tag_configure("good", background="#eaf7ef")
        self.spending_room_tree.tag_configure("warning", background="#fdecec")

    def build_cashflow(self) -> None:
        header = ttk.Frame(self.cashflow_tab)
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="Cashflow", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="A dated forecast of income, scheduled bills, card minimums, and planned bank spending.", style="Subtitle.TLabel").pack(side="left", padx=14, pady=7)

        self.cashflow_low_var, self.cashflow_income_var, self.cashflow_outflow_var, self.cashflow_end_var = (tk.StringVar() for _ in range(4))
        cashflow_cards = ttk.Frame(self.cashflow_tab)
        cashflow_cards.pack(fill="x", pady=(0, 14))
        for i in range(4): cashflow_cards.columnconfigure(i, weight=1)
        self.card(cashflow_cards, "Lowest cash", self.cashflow_low_var, 0)
        self.card(cashflow_cards, "Incoming", self.cashflow_income_var, 1)
        self.card(cashflow_cards, "Outgoing", self.cashflow_outflow_var, 2)
        self.card(cashflow_cards, "Ending cash", self.cashflow_end_var, 3)

        forecast = ttk.Frame(self.cashflow_tab, style="Card.TFrame", padding=14)
        forecast.pack(fill="both", expand=True)
        ttk.Label(forecast, text="CASHFLOW TIMELINE", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(forecast, text="Last 14 days, today, and next 45 days: income, bills/card minimums, and dated spending transactions.", style="Subtitle.TLabel").pack(anchor="w", pady=(5, 10))
        self.upcoming_tree = self.tree(forecast, ("Date", "Type", "Name", "Amount", "Running cash"), (115, 150, 520, 130, 140))
        self.upcoming_tree.pack(fill="both", expand=True)
        self.upcoming_tree.tag_configure("income", background="#eaf7ef")
        self.upcoming_tree.tag_configure("outflow", background="#fdecec")
        self.upcoming_tree.tag_configure("neutral", background="#f7f8fa")
        self.upcoming_tree.bind("<MouseWheel>", self.scroll_tree)
        forecast_buttons = ttk.Frame(forecast, style="Card.TFrame")
        forecast_buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(forecast_buttons, text="Export upcoming CSV", command=self.export_upcoming_cashflow).pack(side="left")
        ttk.Button(forecast_buttons, text="Mark selected scheduled item paid", command=self.mark_upcoming_paid).pack(side="right")
        ttk.Button(forecast_buttons, text="Unmark selected paid item", command=self.unmark_upcoming_paid).pack(side="right", padx=(0, 8))
        ttk.Button(forecast_buttons, text="Set selected amount", command=self.set_upcoming_amount).pack(side="right", padx=(0, 8))
        ttk.Button(forecast_buttons, text="Clear selected amount", command=self.clear_upcoming_amount).pack(side="right", padx=(0, 8))

    def build_setup(self) -> None:
        header = ttk.Frame(self.setup_tab)
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="Setup", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="Configure bills, paychecks, and credit cards here. The Dashboard uses this data.", style="Subtitle.TLabel").pack(side="left", padx=14, pady=7)
        panes = ttk.Panedwindow(self.setup_tab, orient="horizontal")
        panes.pack(fill="both", expand=True)
        bill_frame = ttk.Frame(panes, style="Card.TFrame", padding=14)
        income_frame = ttk.Frame(panes, style="Card.TFrame", padding=14)
        panes.add(bill_frame, weight=3); panes.add(income_frame, weight=2)
        self.section_title(bill_frame, "Monthly bills", "Add bill", self.bill_dialog)
        self.bill_tree = self.tree(bill_frame, ("Name", "Due", "Category", "Payment method", "Amount"), (190, 70, 100, 165, 100))
        self.bill_tree.pack(fill="both", expand=True, pady=(10, 8))
        self.bill_tree.bind("<Double-1>", lambda _: self.edit_bill())
        self.action_buttons(bill_frame, self.edit_bill, self.delete_bill)
        self.section_title(income_frame, "Income", "Add income", self.income_dialog)
        self.income_tree = self.tree(income_frame, ("Name", "Frequency", "Pay day", "Amount"), (150, 90, 70, 100))
        self.income_tree.pack(fill="both", expand=True, pady=(10, 8))
        self.income_tree.bind("<Double-1>", lambda _: self.edit_income())
        self.action_buttons(income_frame, self.edit_income, self.delete_income)
        card_frame = ttk.Frame(self.setup_tab, style="Card.TFrame", padding=14)
        card_frame.pack(fill="x", pady=(14, 0))
        card_header = ttk.Frame(card_frame, style="Card.TFrame")
        card_header.pack(fill="x")
        ttk.Label(card_header, text="Credit cards", style="CardTitle.TLabel").pack(side="left")
        ttk.Button(card_header, text="+ Add card", style="Accent.TButton", command=self.card_dialog).pack(side="right")
        ttk.Button(card_header, text="Edit selected card", command=self.edit_card).pack(side="right", padx=(0, 8))
        ttk.Button(card_header, text="Delete selected card", command=self.delete_card).pack(side="right", padx=(0, 8))
        self.cc_tree = self.tree(card_frame, ("Card", "Tracked balance", "Available", "Limit", "APR", "Min. payment", "Due"), (180, 120, 100, 100, 75, 105, 65))
        self.cc_tree.pack(fill="x", pady=(10, 8))
        self.cc_tree.bind("<Double-1>", lambda _: self.edit_card())

    def build_spending(self) -> None:
        top = ttk.Frame(self.spending_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Spending", style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="Import bank PDF", command=self.import_pdf).pack(side="right", padx=(8, 0))
        ttk.Button(top, text="Import bank CSV", style="Accent.TButton", command=self.import_csv).pack(side="right")
        ttk.Label(self.spending_tab, text="Cashflow-account spending affects Dashboard cashflow. Credit-card spending affects card usage and Insights only.", style="Subtitle.TLabel").pack(anchor="w", pady=(6, 16))

        panes = ttk.Panedwindow(self.spending_tab, orient="horizontal")
        panes.pack(fill="both", expand=True)
        bank_frame = ttk.Frame(panes, style="Card.TFrame", padding=14)
        cc_frame = ttk.Frame(panes, style="Card.TFrame", padding=14)
        panes.add(bank_frame, weight=3)
        panes.add(cc_frame, weight=3)

        self.section_title(bank_frame, "Cashflow account transactions", "Add cashflow transaction", self.transaction_dialog)
        ttk.Label(bank_frame, text="Use this for planned cashflow outflows, money in, and credit-card payments from checking.", style="Subtitle.TLabel").pack(anchor="w", pady=(8, 0))
        self.transaction_tree = self.tree(bank_frame, ("Date", "Account", "Description", "Category", "Amount", "Source"), (105, 130, 230, 145, 95, 120))
        self.transaction_tree.pack(fill="both", expand=True, pady=(10, 8))
        self.transaction_tree.bind("<Double-1>", lambda _: self.edit_transaction())
        bank_buttons = ttk.Frame(bank_frame, style="Card.TFrame")
        bank_buttons.pack(fill="x")
        ttk.Button(bank_buttons, text="Add money in", style="Accent.TButton", command=self.money_in_dialog).pack(side="left")
        ttk.Button(bank_buttons, text="Edit selected cashflow transaction", command=self.edit_transaction).pack(side="right", padx=(6, 0))
        ttk.Button(bank_buttons, text="Delete selected cashflow transaction", command=self.delete_transaction).pack(side="right")

        self.section_title(cc_frame, "Spending", "Add spending", self.cc_spending_dialog)
        ttk.Label(cc_frame, text="Choose the account used. Cashflow-account spending reduces cash today; credit-card spending tracks usage without touching cashflow.", style="Subtitle.TLabel").pack(anchor="w", pady=(8, 0))
        self.cc_spend_tree = self.tree(cc_frame, ("Date", "Account", "Description", "Category", "Amount", "Cashflow?"), (95, 160, 220, 130, 95, 85))
        self.cc_spend_tree.pack(fill="both", expand=True, pady=(10, 8))
        self.cc_spend_tree.bind("<Double-1>", lambda _: self.edit_spending())
        cc_buttons = ttk.Frame(cc_frame, style="Card.TFrame")
        cc_buttons.pack(fill="x")
        ttk.Button(cc_buttons, text="Edit selected spending", command=self.edit_spending).pack(side="right", padx=(6, 0))
        ttk.Button(cc_buttons, text="Delete selected spending", command=self.delete_spending).pack(side="right")

    def build_insights(self) -> None:
        controls = ttk.Frame(self.data_tab)
        controls.pack(fill="x", pady=(0, 12))
        ttk.Label(controls, text="Spending insights", style="Title.TLabel").pack(side="left")
        self.month_var = tk.StringVar(value=date.today().strftime("%Y-%m"))
        self.insight_month_choice = tk.StringVar(value=f"{date.today().month:02d}")
        self.insight_year_choice = tk.StringVar(value=str(date.today().year))
        self.insight_view_var = tk.StringVar(value="Full monthly obligations + spending")
        self.insight_category_var = tk.StringVar(value="All categories")
        ttk.Label(controls, text="Month:").pack(side="right", padx=(12, 4))
        ttk.Combobox(controls, textvariable=self.insight_month_choice, values=tuple(f"{n:02d}" for n in range(1, 13)), state="readonly", width=5).pack(side="right")
        ttk.Label(controls, text="Year:").pack(side="right", padx=(12, 4))
        ttk.Combobox(controls, textvariable=self.insight_year_choice, values=tuple(str(date.today().year + offset) for offset in range(-3, 3)), state="readonly", width=7).pack(side="right")
        ttk.Label(controls, text="Category:").pack(side="right", padx=(12, 4))
        self.insight_category_choice = ttk.Combobox(controls, textvariable=self.insight_category_var, values=("All categories",), state="readonly", width=20)
        self.insight_category_choice.pack(side="right")
        ttk.Label(controls, text="View:").pack(side="right", padx=(12, 4))
        ttk.Combobox(
            controls,
            textvariable=self.insight_view_var,
            values=(
                "Full monthly obligations + spending",
                "Spending transactions only",
                "Scheduled bills only",
                "Credit-card spending only",
                "Cashflow account spending only",
            ),
            state="readonly",
            width=31,
        ).pack(side="right")
        ttk.Button(controls, text="Apply", command=self.apply_insight_month).pack(side="right", padx=8)
        ttk.Button(controls, text="Restore a backup…", command=self.restore_backup).pack(side="right", padx=8)
        self.insight_total = tk.StringVar()
        self.insight_count = tk.StringVar()
        self.insight_top_category = tk.StringVar()
        self.insight_avg = tk.StringVar()
        summary = ttk.Frame(self.data_tab)
        summary.pack(fill="x", pady=(0, 12))
        for i in range(4): summary.columnconfigure(i, weight=1)
        self.card(summary, "Selected view total", self.insight_total, 0)
        self.card(summary, "Items", self.insight_count, 1)
        self.card(summary, "Top category", self.insight_top_category, 2)
        self.card(summary, "Average item", self.insight_avg, 3)
        chart_card = ttk.Frame(self.data_tab, style="Card.TFrame", padding=16)
        chart_card.pack(fill="both", expand=True)
        ttk.Label(chart_card, text="Spending by category", style="CardTitle.TLabel").pack(anchor="w")
        self.chart = tk.Canvas(chart_card, background="#ffffff", highlightthickness=0)
        self.chart.pack(fill="both", expand=True, pady=(10, 0))
        self.chart.bind("<Configure>", lambda _: self.draw_chart())

    def build_settings(self) -> None:
        header = ttk.Frame(self.settings_tab)
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="Settings", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="Backups, update checks, and app-level preferences.", style="Subtitle.TLabel").pack(side="left", padx=14, pady=7)

        update_card = ttk.Frame(self.settings_tab, style="Card.TFrame", padding=18)
        update_card.pack(fill="x")
        ttk.Label(update_card, text="APP UPDATES", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(update_card, text=f"Current version: {APP_VERSION}", style="Subtitle.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 4))
        ttk.Label(update_card, text=f"Updates come from the official Pocket Ledger releases: {RELEASES_PAGE_URL}", style="Subtitle.TLabel").grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 12))

        self.auto_update_var = tk.BooleanVar(value=self.db.setting("auto_update_check", "1") == "1")
        self.update_status_var = tk.StringVar(value="Not checked yet.")

        ttk.Checkbutton(update_card, text="Check for updates when Pocket Ledger starts", variable=self.auto_update_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Label(update_card, textvariable=self.update_status_var, style="Subtitle.TLabel").grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(update_card, text="Save settings", command=self.save_update_settings).grid(row=5, column=1, sticky="e", pady=(14, 0))
        ttk.Button(update_card, text="Check now", style="Accent.TButton", command=lambda: self.check_for_updates(silent=False)).grid(row=5, column=2, sticky="w", padx=(8, 0), pady=(14, 0))

    def save_update_settings(self):
        self.db.set_setting("auto_update_check", "1" if self.auto_update_var.get() else "0")
        self.update_status_var.set("Update settings saved.")

    def auto_check_updates(self):
        if self.db.setting("auto_update_check", "1") == "1":
            self.check_for_updates(silent=True)

    def show_whats_new_once(self):
        if self.db.setting("whats_new_seen_version", "") == APP_VERSION:
            return
        items = WHATS_NEW.get(APP_VERSION, [])
        if not items:
            self.db.set_setting("whats_new_seen_version", APP_VERSION)
            return
        message = f"Pocket Ledger {APP_VERSION}\n\n" + "\n".join(f"• {item}" for item in items)
        messagebox.showinfo("What's New", message)
        self.db.set_setting("whats_new_seen_version", APP_VERSION)

    def version_tuple(self, value: str) -> tuple[int, ...]:
        cleaned = value.strip().lower().lstrip("v")
        parts = []
        for piece in re.split(r"[^0-9]+", cleaned):
            if piece:
                parts.append(int(piece))
        return tuple(parts or [0])

    def update_download_dir(self) -> Path:
        downloads = Path.home() / "Downloads"
        folder = downloads if downloads.exists() else APP_DIR / "Updates"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def pick_release_asset(self, release: dict) -> dict | None:
        assets = release.get("assets") or []
        if not isinstance(assets, list):
            return None
        preferred = []
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
            if "pocket-ledger" in name or "pocket ledger" in name:
                score += 2
            if name.endswith(".exe"):
                score += 1
            preferred.append((score, asset))
        if not preferred:
            return None
        preferred.sort(key=lambda item: item[0], reverse=True)
        return preferred[0][1]

    def download_update_asset(self, asset: dict, tag: str) -> Path:
        raw_name = str(asset.get("name") or f"Pocket-Ledger-{tag}-windows.zip")
        safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", raw_name).strip() or f"Pocket-Ledger-{tag}-windows.zip"
        destination = self.update_download_dir() / safe_name
        url = asset["browser_download_url"]
        request = urllib.request.Request(url, headers={"User-Agent": "PocketLedger"})
        self.update_status_var.set(f"Downloading {tag}...")
        self.update_idletasks()
        with urllib.request.urlopen(request, timeout=60) as response:
            destination.write_bytes(response.read())
        return destination

    def open_downloaded_update(self, path: Path):
        try:
            os.startfile(path)
        except OSError:
            try:
                os.startfile(path.parent)
            except OSError:
                webbrowser.open(RELEASES_PAGE_URL)

    def check_for_updates(self, silent: bool = False):
        try:
            request = urllib.request.Request(RELEASES_API_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "PocketLedger"})
            with urllib.request.urlopen(request, timeout=8) as response:
                release = json.loads(response.read().decode("utf-8"))
            tag = str(release.get("tag_name", "")).strip()
            if tag and self.version_tuple(tag) > self.version_tuple(APP_VERSION):
                self.update_status_var.set(f"Update available: {tag}")
                asset = self.pick_release_asset(release)
                if not asset:
                    message = f"Pocket Ledger {tag} is available, but I could not find a downloadable Windows asset."
                    if not silent:
                        messagebox.showwarning("Update available", message)
                    self.update_status_var.set(message)
                    return
                if messagebox.askyesno(
                    "Update available",
                    f"Pocket Ledger {tag} is available.\n\nDownload {asset.get('name', 'the Windows update')} now?"
                ):
                    path = self.download_update_asset(asset, tag)
                    self.update_status_var.set(f"Downloaded {tag} to {path}")
                    messagebox.showinfo(
                        "Update downloaded",
                        "The update was downloaded.\n\n"
                        "Close Pocket Ledger, unzip/run the downloaded update, then reopen the app.\n\n"
                        f"Saved to:\n{path}",
                    )
                    self.open_downloaded_update(path)
            else:
                self.update_status_var.set(f"Pocket Ledger is up to date ({APP_VERSION}).")
                if not silent:
                    messagebox.showinfo("No update found", f"Pocket Ledger is up to date.\n\nCurrent version: {APP_VERSION}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as e:
            self.update_status_var.set("Could not check for updates.")
            if not silent:
                messagebox.showerror("Update check failed", f"Could not check GitHub releases.\n\n{e}")

    def section_title(self, parent, title, button, action) -> None:
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text=title, style="CardTitle.TLabel").pack(side="left")
        ttk.Button(row, text=f"+ {button}", style="Accent.TButton", command=action).pack(side="right")

    def tree(self, parent, cols, widths):
        view = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse")
        for col, width in zip(cols, widths):
            view.heading(col, text=col, command=lambda c=col, v=view: self.sort_tree(v, c, False))
            view.column(col, width=width, anchor="w")
        return view

    def sort_tree(self, tree, col, reverse):
        def key(value):
            text = str(value).replace("$", "").replace(",", "").replace("+", "").strip()
            ordinal = re.fullmatch(r"(\d+)(?:st|nd|rd|th)?", text.lower())
            if ordinal:
                return float(ordinal.group(1))
            try:
                return float(text)
            except ValueError:
                return str(value).lower()
        rows = [(key(tree.set(item, col)), item) for item in tree.get_children("")]
        rows.sort(reverse=reverse)
        for index, (_value, item) in enumerate(rows):
            tree.move(item, "", index)
        tree.heading(col, command=lambda: self.sort_tree(tree, col, not reverse))

    def action_buttons(self, parent, edit, delete) -> None:
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Button(row, text="Edit selected", command=edit).pack(side="right", padx=(6, 0))
        ttk.Button(row, text="Delete", command=delete).pack(side="right")

    def selected(self, tree):
        item = tree.focus()
        if not item: messagebox.showinfo("Select a row", "Select an item first."); return None
        return int(item)

    def selected_raw(self, tree):
        item = tree.focus()
        if not item:
            messagebox.showinfo("Select a row", "Select an item first.")
            return None
        return item

    def cash_account(self):
        account = self.db.one("SELECT * FROM cash_accounts WHERE ledger_id=? ORDER BY id LIMIT 1", (self.ledger_id,))
        if account:
            return account
        self.db.execute(
            "INSERT INTO cash_accounts(ledger_id,name,starting_balance,start_date,notes) VALUES(?,?,?,?,?)",
            (self.ledger_id, DEFAULT_CASH_ACCOUNT_NAME, 0, date.today().isoformat(), "Default cashflow account"),
        )
        return self.db.one("SELECT * FROM cash_accounts WHERE ledger_id=? ORDER BY id LIMIT 1", (self.ledger_id,))

    def spending_account_choices(self):
        cash = self.cash_account()
        choices = [f"cash:{cash['id']} - {cash['name']} (cashflow)"]
        choices.extend(f"card:{row['id']} - {row['name']}" for row in self.db.rows("SELECT id,name FROM cards WHERE ledger_id=? ORDER BY name", (self.ledger_id,)))
        return tuple(choices)

    def parse_account_choice(self, choice: str) -> tuple[str, int]:
        prefix, rest = choice.split(":", 1)
        account_id = int(rest.split(" - ", 1)[0])
        return prefix, account_id

    def center_child(self, win: tk.Toplevel) -> None:
        win.update_idletasks()
        parent_x, parent_y = self.winfo_rootx(), self.winfo_rooty()
        parent_w, parent_h = self.winfo_width(), self.winfo_height()
        width, height = win.winfo_width(), win.winfo_height()
        x = parent_x + max(0, (parent_w - width) // 2)
        y = parent_y + max(0, (parent_h - height) // 2)
        win.geometry(f"+{x}+{y}")

    def form(self, title, fields, initial, save):
        win = tk.Toplevel(self); win.title(title); win.transient(self); win.grab_set(); win.configure(bg="#f5f7fb"); win.resizable(False, False)
        body = ttk.Frame(win, padding=22); body.pack(fill="both", expand=True)
        values = {}
        for row, (name, kind, choices) in enumerate(fields):
            ttk.Label(body, text=name).grid(row=row, column=0, sticky="w", pady=6, padx=(0, 12))
            var = tk.StringVar(value=str(initial.get(name, ""))); values[name] = var
            if kind == "choice": widget = ttk.Combobox(body, textvariable=var, values=choices, state="readonly", width=27)
            elif kind == "day": widget = ttk.Combobox(body, textvariable=var, values=tuple(str(n) for n in range(1, 32)), state="readonly", width=27)
            else: widget = ttk.Entry(body, textvariable=var, width=30)
            widget.grid(row=row, column=1, pady=6, sticky="ew")
        def submit():
            try: save({key: var.get().strip() for key, var in values.items()}); win.destroy(); self.refresh_all()
            except ValueError as e: messagebox.showerror("Check the details", str(e), parent=win)
        ttk.Button(body, text="Save", style="Accent.TButton", command=submit).grid(row=len(fields), column=1, sticky="e", pady=(14, 0))
        self.center_child(win)
        win.wait_window()

    def bill_dialog(self, row=None):
        initial = {"Paid from": BANK_MANUAL} if row is None else {"Name":row["name"], "Date due":row["due_day"], "Amount":row["amount"], "Category":row["category"], "Paid from":row["paid_from"], "Notes":row["notes"]}
        def save(v):
            args=(v["Name"], self.int_day(v["Date due"]), self.num(v["Amount"]), v["Category"], v["Paid from"] or BANK_MANUAL, v["Notes"])
            if row: self.db.execute("UPDATE bills SET name=?,due_day=?,amount=?,category=?,paid_from=?,notes=? WHERE id=? AND ledger_id=?", args+(row["id"], self.ledger_id))
            else: self.db.execute("INSERT INTO bills(name,due_day,amount,category,paid_from,notes,ledger_id) VALUES(?,?,?,?,?,?,?)", args+(self.ledger_id,))
        self.form("Edit bill" if row else "Add bill", [("Name","text",()),("Date due","day",()),("Amount","text",()),("Category","choice",CATEGORIES),("Paid from","choice",PAYMENT_METHODS),("Notes","text",())], initial, save)

    def income_dialog(self, row=None):
        initial = {} if row is None else {"Name":row["name"], "Amount":row["amount"], "Frequency":row["frequency"], "Pay day":row["pay_day"] or "", "Notes":row["notes"]}
        def save(v):
            day = self.int_day(v["Pay day"]) if v["Pay day"] else None; args=(v["Name"],self.num(v["Amount"]),v["Frequency"],day,v["Notes"])
            if row: self.db.execute("UPDATE income SET name=?,amount=?,frequency=?,pay_day=?,notes=? WHERE id=? AND ledger_id=?",args+(row["id"], self.ledger_id))
            else: self.db.execute("INSERT INTO income(name,amount,frequency,pay_day,notes,ledger_id) VALUES(?,?,?,?,?,?)",args+(self.ledger_id,))
        self.form("Edit income" if row else "Add income", [("Name","text",()),("Amount","text",()),("Frequency","choice",("Monthly","Biweekly","Weekly","Annual")),("Pay day","day",()),("Notes","text",())], initial, save)

    def card_dialog(self, row=None):
        initial = {} if row is None else {"Card name":row["name"],"Balance":row["balance"],"Credit limit":row["credit_limit"],"APR %":row["apr"],"Minimum payment":row["minimum_payment"],"Due day":row["due_day"] or "", "Notes":row["notes"]}
        def save(v):
            day=self.int_day(v["Due day"]) if v["Due day"] else None; args=(v["Card name"],self.num(v["Balance"]),self.num(v["Credit limit"]),self.num(v["APR %"]),self.num(v["Minimum payment"]),day,v["Notes"])
            if row:self.db.execute("UPDATE cards SET name=?,balance=?,credit_limit=?,apr=?,minimum_payment=?,due_day=?,notes=? WHERE id=? AND ledger_id=?",args+(row["id"], self.ledger_id))
            else:self.db.execute("INSERT INTO cards(name,balance,credit_limit,apr,minimum_payment,due_day,notes,ledger_id) VALUES(?,?,?,?,?,?,?,?)",args+(self.ledger_id,))
        self.form("Edit card" if row else "Add credit card", [("Card name","text",()),("Balance","text",()),("Credit limit","text",()),("APR %","text",()),("Minimum payment","text",()),("Due day","day",()),("Notes","text",())], initial, save)

    def transaction_dialog(self, row=None):
        cash = self.cash_account()
        initial = {"Date":date.today().isoformat(),"Category":"Other"} if row is None else {"Date":row["trans_date"],"Description":row["description"],"Amount":row["amount"],"Category":row["category"]}
        def save(v):
            args=(self.valid_date(v["Date"]),v["Description"],self.num(v["Amount"]),v["Category"])
            if row: self.db.execute("UPDATE transactions SET trans_date=?,description=?,amount=?,category=? WHERE id=? AND ledger_id=?", args+(row["id"], self.ledger_id))
            else: self.db.execute("INSERT INTO transactions(trans_date,description,amount,category,source,account_id,ledger_id) VALUES(?,?,?,?,?,?,?)", args+("Manual", cash["id"], self.ledger_id))
        self.form("Edit bank transaction" if row else "Add transaction", [("Date","text",()),("Description","text",()),("Amount","text",()),("Category","choice",SPENDING_CATEGORIES)], initial, save)

    def money_in_dialog(self):
        cash = self.cash_account()
        def save(v):
            self.db.execute(
                "INSERT INTO transactions(trans_date,description,amount,category,source,account_id,ledger_id) VALUES(?,?,?,?,?,?,?)",
                (self.valid_date(v["Date"]), v["Description"], self.num(v["Amount"]), EXTRA_INCOME_CATEGORY, "Manual", cash["id"], self.ledger_id),
            )
        self.form("Add money in", [("Date","text",()),("Description","text",()),("Amount","text",())], {"Date":date.today().isoformat(),"Description":"Extra income"}, save)

    def cc_spending_dialog(self, row=None):
        choices = self.spending_account_choices()
        if row is None:
            initial = {"Date": date.today().isoformat(), "Account": choices[0], "Category": "Other"}
        else:
            row_kind = row["kind"] if "kind" in row.keys() else "card"
            if row_kind == "cash":
                account = self.cash_account()
                account_choice = f"cash:{account['id']} - {account['name']} (cashflow)"
            else:
                card_name = self.db.one("SELECT name FROM cards WHERE id=? AND ledger_id=?", (row["card_id"], self.ledger_id))
                account_choice = f"card:{row['card_id']} - {card_name['name'] if card_name else 'Unknown card'}"
            initial = {
                "Date": row["spend_date"] if "spend_date" in row.keys() else row["trans_date"],
                "Account": account_choice,
                "Description": row["description"],
                "Amount": row["amount"],
                "Category": row["category"],
                "Notes": row["notes"] if "notes" in row.keys() else "",
            }
        def save(v):
            kind, account_id = self.parse_account_choice(v["Account"])
            amount = self.num(v["Amount"])
            if row:
                old_kind = row["kind"] if "kind" in row.keys() else "card"
                if old_kind == kind == "cash":
                    self.db.execute(
                        "UPDATE transactions SET trans_date=?,description=?,amount=?,category=?,account_id=? WHERE id=? AND ledger_id=?",
                        (self.valid_date(v["Date"]), v["Description"], amount, v["Category"], account_id, row["id"], self.ledger_id),
                    )
                elif old_kind == kind == "card":
                    self.db.execute(
                        "UPDATE cc_spending SET spend_date=?,card_id=?,description=?,amount=?,category=?,notes=? WHERE id=? AND ledger_id=?",
                        (self.valid_date(v["Date"]), account_id, v["Description"], amount, v["Category"], v["Notes"], row["id"], self.ledger_id),
                    )
                else:
                    self.db.execute("DELETE FROM transactions WHERE id=? AND ledger_id=?" if old_kind == "cash" else "DELETE FROM cc_spending WHERE id=? AND ledger_id=?", (row["id"], self.ledger_id))
                    if kind == "cash":
                        self.db.execute(
                            "INSERT INTO transactions(trans_date,description,amount,category,source,account_id,ledger_id) VALUES(?,?,?,?,?,?,?)",
                            (self.valid_date(v["Date"]), v["Description"], amount, v["Category"], "Manual", account_id, self.ledger_id),
                        )
                    else:
                        self.db.execute(
                            "INSERT INTO cc_spending(spend_date,card_id,description,amount,category,notes,ledger_id) VALUES(?,?,?,?,?,?,?)",
                            (self.valid_date(v["Date"]), account_id, v["Description"], amount, v["Category"], v["Notes"], self.ledger_id),
                        )
            else:
                if kind == "cash":
                    self.db.execute(
                        "INSERT INTO transactions(trans_date,description,amount,category,source,account_id,ledger_id) VALUES(?,?,?,?,?,?,?)",
                        (self.valid_date(v["Date"]), v["Description"], amount, v["Category"], "Manual", account_id, self.ledger_id),
                    )
                else:
                    self.db.execute(
                        "INSERT INTO cc_spending(spend_date,card_id,description,amount,category,notes,ledger_id) VALUES(?,?,?,?,?,?,?)",
                        (self.valid_date(v["Date"]), account_id, v["Description"], amount, v["Category"], v["Notes"], self.ledger_id),
                    )
        self.form("Edit spending" if row else "Add spending", [("Date","text",()),("Account","choice",choices),("Description","text",()),("Amount","text",()),("Category","choice",SPENDING_CATEGORIES),("Notes","text",())], initial, save)

    def account_dialog(self):
        initial = {
            "Cashflow account name": self.cash_account()["name"],
            "Starting balance": self.cash_account()["starting_balance"],
            "As-of date": self.cash_account()["start_date"] or date.today().isoformat(),
        }
        def save(v):
            self.db.execute(
                "UPDATE cash_accounts SET name=?,starting_balance=?,start_date=? WHERE id=? AND ledger_id=?",
                (v["Cashflow account name"], self.num(v["Starting balance"]), self.valid_date(v["As-of date"]), self.cash_account()["id"], self.ledger_id),
            )
        self.form("Set cashflow account", [("Cashflow account name","text",()),("Starting balance","text",()),("As-of date","text",())], initial, save)

    def mark_upcoming_paid(self):
        item = self.upcoming_tree.focus()
        if not item:
            messagebox.showinfo("Select a row", "Select a scheduled cashflow row first.")
            return
        values = self.upcoming_tree.item(item, "values")
        if len(values) < 5:
            return
        event_date, event_type, event_name, amount_text, _running = values
        if event_type.startswith("Paid:"):
            messagebox.showinfo("Already paid", "That item is already marked paid.")
            return
        if event_type in ("Income", "Spending"):
            messagebox.showinfo("Not a scheduled bill", "Only scheduled bills and card minimums can be marked paid here.")
            return
        if not messagebox.askyesno("Mark paid", f"Mark this scheduled item paid?\n\n{event_date} - {event_type} - {event_name}"):
            return
        self.db.execute(
            "INSERT INTO paid_scheduled(event_date,event_type,event_name,amount,paid_date,notes,ledger_id) VALUES(?,?,?,?,?,?,?)",
            (event_date, event_type, event_name, abs(self.num(amount_text)), date.today().isoformat(), "Marked paid from dashboard", self.ledger_id),
        )
        self.refresh_all()

    def unmark_upcoming_paid(self):
        item = self.upcoming_tree.focus()
        if not item:
            messagebox.showinfo("Select a row", "Select a paid cashflow row first.")
            return
        values = self.upcoming_tree.item(item, "values")
        if len(values) < 5:
            return
        event_date, event_type, event_name, _amount_text, _running = values
        if not event_type.startswith("Paid: "):
            messagebox.showinfo("Not marked paid", "Select a row that starts with 'Paid:' to unmark it.")
            return
        original_type = event_type.replace("Paid: ", "", 1)
        if not messagebox.askyesno("Unmark paid", f"Put this scheduled item back into cashflow?\n\n{event_date} - {original_type} - {event_name}"):
            return
        self.db.execute(
            "DELETE FROM paid_scheduled WHERE ledger_id=? AND event_date=? AND event_type=? AND event_name=?",
            (self.ledger_id, event_date, original_type, event_name),
        )
        self.refresh_all()

    def set_upcoming_amount(self):
        item = self.upcoming_tree.focus()
        if not item:
            messagebox.showinfo("Select a row", "Select a scheduled cashflow row first.")
            return
        values = self.upcoming_tree.item(item, "values")
        if len(values) < 5:
            return
        event_date, event_type, event_name, amount_text, _running = values
        if event_type.startswith("Paid:"):
            event_type = event_type.replace("Paid: ", "", 1)
        if event_type in ("Income", "Spending", "Bill on credit card"):
            messagebox.showinfo("Not adjustable here", "Set amount overrides for scheduled bank bills or card minimums.")
            return
        initial = {
            "Amount": str(abs(self.num(amount_text))),
            "Notes": "Actual amount for this occurrence",
        }
        def save(v):
            existing = self.db.one(
                "SELECT id FROM scheduled_overrides WHERE ledger_id=? AND event_date=? AND event_type=? AND event_name=?",
                (self.ledger_id, event_date, event_type, event_name),
            )
            if existing:
                self.db.execute("UPDATE scheduled_overrides SET amount=?,notes=? WHERE id=? AND ledger_id=?", (self.num(v["Amount"]), v["Notes"], existing["id"], self.ledger_id))
            else:
                self.db.execute(
                    "INSERT INTO scheduled_overrides(event_date,event_type,event_name,amount,notes,ledger_id) VALUES(?,?,?,?,?,?)",
                    (event_date, event_type, event_name, self.num(v["Amount"]), v["Notes"], self.ledger_id),
                )
        self.form(f"Set amount for {event_name}", [("Amount","text",()),("Notes","text",())], initial, save)

    def clear_upcoming_amount(self):
        item = self.upcoming_tree.focus()
        if not item:
            messagebox.showinfo("Select a row", "Select a scheduled cashflow row first.")
            return
        values = self.upcoming_tree.item(item, "values")
        if len(values) < 5:
            return
        event_date, event_type, event_name, _amount_text, _running = values
        if event_type.startswith("Paid: "):
            event_type = event_type.replace("Paid: ", "", 1)
        if not messagebox.askyesno("Clear amount override", f"Use the normal recurring amount again?\n\n{event_date} - {event_type} - {event_name}"):
            return
        self.db.execute(
            "DELETE FROM scheduled_overrides WHERE ledger_id=? AND event_date=? AND event_type=? AND event_name=?",
            (self.ledger_id, event_date, event_type, event_name),
        )
        self.refresh_all()

    def edit_bill(self):
        key=self.selected(self.bill_tree)
        if key: self.bill_dialog(self.db.one("SELECT * FROM bills WHERE id=? AND ledger_id=?",(key,self.ledger_id)))
    def edit_income(self):
        key=self.selected(self.income_tree)
        if key: self.income_dialog(self.db.one("SELECT * FROM income WHERE id=? AND ledger_id=?",(key,self.ledger_id)))
    def edit_card(self):
        key=self.selected(self.cc_tree)
        if key: self.card_dialog(self.db.one("SELECT * FROM cards WHERE id=? AND ledger_id=?",(key,self.ledger_id)))
    def edit_cc_spending(self):
        self.edit_spending()
    def edit_spending(self):
        key=self.selected_raw(self.cc_spend_tree)
        if not key:
            return
        kind, raw_id = key.split(":", 1)
        if kind == "cash":
            row = self.db.one("SELECT *, 'cash' kind FROM transactions WHERE id=? AND ledger_id=?", (int(raw_id), self.ledger_id))
        else:
            row = self.db.one("SELECT *, 'card' kind FROM cc_spending WHERE id=? AND ledger_id=?", (int(raw_id), self.ledger_id))
        if row:
            self.cc_spending_dialog(row)
    def edit_transaction(self):
        key=self.selected(self.transaction_tree)
        if key: self.transaction_dialog(self.db.one("SELECT * FROM transactions WHERE id=? AND ledger_id=?",(key,self.ledger_id)))
    def delete(self, table, tree):
        key=self.selected(tree)
        label = {"bills": "bill", "income": "income source", "cards": "credit card", "transactions": "transaction", "cc_spending": "credit-card spending item"}.get(table, "item")
        if key and messagebox.askyesno("Delete", f"Delete the selected {label}?", parent=self): self.db.execute(f"DELETE FROM {table} WHERE id=? AND ledger_id=?",(key,self.ledger_id)); self.refresh_all()
    def delete_bill(self): self.delete("bills",self.bill_tree)
    def delete_income(self): self.delete("income",self.income_tree)
    def delete_card(self): self.delete("cards",self.cc_tree)
    def delete_transaction(self): self.delete("transactions",self.transaction_tree)
    def delete_cc_spending(self): self.delete("cc_spending",self.cc_spend_tree)
    def delete_spending(self):
        key = self.selected_raw(self.cc_spend_tree)
        if not key:
            return
        kind, raw_id = key.split(":", 1)
        label = "cashflow spending" if kind == "cash" else "credit-card spending"
        if not messagebox.askyesno("Delete", f"Delete the selected {label}?", parent=self):
            return
        table = "transactions" if kind == "cash" else "cc_spending"
        self.db.execute(f"DELETE FROM {table} WHERE id=? AND ledger_id=?", (int(raw_id), self.ledger_id))
        self.refresh_all()

    def import_csv(self):
        path=filedialog.askopenfilename(title="Choose bank statement CSV",filetypes=[("CSV files","*.csv")])
        if not path:return
        imported=0
        cash = self.cash_account()
        try:
            with open(path, newline="", encoding="utf-8-sig") as stream:
                for raw in csv.DictReader(stream):
                    data={str(k).strip().lower():str(v).strip() for k,v in raw.items() if k}
                    desc=next((data[k] for k in data if k in ("description","memo","name","transaction description")), "Imported transaction")
                    raw_date=next((data[k] for k in data if k in ("date","transaction date","posted date")), "")
                    raw_amount=next((data[k] for k in data if k in ("amount","debit","transaction amount")), "")
                    amount=self.num(raw_amount)
                    if amount <= 0: continue
                    self.db.execute(
                        "INSERT INTO transactions(trans_date,description,amount,category,source,account_id,ledger_id) VALUES(?,?,?,?,?,?,?)",
                        (self.valid_date(raw_date),desc,amount,"Other",Path(path).name,cash["id"],self.ledger_id),
                    ); imported+=1
            self.refresh_all(); messagebox.showinfo("Import complete", f"Imported {imported} spending transactions. Review categories in the database as needed.")
        except (OSError, csv.Error, ValueError) as e: messagebox.showerror("Could not import", f"That CSV could not be read: {e}")

    def import_pdf(self):
        path = filedialog.askopenfilename(title="Choose bank statement PDF", filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if not path:
            return
        if not messagebox.askyesno(
            "Experimental PDF import",
            "PDF bank statements are not clean data files. Pocket Ledger will try to find transaction-looking lines, "
            "but you should review the imported rows afterward. Continue?",
        ):
            return
        try:
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise RuntimeError("PDF import needs the optional pypdf package. Run: py -m pip install pypdf") from exc
            text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
            candidates = self.parse_pdf_transactions(text, Path(path).name)
            if not candidates:
                debug_path = APP_DIR / "last-pdf-import-text.txt"
                debug_path.write_text(text or "[No text could be extracted from this PDF.]", encoding="utf-8")
                messagebox.showinfo(
                    "Nothing imported",
                    "I could not find transaction-looking lines in that PDF. Some bank PDFs are laid out like images "
                    "or split the date/description/amount across columns. CSV/QFX/OFX will be more reliable when available.\n\n"
                    f"I saved the extracted text here so we can tune the importer:\n{debug_path}",
                )
                return
            with self.db.conn:
                self.db.conn.executemany(
                    "INSERT INTO transactions(trans_date,description,amount,category,source,account_id,ledger_id) VALUES(?,?,?,?,?,?,?)",
                    [row + (self.cash_account()["id"], self.ledger_id) for row in candidates],
                )
            self.refresh_all()
            messagebox.showinfo("PDF import complete", f"Imported {len(candidates)} possible spending transactions. Please review them.")
        except (OSError, RuntimeError, ValueError) as e:
            messagebox.showerror("Could not import PDF", str(e))

    def parse_pdf_transactions(self, text: str, source: str):
        transactions = []
        current_year = date.today().year
        skip_words = (
            "beginning balance", "ending balance", "total", "payment received", "deposit",
            "interest paid", "balance summary", "daily ledger", "account number", "page "
        )
        date_pattern = re.compile(r"\b(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b")
        money_pattern = re.compile(r"(?<!\w)-?\$?\d[\d,]*\.\d{2}(?!\w)")
        for line in text.splitlines():
            compact = " ".join(line.split())
            if not compact or any(word in compact.lower() for word in skip_words):
                continue
            date_match = date_pattern.search(compact)
            amounts = money_pattern.findall(compact)
            if not date_match or not amounts:
                continue
            raw_date = date_match.group(1)
            raw_amount = amounts[-1]
            description = compact[date_match.end():compact.rfind(raw_amount)].strip(" -")
            description = date_pattern.sub("", description).strip(" -")
            if len(description) < 3:
                description = "Imported PDF transaction"
            amount = abs(self.num(raw_amount))
            if amount == 0:
                continue
            if raw_date.count("/") == 1:
                raw_date = f"{raw_date}/{current_year}"
            trans_date = self.valid_date(raw_date)
            transactions.append((trans_date, description[:220], amount, "Other", source))
        return transactions

    def backup_data(self):
        """Create a consistent copy of the SQLite database without closing the app."""
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        path = filedialog.asksaveasfilename(
            title="Save Pocket Ledger backup", defaultextension=".db",
            initialfile=f"pocket-ledger-backup_{stamp}.db",
            filetypes=[("Pocket Ledger backup", "*.db"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with sqlite3.connect(path) as destination:
                self.db.conn.backup(destination)
            messagebox.showinfo("Backup complete", f"Your backup was saved to:\n{path}")
        except sqlite3.Error as e:
            messagebox.showerror("Backup failed", str(e))

    def export_full_data(self):
        """Export all user-entered data as a portable, readable JSON file."""
        stamp = datetime.now().strftime("%Y-%m-%d")
        path = filedialog.asksaveasfilename(
            title="Export all Pocket Ledger data", defaultextension=".json",
            initialfile=f"pocket-ledger-data_{stamp}.json",
            filetypes=[("Pocket Ledger data", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            payload = {
                "app": "Pocket Ledger", "format_version": 2,
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "ledger": dict(self.db.one("SELECT id,name,notes FROM ledgers WHERE id=?", (self.ledger_id,))),
                "cash_accounts": [dict(row) for row in self.db.rows("SELECT name,starting_balance,start_date,notes FROM cash_accounts WHERE ledger_id=?", (self.ledger_id,))],
                "bills": [dict(row) for row in self.db.rows("SELECT name,due_day,amount,category,paid_from,notes FROM bills WHERE ledger_id=?", (self.ledger_id,))],
                "income": [dict(row) for row in self.db.rows("SELECT name,amount,frequency,pay_day,notes FROM income WHERE ledger_id=?", (self.ledger_id,))],
                "cards": [dict(row) for row in self.db.rows("SELECT id,name,balance,credit_limit,apr,minimum_payment,due_day,notes FROM cards WHERE ledger_id=?", (self.ledger_id,))],
                "transactions": [dict(row) for row in self.db.rows("SELECT trans_date,description,amount,category,source FROM transactions WHERE ledger_id=?", (self.ledger_id,))],
                "cc_spending": [dict(row) for row in self.db.rows("SELECT spend_date,card_id,description,amount,category,notes FROM cc_spending WHERE ledger_id=?", (self.ledger_id,))],
                "paid_scheduled": [dict(row) for row in self.db.rows("SELECT event_date,event_type,event_name,amount,paid_date,notes FROM paid_scheduled WHERE ledger_id=?", (self.ledger_id,))],
                "scheduled_overrides": [dict(row) for row in self.db.rows("SELECT event_date,event_type,event_name,amount,notes FROM scheduled_overrides WHERE ledger_id=?", (self.ledger_id,))],
            }
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
            messagebox.showinfo("Export complete", f"All of your Pocket Ledger data was saved to:\n{path}")
        except (OSError, TypeError) as e:
            messagebox.showerror("Export failed", str(e))

    def import_full_data(self):
        path = filedialog.askopenfilename(
            title="Import Pocket Ledger data", filetypes=[("Pocket Ledger data", "*.json"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
            if data.get("app") != "Pocket Ledger" or not isinstance(data.get("format_version"), int):
                raise ValueError("That file is not a Pocket Ledger full-data export.")
            for table in ("bills", "income", "cards", "transactions"):
                if not isinstance(data.get(table), list):
                    raise ValueError(f"The import file is missing a valid {table} section.")
            if "settings" in data and not isinstance(data["settings"], list):
                raise ValueError("The import file has invalid settings data.")
            if "paid_scheduled" in data and not isinstance(data["paid_scheduled"], list):
                raise ValueError("The import file has invalid paid-scheduled data.")
            if "scheduled_overrides" in data and not isinstance(data["scheduled_overrides"], list):
                raise ValueError("The import file has invalid scheduled-overrides data.")
            if "cc_spending" in data and not isinstance(data["cc_spending"], list):
                raise ValueError("The import file has invalid credit-card spending data.")
            for bill in data["bills"]:
                if bill.get("paid_from") == BANK_ACCOUNT:
                    bill["paid_from"] = BANK_MANUAL
                bill.setdefault("paid_from", BANK_MANUAL)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            messagebox.showerror("Import failed", str(e))
            return
        if not messagebox.askyesno(
            "Import full data", "This will add the imported bills, income, cards, and transactions to the currently selected ledger. "
            "Make a backup first if you may want to undo it. Continue?"
        ):
            return
        try:
            cash = self.cash_account()
            card_id_map = {}
            with self.db.conn:
                self.db.conn.executemany(
                    "INSERT INTO bills(name,due_day,amount,category,paid_from,notes,ledger_id) VALUES(:name,:due_day,:amount,:category,:paid_from,:notes,:ledger_id)",
                    [dict(row, ledger_id=self.ledger_id) for row in data["bills"]]
                )
                self.db.conn.executemany(
                    "INSERT INTO income(name,amount,frequency,pay_day,notes,ledger_id) VALUES(:name,:amount,:frequency,:pay_day,:notes,:ledger_id)",
                    [dict(row, ledger_id=self.ledger_id) for row in data["income"]]
                )
                for card in data["cards"]:
                    old_id = card.get("id")
                    clean = {key: card.get(key) for key in ("name","balance","credit_limit","apr","minimum_payment","due_day","notes")}
                    cursor = self.db.conn.execute(
                        "INSERT INTO cards(name,balance,credit_limit,apr,minimum_payment,due_day,notes,ledger_id) VALUES(:name,:balance,:credit_limit,:apr,:minimum_payment,:due_day,:notes,:ledger_id)",
                        dict(clean, ledger_id=self.ledger_id),
                    )
                    if old_id is not None:
                        card_id_map[old_id] = cursor.lastrowid
                self.db.conn.executemany(
                    "INSERT INTO transactions(trans_date,description,amount,category,source,account_id,ledger_id) VALUES(:trans_date,:description,:amount,:category,:source,:account_id,:ledger_id)",
                    [dict(row, account_id=cash["id"], ledger_id=self.ledger_id) for row in data["transactions"]]
                )
                self.db.conn.executemany(
                    "INSERT INTO cc_spending(spend_date,card_id,description,amount,category,notes,ledger_id) VALUES(:spend_date,:card_id,:description,:amount,:category,:notes,:ledger_id)",
                    [dict(row, card_id=card_id_map.get(row.get("card_id"), row.get("card_id")), ledger_id=self.ledger_id) for row in data.get("cc_spending", [])]
                )
                self.db.conn.executemany(
                    "INSERT INTO paid_scheduled(event_date,event_type,event_name,amount,paid_date,notes,ledger_id) VALUES(:event_date,:event_type,:event_name,:amount,:paid_date,:notes,:ledger_id)",
                    [dict(row, ledger_id=self.ledger_id) for row in data.get("paid_scheduled", [])]
                )
                self.db.conn.executemany(
                    "INSERT OR IGNORE INTO scheduled_overrides(event_date,event_type,event_name,amount,notes,ledger_id) VALUES(:event_date,:event_type,:event_name,:amount,:notes,:ledger_id)",
                    [dict(row, ledger_id=self.ledger_id) for row in data.get("scheduled_overrides", [])]
                )
            self.refresh_all()
            messagebox.showinfo("Import complete", "Your exported data has been added to Pocket Ledger.")
        except (sqlite3.Error, KeyError, TypeError) as e:
            messagebox.showerror("Import failed", f"No data was imported.\n\n{e}")

    def restore_backup(self):
        path = filedialog.askopenfilename(
            title="Choose Pocket Ledger backup", filetypes=[("Pocket Ledger backup", "*.db"), ("All files", "*.*")]
        )
        if not path:
            return
        if not messagebox.askyesno(
            "Restore backup", "This replaces the data currently shown in Pocket Ledger. "
            "A safety backup of the current data will be saved first. Continue?"
        ):
            return
        safety_path = APP_DIR / f"before-restore_{datetime.now():%Y-%m-%d_%H%M%S}.db"
        try:
            with sqlite3.connect(safety_path) as safety:
                self.db.conn.backup(safety)
            with sqlite3.connect(path) as source:
                source.backup(self.db.conn)
            self.refresh_all()
            messagebox.showinfo("Restore complete", f"Backup restored. Your previous data was saved here:\n{safety_path}")
        except sqlite3.Error as e:
            messagebox.showerror("Restore failed", f"Your current data was not changed.\n\n{e}")

    def export_transactions(self):
        stamp = datetime.now().strftime("%Y-%m-%d")
        path = filedialog.asksaveasfilename(
            title="Export spending transactions", defaultextension=".csv",
            initialfile=f"pocket-ledger-spending_{stamp}.csv",
            filetypes=[("CSV spreadsheet", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            rows = self.db.rows("SELECT trans_date,description,amount,category,source FROM transactions WHERE ledger_id=? ORDER BY trans_date DESC,id DESC", (self.ledger_id,))
            with open(path, "w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(("Date", "Description", "Amount", "Category", "Source"))
                writer.writerows((r["trans_date"], r["description"], r["amount"], r["category"], r["source"]) for r in rows)
            messagebox.showinfo("Export complete", f"Exported {len(rows)} transactions to:\n{path}")
        except OSError as e:
            messagebox.showerror("Export failed", str(e))

    def export_upcoming_cashflow(self):
        self.refresh_all()
        rows = getattr(self, "current_upcoming", [])
        if not rows:
            messagebox.showinfo("Nothing to export", "There are no upcoming cashflow rows to export.")
            return
        stamp = datetime.now().strftime("%Y-%m-%d")
        path = filedialog.asksaveasfilename(
            title="Export upcoming cashflow", defaultextension=".csv",
            initialfile=f"pocket-ledger-upcoming-cashflow_{stamp}.csv",
            filetypes=[("CSV spreadsheet", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(("Date", "Type", "Name", "Amount", "Running cash"))
                writer.writerows((r["date"], r["kind"], r["name"], r["amount"], r["running"]) for r in rows)
            messagebox.showinfo("Export complete", f"Exported {len(rows)} upcoming cashflow rows to:\n{path}")
        except OSError as e:
            messagebox.showerror("Export failed", str(e))

    def num(self, value: str) -> float:
        try: return float(value.replace("$", "").replace(",", "").strip())
        except (TypeError, ValueError): raise ValueError("Enter a valid amount, such as 125.50.")
    def int_day(self, value: str) -> int:
        try:
            day=int(value)
            if not 1 <= day <= 31: raise ValueError
            return day
        except ValueError: raise ValueError("Day must be a number between 1 and 31.")
    def valid_date(self, value: str) -> str:
        for fmt in ("%Y-%m-%d","%m/%d/%Y","%m/%d/%y","%Y/%m/%d"):
            try:return datetime.strptime(value.strip(),fmt).date().isoformat()
            except ValueError:pass
        raise ValueError("Use a date like 2026-06-22 or 06/22/2026.")

    def parse_iso_date(self, value: str, fallback: date | None = None) -> date:
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except (AttributeError, ValueError):
            return fallback or date.today()

    def recurring_date_in_month(self, year: int, month: int, day: int) -> date:
        return date(year, month, min(day, calendar.monthrange(year, month)[1]))

    def dates_between(self, day: int | None, start: date, end: date) -> list[date]:
        if not day or start > end:
            return []
        found = []
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            candidate = self.recurring_date_in_month(year, month, day)
            if start <= candidate <= end:
                found.append(candidate)
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return found

    def scheduled_income_between(self, income, start: date, end: date) -> float:
        total = 0.0
        for row in income:
            for _ in self.dates_between(row["pay_day"], start, end):
                total += row["amount"]
        return total

    def scheduled_bills_between(self, bills, cards, start: date, end: date) -> float:
        bill_total = sum(row["amount"] * len(self.dates_between(row["due_day"], start, end)) for row in bills if is_bank_paid(row["paid_from"]))
        card_total = sum(row["minimum_payment"] * len(self.dates_between(row["due_day"], start, end)) for row in cards)
        return bill_total + card_total

    def bill_event_type(self, bill) -> str:
        if is_bank_paid(bill["paid_from"]):
            return "ACH bill" if bill["paid_from"] == BANK_ACH else "Bill"
        return "Bill on credit card"

    def paid_rows_between(self, start: date, end: date):
        return self.db.rows(
            "SELECT * FROM paid_scheduled WHERE ledger_id=? AND event_date BETWEEN ? AND ?",
            (self.ledger_id, start.isoformat(), end.isoformat()),
        )

    def paid_keys_between(self, start: date, end: date) -> set[tuple[str, str, str]]:
        return {(row["event_date"], row["event_type"], row["event_name"]) for row in self.paid_rows_between(start, end)}

    def event_key(self, event_date: date, event_type: str, event_name: str) -> tuple[str, str, str]:
        return (event_date.isoformat(), event_type, event_name)

    def override_rows_between(self, start: date, end: date):
        return self.db.rows(
            "SELECT * FROM scheduled_overrides WHERE ledger_id=? AND event_date BETWEEN ? AND ?",
            (self.ledger_id, start.isoformat(), end.isoformat()),
        )

    def overrides_between(self, start: date, end: date) -> dict[tuple[str, str, str], float]:
        return {
            (row["event_date"], row["event_type"], row["event_name"]): row["amount"]
            for row in self.override_rows_between(start, end)
        }

    def override_amount(self, overrides, event_date: date, event_type: str, event_name: str, default: float) -> float:
        return float((overrides or {}).get(self.event_key(event_date, event_type, event_name), default))

    def transaction_total_between(self, transactions, start: date, end: date) -> float:
        total = 0.0
        for row in transactions:
            trans_date = self.parse_iso_date(row["trans_date"], None)
            if start <= trans_date <= end:
                total += -row["amount"] if row["category"] == EXTRA_INCOME_CATEGORY else row["amount"]
        return total

    def credit_card_payment_total_between(self, transactions, start: date, end: date) -> float:
        total = 0.0
        for row in transactions:
            trans_date = self.parse_iso_date(row["trans_date"], None)
            if start <= trans_date <= end and row["category"] == "Credit Card Payment":
                total += row["amount"]
        return total

    def cash_balance_on(self, bank_start: float, start_date: date, target: date, income, bills, cards, transactions, paid_keys=None, overrides=None) -> float:
        if target <= start_date:
            return bank_start
        end = target - timedelta(days=1)
        income_received = self.scheduled_income_between(income, start_date, end)
        due_outflow = self.scheduled_checking_outflow_between(bills, cards, transactions, start_date, end, paid_keys, overrides)
        spending = self.transaction_total_between(transactions, start_date, end)
        return bank_start + income_received - due_outflow - spending

    def scheduled_checking_outflow_between(self, bills, cards, transactions, start: date, end: date, paid_keys=None, overrides=None) -> float:
        paid_keys = paid_keys or set()
        bill_total = 0.0
        for row in bills:
            if not is_bank_paid(row["paid_from"]):
                continue
            kind = self.bill_event_type(row)
            for event_date in self.dates_between(row["due_day"], start, end):
                if self.event_key(event_date, kind, row["name"]) not in paid_keys:
                    bill_total += self.override_amount(overrides, event_date, kind, row["name"], row["amount"])
        card_total = 0.0
        for row in cards:
            for event_date in self.dates_between(row["due_day"], start, end):
                if self.event_key(event_date, "Card minimum", row["name"]) not in paid_keys:
                    card_total += self.override_amount(overrides, event_date, "Card minimum", row["name"], row["minimum_payment"])
        return bill_total + card_total

    def upcoming_events(self, bills, income, cards, transactions, start_cash: float, start: date, end: date, paid_keys=None, overrides=None):
        paid_keys = paid_keys or set()
        events = []
        for row in income:
            for event_date in self.dates_between(row["pay_day"], start, end):
                events.append((event_date, "Income", row["name"], row["amount"]))
        for row in bills:
            for event_date in self.dates_between(row["due_day"], start, end):
                kind = self.bill_event_type(row)
                if self.event_key(event_date, kind, row["name"]) in paid_keys:
                    events.append((event_date, f"Paid: {kind}", row["name"], 0))
                elif is_bank_paid(row["paid_from"]):
                    events.append((event_date, kind, row["name"], -self.override_amount(overrides, event_date, kind, row["name"], row["amount"])))
                else:
                    events.append((event_date, kind, row["name"], 0))
        for row in cards:
            for event_date in self.dates_between(row["due_day"], start, end):
                if self.event_key(event_date, "Card minimum", row["name"]) in paid_keys:
                    events.append((event_date, "Paid: Card minimum", row["name"], 0))
                else:
                    events.append((event_date, "Card minimum", row["name"], -self.override_amount(overrides, event_date, "Card minimum", row["name"], row["minimum_payment"])))
        for row in transactions:
            event_date = self.parse_iso_date(row["trans_date"], None)
            if start <= event_date <= end:
                if row["category"] == EXTRA_INCOME_CATEGORY:
                    events.append((event_date, "Extra income", row["description"], row["amount"]))
                else:
                    events.append((event_date, "Spending", row["description"], -row["amount"]))
        events.sort(key=lambda item: (item[0], 0 if item[1] == "Income" else 1, item[2]))
        running = start_cash
        rows = []
        for event_date, kind, name, amount in events:
            running += amount
            rows.append({
                "id": f"{event_date.isoformat()}-{kind}-{name}-{len(rows)}",
                "date": event_date.isoformat(),
                "kind": kind,
                "name": name,
                "amount": amount,
                "running": running,
            })
        return rows

    def next_income_dates(self, income, start: date, count: int = 5) -> list[tuple[date, sqlite3.Row]]:
        found = []
        current = start
        safety = 0
        while len(found) < count and safety < 18:
            for row in income:
                if row["pay_day"]:
                    event_date = self.recurring_date_in_month(current.year, current.month, row["pay_day"])
                    if event_date >= start:
                        found.append((event_date, row))
            year, month = (current.year + 1, 1) if current.month == 12 else (current.year, current.month + 1)
            current = date(year, month, 1)
            unique = {}
            for event_date, row in found:
                unique[(event_date.isoformat(), row["id"])] = (event_date, row)
            found = sorted(unique.values(), key=lambda item: (item[0], item[1]["name"]))[:count]
            safety += 1
        return found

    def spending_room_periods(self, bills, income, cards, transactions, cash_today: float, today: date, paid_keys=None, overrides=None):
        paid_keys = paid_keys or set()
        pay_dates = self.next_income_dates(income, today + timedelta(days=1), 5)
        if not pay_dates:
            due = self.scheduled_checking_outflow_between(bills, cards, transactions, today + timedelta(days=1), today + timedelta(days=30), paid_keys, overrides)
            planned = self.transaction_total_between(transactions, today + timedelta(days=1), today + timedelta(days=30))
            due += planned
            after = cash_today - due
            safe = max(0, after)
            return [{
                "id": "no-income",
                "period": "Next 30 days",
                "starting": cash_today,
                "income": 0,
                "due": due,
                "after": after,
                "safe": safe,
                "daily": safe / 30,
            }]
        periods = []
        opening = cash_today
        period_start = today + timedelta(days=1)
        income_at_start = 0.0
        for idx, (payday, source) in enumerate(pay_dates[:5]):
            period_end = payday - timedelta(days=1)
            due = self.scheduled_checking_outflow_between(bills, cards, transactions, period_start, period_end, paid_keys, overrides)
            due += self.transaction_total_between(transactions, period_start, period_end)
            after = opening + income_at_start - due
            days = max(1, (period_end - period_start).days + 1)
            label = f"{period_start.strftime('%b %d')} - {period_end.strftime('%b %d')}" if period_start <= period_end else f"Before {payday.strftime('%b %d')}"
            periods.append({
                "id": f"room-{idx}",
                "period": label,
                "starting": opening,
                "income": income_at_start,
                "due": due,
                "after": after,
                "safe": after,
                "daily": after / days,
                "days": days,
            })
            opening = after
            period_start = payday
            income_at_start = source["amount"]
        for idx, row in enumerate(periods):
            lowest_upcoming = min(period["after"] for period in periods[idx:])
            protected_safe = max(0, lowest_upcoming)
            row["safe"] = protected_safe
            row["daily"] = protected_safe / row["days"]
        return periods

    def fill(self, tree, rows, formatter):
        tree.delete(*tree.get_children())
        for index, row in enumerate(rows):
            row_id = row["id"] if "id" in row.keys() else f"{tree}_{index}"
            tree.insert("", "end", iid=str(row_id), values=formatter(row))

    def fill_tagged(self, tree, rows, formatter, tagger):
        tree.delete(*tree.get_children())
        for index, row in enumerate(rows):
            row_id = row["id"] if "id" in row.keys() else f"{tree}_{index}"
            tree.insert("", "end", iid=str(row_id), values=formatter(row), tags=(tagger(row),))

    def scroll_tree(self, event):
        event.widget.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def refresh_all(self):
        self.refresh_ledger_choice()
        cash_account = self.cash_account()
        bills=self.db.rows("SELECT * FROM bills WHERE ledger_id=? ORDER BY due_day,name", (self.ledger_id,))
        income=self.db.rows("SELECT * FROM income WHERE ledger_id=? ORDER BY name", (self.ledger_id,))
        cards=self.db.rows("SELECT * FROM cards WHERE ledger_id=? ORDER BY name", (self.ledger_id,))
        transactions=self.db.rows("""
            SELECT t.*, COALESCE(a.name, ?) account_name
            FROM transactions t LEFT JOIN cash_accounts a ON a.id=t.account_id
            WHERE t.ledger_id=?
            ORDER BY t.trans_date DESC,t.id DESC
        """, (cash_account["name"], self.ledger_id))
        cc_spending=self.db.rows("""
            SELECT s.*, COALESCE(c.name,'Unknown card') card_name
            FROM cc_spending s LEFT JOIN cards c ON c.id=s.card_id
            WHERE s.ledger_id=?
            ORDER BY s.spend_date DESC,s.id DESC
        """, (self.ledger_id,))
        cc_totals = {row["id"]: 0.0 for row in cards}
        for row in cc_spending:
            if row["card_id"] in cc_totals:
                cc_totals[row["card_id"]] += row["amount"]
        self.fill(self.bill_tree,bills,lambda r:(r["name"],f"{r['due_day']}{self.suffix(r['due_day'])}",r["category"],r["paid_from"],money(r["amount"])))
        self.fill(self.income_tree,income,lambda r:(r["name"],r["frequency"],r["pay_day"] or "—",money(r["amount"])))
        self.fill(self.cc_tree,cards,lambda r:(r["name"],money(r["balance"]),money(r["credit_limit"]),f"{r['apr']:.2f}%",money(r["minimum_payment"]),r["due_day"] or "—"))
        self.fill(self.transaction_tree,transactions,lambda r:(r["trans_date"],r["account_name"],r["description"],r["category"],money(r["amount"]),r["source"]))
        self.fill(self.cc_tree,cards,lambda r:(r["name"],money(r["balance"]+cc_totals.get(r["id"],0)),money(r["credit_limit"]-(r["balance"]+cc_totals.get(r["id"],0))),money(r["credit_limit"]),f"{r['apr']:.2f}%",money(r["minimum_payment"]),r["due_day"] or "-"))
        mixed_spending = [
            {
                "id": f"cash:{row['id']}", "date": row["trans_date"], "account": row["account_name"],
                "description": row["description"], "category": row["category"], "amount": row["amount"], "cashflow": "Yes",
            }
            for row in transactions if row["category"] != EXTRA_INCOME_CATEGORY
        ] + [
            {
                "id": f"card:{row['id']}", "date": row["spend_date"], "account": row["card_name"],
                "description": row["description"], "category": row["category"], "amount": row["amount"], "cashflow": "No",
            }
            for row in cc_spending
        ]
        mixed_spending.sort(key=lambda r: (r["date"], r["id"]), reverse=True)
        self.fill(self.cc_spend_tree,mixed_spending,lambda r:(r["date"],r["account"],r["description"],r["category"],money(r["amount"]),r["cashflow"]))
        bank_start = float(cash_account["starting_balance"] or 0)
        bank_date = cash_account["start_date"] or date.today().isoformat()
        start_date = self.parse_iso_date(bank_date)
        today = date.today()
        history_start = max(start_date, today - timedelta(days=14))
        forecast_end = today + timedelta(days=45)
        paid_keys = self.paid_keys_between(start_date, forecast_end)
        overrides = self.overrides_between(start_date, forecast_end)
        income_received = self.scheduled_income_between(income, start_date, today)
        due_outflow = self.scheduled_checking_outflow_between(bills, cards, transactions, start_date, today, paid_keys, overrides)
        spending_row = self.db.one(
            "SELECT COALESCE(SUM(amount),0) total FROM transactions WHERE ledger_id=? AND trans_date BETWEEN ? AND ? AND category<>?",
            (self.ledger_id, start_date.isoformat(), today.isoformat(), EXTRA_INCOME_CATEGORY),
        )
        extra_income_row = self.db.one(
            "SELECT COALESCE(SUM(amount),0) total FROM transactions WHERE ledger_id=? AND trans_date BETWEEN ? AND ? AND category=?",
            (self.ledger_id, start_date.isoformat(), today.isoformat(), EXTRA_INCOME_CATEGORY),
        )
        actual_spending = spending_row["total"] if spending_row else 0
        extra_income = extra_income_row["total"] if extra_income_row else 0
        cash_today = bank_start + income_received + extra_income - due_outflow - actual_spending
        self.bank_start_var.set(money(bank_start))
        self.income_var.set(money(income_received))
        self.outflow_var.set(money(due_outflow))
        self.spent_var.set(money(actual_spending))
        self.projected_var.set(money(cash_today))
        self.card_var.set(money(sum(r["balance"] + cc_totals.get(r["id"], 0) for r in cards)))
        self.account_math_var.set(
            f"{money(bank_start)} {cash_account['name']} start ({bank_date}) + {money(income_received)} received income + {money(extra_income)} extra income "
            f"- {money(due_outflow)} bank/ACH bills and card mins due through today - {money(actual_spending)} spending "
            f"= {money(cash_today)} cash today"
        )
        future_transactions = [row for row in transactions if self.parse_iso_date(row["trans_date"]) > today]
        room = self.spending_room_periods(bills, income, cards, future_transactions, cash_today, today, paid_keys, overrides)
        self.fill_tagged(
            self.spending_room_tree,
            room,
            lambda r: (r["period"], money(r["starting"]), money(r["income"]), money(r["due"]), money(r["after"]), money(r["safe"]), money(r["daily"])),
            lambda r: "good" if r["after"] >= 0 and r["safe"] > 0 else "warning",
        )
        timeline_start_cash = self.cash_balance_on(bank_start, start_date, history_start, income, bills, cards, transactions, paid_keys, overrides)
        timeline_transactions = [row for row in transactions if self.parse_iso_date(row["trans_date"], today) >= history_start]
        upcoming = self.upcoming_events(bills, income, cards, timeline_transactions, timeline_start_cash, history_start, forecast_end, paid_keys, overrides)
        self.current_upcoming = upcoming
        incoming_total = sum(row["amount"] for row in upcoming if row["amount"] > 0)
        outgoing_total = abs(sum(row["amount"] for row in upcoming if row["amount"] < 0))
        running_values = [row["running"] for row in upcoming] or [cash_today]
        self.cashflow_income_var.set(money(incoming_total))
        self.cashflow_outflow_var.set(money(outgoing_total))
        self.cashflow_low_var.set(money(min(running_values)))
        self.cashflow_end_var.set(money(running_values[-1]))
        self.fill_tagged(
            self.upcoming_tree,
            upcoming,
            lambda r: (r["date"], r["kind"], r["name"], signed_money(r["amount"]), money(r["running"])),
            lambda r: "neutral" if r["amount"] == 0 else "income" if r["amount"] > 0 else "outflow",
        )
        self.refresh_cashflow(bills, income, cards, future_transactions, paid_keys, overrides)
        self.refresh_insights()

    def refresh_cashflow_legacy(self, bills, income, cards):
        """Old one-line payday plan kept only as a reference; refresh_cashflow below is active."""
        dated_income = [row for row in income if row["pay_day"]]
        if not dated_income:
            self.cashflow_var.set("Add a pay day to an income source to see bills due before your next paycheck.")
            return
        today = date.today()
        next_pays = []
        for row in dated_income:
            # A pay day is monthly for planning purposes; frequency still controls the monthly income total.
            last_day = calendar.monthrange(today.year, today.month)[1]
            candidate = date(today.year, today.month, min(row["pay_day"], last_day))
            if candidate < today:
                year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
                candidate = date(year, month, min(row["pay_day"], calendar.monthrange(year, month)[1]))
            next_pays.append((candidate, row))
        payday, source = min(next_pays, key=lambda item: item[0])
        period_days = (payday - today).days
        due_bills = []
        for bill in bills:
            year, month = today.year, today.month
            bill_date = date(year, month, min(bill["due_day"], calendar.monthrange(year, month)[1]))
            if bill_date < today:
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
                bill_date = date(year, month, min(bill["due_day"], calendar.monthrange(year, month)[1]))
            if bill_date <= payday:
                due_bills.append(bill["amount"])
        due_cards = []
        for card in cards:
            if not card["due_day"]:
                continue
            year, month = today.year, today.month
            card_date = date(year, month, min(card["due_day"], calendar.monthrange(year, month)[1]))
            if card_date < today:
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
                card_date = date(year, month, min(card["due_day"], calendar.monthrange(year, month)[1]))
            if card_date <= payday:
                due_cards.append(card)
        card_due = [card["minimum_payment"] for card in due_cards]
        due_total = sum(due_bills) + sum(card_due)
        # Preserve the actual scheduled names (including bills that share an amount).
        due_names = []
        for bill in bills:
            year, month = today.year, today.month
            bill_date = date(year, month, min(bill["due_day"], calendar.monthrange(year, month)[1]))
            if bill_date < today:
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
                bill_date = date(year, month, min(bill["due_day"], calendar.monthrange(year, month)[1]))
            if bill_date <= payday:
                due_names.append(bill["name"])
        due_names += [f"{card['name']} minimum" for card in due_cards]
        names_text = ", ".join(due_names[:5]) + ("…" if len(due_names) > 5 else "")
        self.cashflow_var.set(
            f"Next: {source['name']} on {payday.strftime('%b')} {payday.day} ({period_days} days). "
            f"{len(due_bills)} bills and {len(card_due)} card minimums are due before then: {money(due_total)}"
            + (f" — {names_text}" if names_text else ".")
        )
    def refresh_cashflow(self, bills, income, cards, transactions=None, paid_keys=None, overrides=None):
        """Summarize the next paycheck; item details live in the dashboard table."""
        dated_income = [row for row in income if row["pay_day"]]
        if not dated_income:
            self.cashflow_var.set("Add pay days to income sources to see your next paycheck forecast.")
            return
        today = date.today()
        next_pays = []
        for row in dated_income:
            candidate = self.recurring_date_in_month(today.year, today.month, row["pay_day"])
            if candidate <= today:
                year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
                candidate = self.recurring_date_in_month(year, month, row["pay_day"])
            next_pays.append((candidate, row))
        payday, source = min(next_pays, key=lambda item: item[0])
        due_items = self.upcoming_events(bills, [], cards, transactions or [], 0, today + timedelta(days=1), payday, paid_keys or set(), overrides or {})
        due_total = abs(sum(row["amount"] for row in due_items if row["amount"] < 0))
        net_after_due = source["amount"] - due_total
        self.cashflow_var.set(
            f"Next income: {source['name']} pays {money(source['amount'])} on {payday.strftime('%b')} {payday.day}. "
            f"Before that date, {len(due_items)} scheduled bill/card items total {money(due_total)}. "
            f"Estimated room after those items: {signed_money(net_after_due)}. "
            "The table below shows the item-by-item forecast."
        )

    def suffix(self, day): return "th" if 10 < day % 100 < 14 else {1:"st",2:"nd",3:"rd"}.get(day % 10,"th")
    def apply_insight_month(self):
        self.month_var.set(f"{self.insight_year_choice.get()}-{self.insight_month_choice.get()}")
        self.refresh_all()

    def insight_month_range(self, month: str) -> tuple[date, date]:
        start = datetime.strptime(month, "%Y-%m").date().replace(day=1)
        end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
        return start, end

    def insight_records(self, month: str) -> list[dict]:
        start, end = self.insight_month_range(month)
        records = []
        for row in self.db.rows(
            "SELECT category,description,amount FROM transactions WHERE ledger_id=? AND trans_date BETWEEN ? AND ? AND category<>?",
            (self.ledger_id, start.isoformat(), end.isoformat(), EXTRA_INCOME_CATEGORY),
        ):
            records.append({"category": row["category"], "description": row["description"], "source": "Cashflow account", "amount": float(row["amount"] or 0), "view": "cashflow"})
        for row in self.db.rows(
            "SELECT s.category,s.description,s.amount,COALESCE(c.name,'Unknown card') card_name FROM cc_spending s LEFT JOIN cards c ON c.id=s.card_id WHERE s.ledger_id=? AND s.spend_date BETWEEN ? AND ?",
            (self.ledger_id, start.isoformat(), end.isoformat()),
        ):
            records.append({"category": row["category"], "description": row["description"], "source": f"Credit card: {row['card_name']}", "amount": float(row["amount"] or 0), "view": "card"})
        for bill in self.db.rows("SELECT * FROM bills WHERE ledger_id=? ORDER BY due_day,name", (self.ledger_id,)):
            for due_date in self.dates_between(bill["due_day"], start, end):
                records.append({"category": bill["category"], "description": bill["name"], "source": self.bill_event_type(bill), "amount": float(bill["amount"] or 0), "view": "scheduled", "date": due_date.isoformat()})
        for card in self.db.rows("SELECT * FROM cards WHERE ledger_id=? AND due_day IS NOT NULL ORDER BY due_day,name", (self.ledger_id,)):
            for due_date in self.dates_between(card["due_day"], start, end):
                records.append({"category": "Credit Card Minimums", "description": card["name"], "source": "Scheduled card minimum", "amount": float(card["minimum_payment"] or 0), "view": "scheduled", "date": due_date.isoformat()})
        return records

    def filtered_insight_records(self, records: list[dict]) -> list[dict]:
        view = self.insight_view_var.get()
        if view == "Spending transactions only":
            records = [row for row in records if row["view"] in ("cashflow", "card")]
        elif view == "Scheduled bills only":
            records = [row for row in records if row["view"] == "scheduled"]
        elif view == "Credit-card spending only":
            records = [row for row in records if row["view"] == "card"]
        elif view == "Cashflow account spending only":
            records = [row for row in records if row["view"] == "cashflow"]
        category = self.insight_category_var.get()
        if category and category != "All categories":
            records = [row for row in records if row["category"] == category]
        return records

    def refresh_insights(self):
        month=self.month_var.get().strip()
        all_records = self.insight_records(month)
        categories = ("All categories",) + tuple(sorted({row["category"] for row in all_records}))
        current_category = self.insight_category_var.get()
        self.insight_category_choice["values"] = categories
        if current_category not in categories:
            self.insight_category_var.set("All categories")
        records = self.filtered_insight_records(all_records)
        grouped = {}
        detail_grouped = {}
        for row in records:
            grouped.setdefault(row["category"], {"category": row["category"], "total": 0.0, "count": 0})
            grouped[row["category"]]["total"] += row["amount"]
            grouped[row["category"]]["count"] += 1
            key = (row["category"], row["description"], row["source"])
            detail_grouped.setdefault(key, {"category": row["category"], "description": row["description"], "source": row["source"], "total": 0.0, "count": 0})
            detail_grouped[key]["total"] += row["amount"]
            detail_grouped[key]["count"] += 1
        rows=sorted(grouped.values(), key=lambda row: row["total"], reverse=True)
        details=sorted(detail_grouped.values(), key=lambda row: (row["category"], -row["total"], row["description"]))
        total=sum(r["total"] for r in rows); count=sum(r["count"] for r in rows)
        top_category = rows[0]["category"] if rows else "—"
        avg = total / count if count else 0
        self.insight_total.set(money(total)); self.insight_count.set(str(count)); self.insight_top_category.set(top_category); self.insight_avg.set(money(avg)); self.chart_data=rows; self.insight_details=details
        self.draw_chart()
        return
        rows=self.db.rows("""
            SELECT category,SUM(amount) total,COUNT(*) count FROM (
                SELECT category,amount FROM transactions WHERE ledger_id=? AND substr(trans_date,1,7)=? AND category<>?
                UNION ALL
                SELECT category,amount FROM cc_spending WHERE ledger_id=? AND substr(spend_date,1,7)=?
            )
            GROUP BY category ORDER BY total DESC
        """,(self.ledger_id,month,EXTRA_INCOME_CATEGORY,self.ledger_id,month))
        total=sum(r["total"] for r in rows); count=sum(r["count"] for r in rows)
        details=self.db.rows("""
            SELECT category,description,source,SUM(amount) total,COUNT(*) count FROM (
                SELECT category,description,'Cashflow account' source,amount FROM transactions WHERE ledger_id=? AND substr(trans_date,1,7)=? AND category<>?
                UNION ALL
                SELECT category,description,'Credit card' source,amount FROM cc_spending WHERE ledger_id=? AND substr(spend_date,1,7)=?
            )
            GROUP BY category,description,source
            ORDER BY category,total DESC,description
        """,(self.ledger_id,month,EXTRA_INCOME_CATEGORY,self.ledger_id,month))
        top_category = rows[0]["category"] if rows else "—"
        avg = total / count if count else 0
        self.insight_total.set(money(total)); self.insight_count.set(str(count)); self.insight_top_category.set(top_category); self.insight_avg.set(money(avg)); self.chart_data=rows; self.insight_details=details
        self.draw_chart()
    def draw_chart(self):
        canvas=self.chart; canvas.delete("all"); rows=getattr(self,"chart_data",[]); details=getattr(self,"insight_details",[])
        w=max(canvas.winfo_width(),650); h=max(canvas.winfo_height(),350)
        if not rows:
            canvas.create_text(w/2,h/2,text="No spending data for this month yet.",fill="#748198",font=("Segoe UI",12))
            return
        by_category={}
        for detail in details:
            by_category.setdefault(detail["category"],[]).append(detail)
        maximum=max(r["total"] for r in rows) or 1
        y=24; usable=w-260
        colors=("#2a9d96","#74b7b2","#a7d8d4","#d8eeec")
        for r in rows[:8]:
            category=r["category"]; total=r["total"]
            canvas.create_text(14,y,text=category,anchor="w",fill="#25324a",font=("Segoe UI Semibold",11))
            canvas.create_text(w-14,y,text=money(total),anchor="e",fill="#25324a",font=("Segoe UI Semibold",11))
            y+=16
            length=(total/maximum)*usable
            canvas.create_rectangle(14,y,14+length,y+16,fill="#2a9d96",width=0)
            canvas.create_rectangle(14+length,y,14+usable,y+16,fill="#eef3f7",width=0)
            y+=24
            category_details=by_category.get(category,[])[:5]
            if not category_details:
                y+=10
                continue
            detail_max=max(d["total"] for d in category_details) or 1
            for idx,d in enumerate(category_details):
                desc=d["description"][:42] + ("..." if len(d["description"]) > 42 else "")
                source=f"{d['source']} • {d['count']}x"
                canvas.create_text(34,y+8,text=desc,anchor="w",fill="#40516d",font=("Segoe UI",9))
                canvas.create_text(330,y+8,text=source,anchor="w",fill="#748198",font=("Segoe UI",8))
                mini=(d["total"]/detail_max)*max(80,usable-450)
                canvas.create_rectangle(445,y+3,445+mini,y+13,fill=colors[idx % len(colors)],width=0)
                canvas.create_text(w-14,y+8,text=money(d["total"]),anchor="e",fill="#40516d",font=("Segoe UI",9))
                y+=20
            y+=16


if __name__ == "__main__":
    LedgerApp().mainloop()
