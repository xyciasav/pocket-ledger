Pocket Ledger v0.2.47 Qt Preview

- Adds a “Pay selected card purchase” button to the Spending page.
- Selecting a credit-card purchase can now create the matching Cash Activity card payment automatically.
- The generated payment reduces the selected cash account once and ties the payment to the credit card.
- Prevents duplicate linked payments for the same Spending row, avoiding accidental double subtraction from checking.
- Cash-account purchases are ignored by this workflow because they already affect their account directly.
