# Telegram Expenses Bot

Python Telegram bot for splitting group expenses with SQLite storage.

## Features

- Groups with invite links and join codes.
- Expense wizard with inline buttons: payer, participants, equal/custom split.
- Fast participant selection: all, me and payer, clear.
- Group balances and cross-group netting.
- Idempotent payment confirmation from inline buttons.
- Payment history with cancellation by payer or group owner.
- Expense deletion by the user who created the expense.
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

For old databases, migration `002_expense_created_by` backfills `created_by_tg_id` from `payer_tg_id`.

## Tests

Run unit tests without starting Telegram polling:

```bash
python -B -m unittest discover -s tests -p "test_*.py"
```
