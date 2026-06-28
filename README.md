# Pocket Ledger

A local-first Windows desktop budget tracker. Your data stays in a SQLite database at `%USERPROFILE%\PocketLedger\budget.db`.

## Included now

- Dashboard tab with cash today, next payday, and spending-room windows
- Cashflow tab with the 45-day upcoming cashflow forecast and paid/override controls
- Cashflow tab includes forecast summary cards for lowest cash, incoming, outgoing, and ending cash
- Scrollable dashboard so the full forecast remains reachable on smaller screens
- Dashboard summary cards are grouped visually with cash today first
- Setup tab for monthly bills, income sources, and credit cards
- Monthly bills with due date, amount, category, notes, and add/edit/delete controls
- Bills can be marked as `Bank ACH / autopay`, `Bank account / manual`, or `Credit card / elsewhere`, so cashflow does not double-count them
- Income tracking with pay days, so future paychecks are not counted before they arrive
- Credit-card balances, limits, APR, minimum payments, and due dates
- Bank starting balance with a clear cashflow row:
  `bank start + income received since start - bills/card minimums due since start - spending since start = cash today`
- One-off money-in entries for side cash, reimbursements, and other extra income that increase cashflow without counting as spending
- Conservative spending-room view showing what is left for groceries, gas, food, and misc spending while protecting the lowest upcoming cash point
- Future-dated spending transactions show in cashflow as planned spending and reduce spending room
- Spending tab separates bank cashflow transactions from credit-card spending; CC spending feeds Insights and card usage without touching Dashboard cashflow
- Tables can be sorted by clicking column headers
- Bank cashflow transactions can be edited from the Spending tab
- Add/edit popup windows center over the app, and Insights uses month/year selectors instead of typed month text
- Future-dated `Credit Card Payment` transactions count as checking outflows; card minimums still show until that due occurrence is explicitly marked paid
- Upcoming scheduled bills/card minimums can be marked paid for a specific due date, preventing duplicate subtraction when you pay early
- Variable bills can use an estimated recurring amount, then a specific upcoming occurrence can be overridden with the real amount
- Upcoming Cashflow can be exported to CSV from the Cashflow tab
- Manual spending transactions, CSV bank-statement import, and experimental PDF import
- Monthly category chart and summary metrics
- Insights show category bars with visual description/merchant breakdowns underneath each category
- Insights include top category and average item summary cards
- Settings tab for GitHub release update checks
- A light, businesslike interface: Dashboard, Cashflow, Setup, Spending, Insights, and Settings
- Portable full-data JSON export/import, database backup/restore, settings export/import, and a spending CSV export

## Keeping and moving your data

Use **Export full data** to create one portable `.json` file containing all bills, income, cards, transactions, and settings such as the bank starting balance. Keep that file in OneDrive, Google Drive, or on a USB drive. Use **Import full data** on another installation to bring it back. The import intentionally adds to existing bills/cards/transactions, so it is safe to test, but avoid importing the same export twice unless you want duplicates.

**Back up data** and **Restore a backup** are a second, complete recovery option that use the app's native `.db` database format.

## Run it

The easiest option is to download the Windows ZIP from the GitHub release and run `Pocket Ledger.exe`.

If running from source, install the standard Python 3.11+ for Windows from python.org. Pocket Ledger uses Tkinter, so the Python you use must pass:

```powershell
py -c "import tkinter"
```

Then double-click `app.py`, or run:

```powershell
py app.py
```

## Build the EXE

Double-click `build_exe.bat`. The resulting app is at `dist\Pocket Ledger\Pocket Ledger.exe`.

## Updates

Pocket Ledger can check GitHub releases from the **Settings** tab. The default release repository is `xyciasav/pocket-ledger`. When a newer release is found, the app asks before opening the GitHub release page.

## Bank imports

The CSV importer recognizes common date columns (`Date`, `Transaction Date`) and common amount/description columns (`Amount`, `Debit`, `Description`, `Memo`). It imports positive values as spending. Different banks often export debit amounts in different signs or separate debit/credit fields, so the next sensible enhancement is a per-bank import mapping screen.

There is also an experimental **Import bank PDF** option for banks such as Bank of America. PDFs are not clean transaction files, so the app tries to extract transaction-looking lines from the statement text and imports them as `Other`. Review those rows afterward. If Bank of America offers CSV/QFX/OFX for your account, that will be more reliable than PDF.

## Next enhancements

- Category editing and automatic merchant categorization
- Per-bank import preview/mapping, including duplicate transaction detection
- Recurring bill payment history and reminders
- Optional local-LLM connection settings for forecast/advice; this should remain opt-in and never send data anywhere by default
- Budget targets by spending category and richer charts
