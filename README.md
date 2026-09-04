# Telegram Expenses Bot

Python Telegram bot for splitting group expenses with SQLite storage.

## Features

- Groups with invite links and join codes.
- One base currency per group; an expense paid in another currency keeps what
  was actually handed over, next to the rate it worked out to.
- Expense wizard with inline buttons: payer, participants, equal/custom split.
- Fast participant selection: all, me and payer, clear.
- An expense card with the full split, the receipt photo, and edit/delete.
- Expense editing and deletion by the user who created the expense.
- Receipt photos attached to an expense.
- A single debts screen: one net figure per person per currency across every
  shared group, with the per-group breakdown that produced it.
- Chained debt simplification: if A owes B and B owes C, B drops out and A pays
  C directly. Opposite debts in different groups cancel, so there is nothing to
  transfer and the screen says so.
- Payments are claimed by the payer and confirmed by the person who was owed
  the money; nothing moves on the balances until then.
- Partial payments: hand over part of a debt and leave the rest standing.
- Group settings: rename, base currency (while the group is empty), remove a
  member, leave the group.
- Excel export per group: every expense with each person's share, the
  per-person totals the debts are derived from, and a sheet explaining how
  to redo the arithmetic by hand.
- Per-user time zone for every timestamp the bot shows.
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
- `STATE_PATH`: wizard state file, defaults to `./bot_state.pickle`.
- `DEFAULT_CURRENCY`: base currency for new groups, defaults to `RUB`.
- `DEFAULT_TZ_OFFSET`: time zone new users start with, e.g. `+03:00`,
  defaults to UTC.

Commands: `/start`, `/join <код>`, `/cancel`, `/tz <смещение>`.

## Data Model

The app applies migrations automatically on startup and records applied versions in `schema_migrations`.

Important permission rules:

- Viewing groups, expenses, members, and payments requires group membership.
- Adding an expense requires group membership.
- Editing or deleting an expense, and attaching a receipt to it, requires
  being the user who created it.
- Deleting a group, renaming it, changing its currency and removing members
  requires being the group owner.
- Confirming a payment requires being the person it was paid to; either side
  can withdraw an unconfirmed claim.
- Cancelling a confirmed payment requires being the payer or the group owner.
- Settling with a person records one entry per shared group, so cancelling a
  single entry reopens only that group's part of the debt.
- Leaving a group, or being removed from it, requires a zero balance there:
  the debt screen only walks the groups a person still belongs to, so a debt
  left behind would exist for one side only.

For old databases, migration `002_expense_created_by` backfills `created_by_tg_id` from `payer_tg_id`.

A reply keyboard lives on the Telegram client until the bot sends a new one, so
`users.keyboard_version` records which layout each user has been shown. Bump
`KEYBOARD_VERSION` when `main_keyboard()` changes: users holding an older layout
are sent the new one on their next message, and labels from retired keyboards
stay routed (`_LEGACY_DEBT_BUTTONS`) so the buttons still on their screen work.

## Currencies

Every group has one base currency (`groups.currency`). All of
`expenses.amount_cents`, the settlements and every balance are in it, so the
arithmetic never has to guess a rate.

An expense paid in another currency is entered as `100 EUR ужин`; the bot then
asks what left the payer's account in the group's currency and stores both
(`orig_currency`, `orig_amount_cents`). The rate is derived for display only.

Debts are computed per currency and never netted across them — a debt in lira
is not repaid by a credit in roubles. The currency can only be changed while
the group has no expenses or payments, because every stored amount is already
denominated in it.

## Payments

`request_settlement()` records what the payer says they handed over, with
`confirmed_by_to = 0`, one row per shared group sharing a `batch` id. Balances
count only confirmed rows, so an unconfirmed claim changes nothing.

The recipient confirms or rejects the whole batch; either side can withdraw it
while it is open. Only one open claim is allowed per pair per currency, so an
impatient second tap cannot pay the same debt twice.

Paying the full net closes the debt in every shared group and in both
directions. A smaller amount is spread over the groups where the payer owes,
largest debt first, and leaves the rest standing.

## Excel Export

«📊 Выгрузить в Excel» on a group screen sends an `.xlsx` with five sheets:

- `Траты` — one row per expense with a share column per member, so each row
  shows how the amount was cut up and each column what one person consumed;
  plus what was paid in a foreign currency, the rate, and receipt/edit marks.
- `Итоги по людям` — paid, consumed, settlements sent and received, and the
  balance they add up to; the balance column always sums to zero.
- `Кто кому платит` — the same balances as the minimum set of transfers.
- `Платежи` — settlements, each marked confirmed or still awaiting confirmation.
- `Как проверить` — the formulas above in words.

The file is written by `build_xlsx()` in `main.py`: a zip of the few
SpreadsheetML parts Excel needs, so the export adds no dependency. Amounts are
written as numbers with a `0.00` format — not text — so columns can be summed
in the spreadsheet. Deleted expenses are excluded, as they are from the debts.
Timestamps are rendered in the requesting user's time zone.

## Time Zones

Telegram never tells the bot a user's zone, so each user sets their own with
`/tz +3` (`users.tz_offset_min`); new users start at `DEFAULT_TZ_OFFSET`.
Timestamps are formatted from UTC plus that offset, never from the server's
local time.

## Runtime Notes

- Wizard state lives in `user_data` and is persisted with `PicklePersistence`,
  so a restart in the middle of adding an expense does not strand the user.
- `run_polling(drop_pending_updates=False)`: an expense sent while the bot was
  down should still land.
- Screens are split into Telegram-sized messages by `split_message()` — an
  over-long message is refused outright, not truncated, so a long debts screen
  would otherwise never arrive.
- The two slow operations — computing debts across every group and building a
  workbook — run in a worker thread so they do not block other users.

## Tests

Run unit tests without starting Telegram polling:

```bash
python -B -m unittest discover -s tests -p "test_*.py"
```
