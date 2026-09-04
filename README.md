# Telegram Expenses Bot

Python Telegram bot for splitting group expenses with SQLite storage.

## Features

- Groups with invite links and join codes.
- Expense wizard with inline buttons: payer, participants, equal/custom split.
- Fast participant selection: all, me and payer, clear.
- A single debts screen: one net figure per person across every shared group,
  with the per-group breakdown that produced it.
- Chained debt simplification: if A owes B and B owes C, B drops out and A pays
  C directly. Opposite debts in different groups cancel, so there is nothing to
  transfer and the screen says so.
- Settling with someone closes their debts in every shared group at once.
- Idempotent payment confirmation from inline buttons.
- Payment history with cancellation by payer or group owner.
- Expense deletion by the user who created the expense.
- Excel export per group: every expense with each person's share, the
  per-person totals the debts are derived from, and a sheet explaining how
  to redo the arithmetic by hand.
- SQLite migrations in `migrations/`.

## Quick Start

1. Install Python 3.11+.
2. Create a Telegram bot with `@BotFather` and copy the token.
3. Install dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

4. Run the bot:

   ```bash
   export BOT_TOKEN=123456:ABC...
   python main.py
   ```

On Windows PowerShell:

```powershell
$env:BOT_TOKEN = "123456:ABC..."
python main.py
```

Optional environment variables:

- `DB_PATH`: SQLite database path, defaults to `./data.db`.

## Data Model

The app applies migrations automatically on startup and records applied versions in `schema_migrations`.

Important permission rules:

- Viewing groups, expenses, members, and payments requires group membership.
- Adding an expense requires group membership.
- Deleting an expense requires being the user who created it.
- Deleting a group requires being the group owner.
- Cancelling a payment requires being the payer who confirmed it or the group owner.
- Settling with a person records one entry per shared group, so cancelling a
  single entry reopens only that group's part of the debt.

For old databases, migration `002_expense_created_by` backfills `created_by_tg_id` from `payer_tg_id`.

A reply keyboard lives on the Telegram client until the bot sends a new one, so
`users.keyboard_version` records which layout each user has been shown. Bump
`KEYBOARD_VERSION` when `main_keyboard()` changes: users holding an older layout
are sent the new one on their next message, and labels from retired keyboards
stay routed (`_LEGACY_DEBT_BUTTONS`) so the buttons still on their screen work.

## Excel Export

«📊 Выгрузить в Excel» on a group screen sends an `.xlsx` with five sheets:

- `Траты` — one row per expense with a share column per member, so each row
  shows how the amount was cut up and each column what one person consumed.
- `Итоги по людям` — paid, consumed, settlements sent and received, and the
  balance they add up to; the balance column always sums to zero.
- `Кто кому платит` — the same balances as the minimum set of transfers.
- `Платежи` — settlements already recorded.
- `Как проверить` — the formulas above in words.

The file is written by `build_xlsx()` in `main.py`: a zip of the few
SpreadsheetML parts Excel needs, so the export adds no dependency. Amounts are
written as numbers with a `0.00` format — not text — so columns can be summed
in the spreadsheet. Deleted expenses are excluded, as they are from the debts.

## Tests

Run unit tests without starting Telegram polling:

```bash
python -B -m unittest discover -s tests -p "test_*.py"
```
