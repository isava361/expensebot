# Telegram Expenses Bot

Python Telegram bot for splitting group expenses with SQLite storage.

## Features

- Groups with invite links and join codes; the link lives on the group card.
- One base currency per group; an expense paid in another currency is
  converted at the day's rate, keeps what was actually handed over, and the
  rate can be replaced with the amount the bank really took.
- Expense wizard with inline buttons: payer, participants, equal/custom split.
- A review card before every save, including edits: check the amount, payer
  and shares, change them, or cancel without writing to the ledger.
- Fast participant selection: all, me and payer, clear.
- An expense card with the full split, the receipt photo, and edit/delete.
- Expense editing and deletion by the user who created the expense.
- Expense history with actor, time, and before/after values; deletion can be
  undone from its confirmation or from the deleted-expenses list.
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
- Atomic writes and migrations, verified SQLite backups, and a restore command.

## Quick Start

For the Telegram Mini App at the root of a separate HTTPS subdomain, see
[`deploy/README.md`](deploy/README.md). It includes the isolated Ubuntu systemd
service, Nginx 8443 vhost, Certbot webroot renewal, reload verification and bot
menu setup. The Mini App reuses this bot's ledger and validates Telegram
`initData` on every API request. Set `MINIAPP_URL` to enable it; `MINIAPP_PORT`
defaults to the candidate loopback port 18082 (check availability before deploying).

The Mini App covers groups, invites, expenses (create, edit and delete, in the
group's currency or a foreign one converted at the day's rate), group settings
(rename, base currency while the group is empty, remove a member, leave) and
the debts screen with payments. It drives Telegram's own main and back buttons
and haptics rather than drawing its own chrome. Expense history, receipt photos
and Excel export have no Mini App screen yet and stay in the chat with the bot.

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
- `DEFAULT_CURRENCY`: base currency for new groups, defaults to `RUB`;
  an unrecognised code falls back to `RUB` rather than being stored.
- `DEFAULT_TZ_OFFSET`: time zone new users start with, e.g. `+03:00`,
  defaults to UTC.
- `RATES_URL`: exchange-rate endpoint with a `{base}` placeholder, defaults
  to `https://open.er-api.com/v6/latest/{base}`. Set it empty to turn
  automatic conversion off and always ask for the amount.
- `RATES_TTL`: seconds a fetched rate table is reused, defaults to 21600.
- `BACKUP_DIR`: directory for automatic database snapshots; defaults to
  `backups/` alongside `DB_PATH`. Use a separate disk or a backed-up directory
  if the snapshots should also survive loss of the database disk.
- `BACKUP_INTERVAL`: seconds between runtime snapshots, defaults to 86400
  (one day), must be positive. An existing database is also backed up before
  startup migrations; another snapshot starts when the bot connects.

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

## Reviewing and changing expenses

Choosing equal shares or entering the last custom share opens a review card;
only **Сохранить** changes balances or sends notifications. The card lets you
change the amount/description, payer, participants or split. For the last
custom share the bot offers the exact remainder as a button. A different
entered amount is rejected rather than silently replaced.

Every creation, edit, receipt change, deletion and restoration is recorded in
`expense_history` in the same transaction as the expense. The expense card's
**История изменений** shows who acted, when, and what changed, with pagination.
History starts with this update: for older expenses their first new change
captures the previous state, but earlier overwritten versions cannot be recovered.

Deleting a single expense preserves its receipt and shares. Its author can
undo deletion immediately or open **Траты → Удалённые траты** later and restore
it. Other current group members can read the history. Restoring an expense
requires its payer and participants to still belong to the group. Changing
an old expense is also refused if it would recreate a balance for a departed
member. Deleting an entire group still removes that group's expenses and history.

Notifications for expense edits, deletion and restoration include everyone
affected in either the old or new split, including both payers. Delivery is
best-effort: a user who blocked the bot will not receive a notification.
An edit started against an older revision is refused if the expense has
changed meanwhile; reopen its card to edit the current version.

## Navigation

The reply keyboard is permanent screen furniture, so it carries only the daily
actions:

```
[🧾 Добавить трату]
[💰 Долги]  [👥 Мои группы]
```

Everything else hangs off the screen it belongs to. Creating a group sits on
the group list, which is also where somebody with no groups yet lands.
Inviting people sits on the group card, next to the `/join` command itself —
it used to have a keyboard button of its own, which was a second route to the
same card.

The card is deliberately short:

```
Сочи · #1 · RUB
Приглашение: /join <код>
<долги по этой группе>

[🧾 Добавить трату]
[📋 Траты]      [💸 Платежи]
[🔗 Пригласить] [📊 Excel]
[⚙️ Настройки]  [« Группы]
```

Debts are not on it: that screen spans every group and every currency, and a
button for it here would read as if it showed this group alone — the card
links to the keyboard button in words instead. Members live in the settings,
next to the buttons that add and remove them.

A reply keyboard lives on the Telegram client until the bot sends a new one, so
`users.keyboard_version` records which layout each user has been shown. Bump
`KEYBOARD_VERSION` when `main_keyboard()` changes: users holding an older layout
are sent the new one on their next message, and labels from retired keyboards
stay routed (`_LEGACY_DEBT_BUTTONS`, `_LEGACY_GROUP_BUTTONS`) so the buttons
still on their screen work — «🔗 Приглашение» opens the group list, from which
the card is one tap away.

## Currencies

Every group has one base currency (`groups.currency`). All of
`expenses.amount_cents`, the settlements and every balance are in it, so the
arithmetic never has to guess a rate.

An amount with no currency on it is simply in the group's currency — that is
the everyday case and it asks nothing extra. An expense paid in another
currency is entered as `100 EUR ужин`; the bot converts it at the day's rate
and shows what it got:

```
100.00 EUR ≈ 10075.57 RUB
Курс: 1 EUR = 100.7557 RUB (на 2026-09-04 03:02)
Если банк списал другую сумму — пришлите её числом.
```

Sending a number replaces the converted amount — the others will check the
split against a bank statement, and a card rate with its spread is rarely the
market one. Both figures are stored (`orig_currency`, `orig_amount_cents`
alongside `amount_cents`); the rate itself is not, it is derived from the pair
for display.

Rates come from `open.er-api.com` (no key, updated daily) and are cached per
base currency for `RATES_TTL`. Every lookup is best-effort: if the provider is
unreachable, slow or does not know the currency, the bot falls back to asking
for the amount, so nothing about adding an expense depends on the network
beyond Telegram itself.

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

Pending requests and confirmation messages show the actual net transfer:
1000 RUB owed in one group and 400 RUB owed in the reverse direction mean a
600 RUB payment. Both ledger entries are still retained for correct group
balances. New batches explicitly store the requester and recipient, including
zero-net offsets. Older batches lack this metadata and infer direction from
their ledger rows; for an old zero-net batch its original initiator is unknown.

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

The file is written by `build_xlsx()` in `workbook.py`: a zip of the few
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
- Repository mutations roll back on errors, including failures while writing
  shares or history. Amounts must be integer cents, positive and within limits;
  shares must be nonnegative and add up exactly. Database triggers also reject
  invalid individual expense/share amounts.
- A unique draft operation ID prevents re-saving a persisted draft from
  creating another expense. Save buttons are tied to the current review card.
- Migration SQL and its version marker are committed together. A startup
  backup failure prevents migration of an existing database. Runtime backup
  failures are logged, and the next scheduled attempt still runs.

## Backups and recovery

Snapshots use SQLite's backup API, so committed data in WAL is included.
Each completed snapshot passes `PRAGMA integrity_check` and a check for the
bot's core tables. The tests also restore a snapshot and compare balances and
history. Backups cover the ledger, users and history; the separate wizard
state pickle and bot token are not included. Automatic snapshots are retained
without pruning, so monitor the backup directory's disk usage.

Create a backup without starting Telegram (uses `DB_PATH`):

```powershell
python main.py backup --output .\backups\manual.db
```

Restore to a **new** database file:

```powershell
python main.py restore --backup .\backups\manual.db --output .\recovered.db
```

Both commands refuse to overwrite an existing destination. Stop the bot before
switching it to the restored file, then start with a new wizard-state path so
drafts from a newer ledger cannot be applied accidentally:

```powershell
$env:DB_PATH = ".\recovered.db"
$env:STATE_PATH = ".\recovered-state.pickle"
python main.py
```

Keep the original database until the restored data has been checked. Startup
migrations also work on restored databases from older versions.

## Code layout

- `main.py`: entry point, backup/restore CLI and runtime lifecycle; retains
  imports used by the original tests and integrations.
- `core.py`: money arithmetic, parsing, formatting and shared defaults.
- `repository.py`: SQLite ledger, permissions, migrations and history.
- `storage.py`: transaction wrapper, snapshot verification and restoration.
- `handlers.py`: Telegram screens, expense wizard and notifications.
- `rates.py`: cached currency conversion.
- `workbook.py`: XLSX writer and group reports.
- `miniapp.py`: Mini App HTTP server, `initData` validation and its API.
- `web/`: the Mini App itself, as ES modules — `lib/money.js` (splitting and
  formatting, no DOM), `lib/api.js` (requests, the memory cache and the
  offline queue), `lib/tg.js` (Telegram buttons, haptics, theme, viewport),
  `lib/dom.js` (the shared widgets), `expense.js` and `app.js` (the screens).

## Tests

Run unit tests without starting Telegram polling:

```bash
python -B -m unittest discover -s tests -p "test_*.py"
```

The Mini App's money arithmetic and offline queue are covered by `tests/test_web.js`, which
the command above runs through `node --test` when node is installed and skips
when it is not. To run it alone:

```bash
node --test tests/test_web.js
```

Browser regressions cover expense editing, concurrent currency conversions,
storage failures, pagination and local dates. They use mocked API responses
and do not connect to Telegram or a database. Install the optional browser
test dependency and run:

```bash
npm install --no-save --package-lock=false playwright
npx playwright install chromium
node --test tests/test_web_browser.cjs
```

Set `MINIAPP_BROWSER_CHANNEL=msedge` to use an installed Microsoft Edge instead
of Playwright's Chromium.
