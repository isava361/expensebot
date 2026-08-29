#!/usr/bin/env python3
"""
Expense-splitting Telegram bot (python-telegram-bot v21+ + SQLite).

Requirements: pip install "python-telegram-bot>=21.0"

ENV:
  BOT_TOKEN=<telegram bot token>
  DB_PATH=./data.db
"""

import base64
import html
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

GROUPS_PER_PAGE = 5
EXPENSES_PER_PAGE = 10
MEMBERS_PER_PAGE = 15
SETTLEMENTS_PER_PAGE = 10
MAX_AMOUNT_CENTS = 1_000_000_000
# Bump whenever main_keyboard() changes: a reply keyboard lives on the client
# until the bot sends a new one, so users have to be pushed the new layout.
KEYBOARD_VERSION = 1
MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# ---------- Utils ----------

_AMOUNT_WITH_DESC_RE = re.compile(
    r"^\s*(?P<amount>[+-]?(?:(?:\d{1,3}(?:[ _]\d{3})+|\d+)(?:[.,]\d*)?|[.,]\d+))"
    r"(?=$|\s)(?:\s+(?P<desc>.*))?$"
)


def now_unix() -> int:
    return int(time.time())


def cents_from_str(s: str) -> int:
    """Parse monetary string to integer cents.

    Bug fix vs Go original: '12.' now correctly returns 1200 (not 12).
    The Go code failed to pad an empty frac string, so ParseInt('12') = 12 cents.
    """
    s = s.strip().replace(",", ".")
    if not s:
        raise ValueError("empty amount")

    negative = s.startswith("-")
    if s[0] in "+-":
        s = s[1:]
    if not s:
        raise ValueError("empty amount after sign")

    if " " in s or "_" in s:
        if not re.fullmatch(r"\d{1,3}(?:[ _]\d{3})+(?:\.\d*)?", s):
            raise ValueError("invalid thousands separator")
        s = s.replace(" ", "").replace("_", "")

    if s.count(".") > 1:
        raise ValueError("invalid amount")

    if "." in s:
        int_part, frac = s.split(".", 1)
        if not int_part and not frac:
            raise ValueError("empty amount")
        int_part = int_part or "0"
        if not int_part.isdigit() or (frac and not frac.isdigit()):
            raise ValueError("invalid amount")
        if len(frac) > 2:
            raise ValueError("too many decimal places")
        frac = (frac + "00")[:2]
        value = int(int_part) * 100 + int(frac)
    else:
        if not s.isdigit():
            raise ValueError("invalid amount")
        value = int(s) * 100

    value = -value if negative else value
    if abs(value) > MAX_AMOUNT_CENTS:
        raise ValueError("amount too large")
    return value


def split_amount_and_description(text: str) -> tuple[str, str]:
    match = _AMOUNT_WITH_DESC_RE.match(text)
    if not match:
        return "", ""
    amount = match.group("amount")
    desc = (match.group("desc") or "").strip()
    first_desc_token = desc.split(maxsplit=1)[0] if desc else ""
    plain_amount = amount.lstrip("+-")
    if (
        first_desc_token
        and " " not in amount
        and "_" not in amount
        and re.fullmatch(r"\d{1,3}", plain_amount)
        and re.fullmatch(r"\d[\d.,_]*", first_desc_token)
    ):
        return "", ""
    return amount, desc


def format_cents(c: int) -> str:
    sign = ""
    if c < 0:
        sign = "-"
        c = -c
    return f"{sign}{c // 100}.{c % 100:02d}"


def format_time(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def rand_code() -> str:
    b = secrets.token_bytes(8)
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


# ---------- Invite-code parsing ----------

_CODE_RE = re.compile(r"[a-zA-Z0-9\-_]+")


def extract_start_code_from_text(raw: str) -> str:
    s = (
        raw.replace(" ", " ")
        .replace(" ", " ")
        .replace(" ", " ")
        .strip()
    )
    m = re.search(r"(?i)start=([a-zA-Z0-9\-_]+)", s)
    return m.group(1) if m else ""


def extract_bare_code(raw: str) -> str:
    s = raw.strip().replace("+", " ")
    if re.search(r"\s", s):
        return ""
    if not 6 <= len(s) <= 64:
        return ""
    return s if _CODE_RE.fullmatch(s) else ""


# ---------- Debt netting ----------

def settle_net(net: dict[int, int]) -> dict[tuple[int, int], int]:
    """Turn per-user net positions into a minimal set of directed debts.

    ``net`` maps a user id to their balance in cents: positive means the
    group owes them, negative means they owe the group. The sum over all
    users is zero.

    Debts are chained through intermediaries, not just netted pairwise: if
    A owes B and B owes C, B drops out and A pays C directly. The greedy
    largest-debtor/largest-creditor matching keeps the number of transfers
    small and is deterministic for a given ``net``, which matters because
    the payment buttons carry the computed amount and are re-validated
    against a freshly computed balance.
    """
    debtors = sorted(
        ((uid, -v) for uid, v in net.items() if v < 0),
        key=lambda t: (-t[1], t[0]),
    )
    creditors = sorted(
        ((uid, v) for uid, v in net.items() if v > 0),
        key=lambda t: (-t[1], t[0]),
    )

    result: dict[tuple[int, int], int] = {}
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        debtor, owed = debtors[i]
        creditor, due = creditors[j]
        amount = min(owed, due)
        key = (debtor, creditor)
        result[key] = result.get(key, 0) + amount
        owed -= amount
        due -= amount
        debtors[i] = (debtor, owed)
        creditors[j] = (creditor, due)
        if owed == 0:
            i += 1
        if due == 0:
            j += 1

    return result


# ---------- Repo ----------

class Repo:
    def __init__(self, db_path: str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        if not MIGRATIONS_DIR.exists():
            raise RuntimeError(f"migrations directory not found: {MIGRATIONS_DIR}")

        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations("
                "version TEXT PRIMARY KEY,"
                "applied_at INTEGER NOT NULL"
                ")"
            )
            applied = {
                row["version"]
                for row in self._conn.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = path.stem
                if version in applied:
                    continue
                if version == "002_expense_created_by":
                    self._migrate_expense_created_by()
                    self._conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (version, now_unix()),
                    )
                    continue
                sql = path.read_text(encoding="utf-8")
                self._conn.executescript(sql)
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                    (version, now_unix()),
                )
            self._conn.commit()

    def _column_exists(self, table: str, column: str) -> bool:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row["name"] == column for row in rows)

    def _migrate_expense_created_by(self) -> None:
        if not self._column_exists("expenses", "created_by_tg_id"):
            self._conn.execute("ALTER TABLE expenses ADD COLUMN created_by_tg_id INTEGER")
        self._conn.execute(
            "UPDATE expenses SET created_by_tg_id=payer_tg_id "
            "WHERE created_by_tg_id IS NULL"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- users --

    def upsert_user(self, tg_id: int, name: str) -> None:
        with self._lock:
            # New users are stamped with the current keyboard: they are shown
            # it by /start, so they must not be told it changed.
            self._conn.execute(
                "INSERT INTO users(tg_id,name,keyboard_version) VALUES(?,?,?)"
                " ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name",
                (tg_id, name, KEYBOARD_VERSION),
            )
            self._conn.commit()

    def has_stale_keyboard(self, uid: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT keyboard_version FROM users WHERE tg_id=?", (uid,)
            ).fetchone()
        return row is not None and row["keyboard_version"] < KEYBOARD_VERSION

    def mark_keyboard_current(self, uid: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET keyboard_version=? WHERE tg_id=?",
                (KEYBOARD_VERSION, uid),
            )
            self._conn.commit()

    def user_name(self, uid: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM users WHERE tg_id=?", (uid,)
            ).fetchone()
        return row["name"] if row else str(uid)

    # -- groups --

    def create_group(self, title: str, owner: int) -> tuple[int, str]:
        code = rand_code()
        with self._lock:
            cur = self._conn.execute(
                'INSERT INTO "groups"(title,owner_tg_id,invite_code,created_at)'
                " VALUES(?,?,?,?)",
                (title, owner, code, now_unix()),
            )
            gid = cur.lastrowid
            self._conn.execute(
                "INSERT INTO group_members(group_id,tg_id,role) VALUES(?,?,?)",
                (gid, owner, "owner"),
            )
            self._conn.commit()
        return gid, code

    def join_by_code(self, code: str, uid: int) -> tuple[int, str]:
        with self._lock:
            row = self._conn.execute(
                'SELECT id,title FROM "groups" WHERE invite_code=?', (code,)
            ).fetchone()
            if row is None:
                raise ValueError("invalid invite code")
            gid, title = row["id"], row["title"]
            self._conn.execute(
                "INSERT INTO group_members(group_id,tg_id,role) VALUES(?,?,?)"
                " ON CONFLICT(group_id,tg_id) DO NOTHING",
                (gid, uid, "member"),
            )
            self._conn.commit()
        return gid, title

    def list_user_groups(self, uid: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT g.id,g.title FROM "groups" g'
                " JOIN group_members m ON g.id=m.group_id"
                " WHERE m.tg_id=? ORDER BY g.created_at DESC",
                (uid,),
            ).fetchall()
        return [{"id": r["id"], "title": r["title"]} for r in rows]

    def is_group_member(self, group_id: int, uid: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM group_members WHERE group_id=? AND tg_id=?",
                (group_id, uid),
            ).fetchone()
        return row is not None

    def can_view_group(self, group_id: int, uid: int) -> bool:
        return self.is_group_member(group_id, uid)

    def can_add_expense(self, group_id: int, uid: int) -> bool:
        return self.is_group_member(group_id, uid)

    def can_view_settlements(self, group_id: int, uid: int) -> bool:
        return self.is_group_member(group_id, uid)

    def get_invite_code(self, group_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                'SELECT invite_code FROM "groups" WHERE id=?', (group_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"group {group_id} not found")
        return row["invite_code"]

    def get_group_title(self, group_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                'SELECT title FROM "groups" WHERE id=?', (group_id,)
            ).fetchone()
        return row["title"] if row else ""

    def is_group_owner(self, group_id: int, uid: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                'SELECT owner_tg_id FROM "groups" WHERE id=?', (group_id,)
            ).fetchone()
        return row is not None and row["owner_tg_id"] == uid

    def can_delete_group(self, group_id: int, uid: int) -> bool:
        return self.is_group_owner(group_id, uid)

    def delete_group(self, group_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM settlements WHERE group_id=?", (group_id,))
            self._conn.execute('DELETE FROM "groups" WHERE id=?', (group_id,))
            self._conn.commit()

    # -- members --

    def list_members(self, group_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.tg_id,u.name FROM group_members m"
                " JOIN users u ON u.tg_id=m.tg_id"
                " WHERE m.group_id=? ORDER BY u.name",
                (group_id,),
            ).fetchall()
        return [{"id": r["tg_id"], "name": r["name"]} for r in rows]

    def list_members_detailed(self, group_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.tg_id, u.name, m.role"
                " FROM group_members m"
                " JOIN users u ON u.tg_id = m.tg_id"
                " WHERE m.group_id = ?"
                " ORDER BY (m.role <> 'owner'), u.name",
                (group_id,),
            ).fetchall()
        return [{"id": r["tg_id"], "name": r["name"], "role": r["role"]} for r in rows]

    # -- expenses --

    def create_expense(
        self,
        group_id: int,
        created_by: int,
        payer: int,
        description: str,
        amount_cents: int,
        shares: dict,
    ) -> int:
        if not shares:
            raise ValueError("no participants")
        with self._lock:
            rows = self._conn.execute(
                "SELECT tg_id FROM group_members WHERE group_id=?",
                (group_id,),
            ).fetchall()
            members = {r["tg_id"] for r in rows}
            if created_by not in members:
                raise ValueError("creator is not a group member")
            if payer not in members:
                raise ValueError("payer is not a group member")
            if not set(shares).issubset(members):
                raise ValueError("expense participant is not a group member")

            cur = self._conn.execute(
                "INSERT INTO expenses("
                "group_id,created_by_tg_id,payer_tg_id,description,amount_cents,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (group_id, created_by, payer, description, amount_cents, now_unix()),
            )
            expense_id = cur.lastrowid
            self._conn.executemany(
                "INSERT INTO expense_participants(expense_id,participant_tg_id,share_cents)"
                " VALUES(?,?,?)",
                [(expense_id, pid, cents) for pid, cents in shares.items()],
            )
            self._conn.commit()
        return expense_id

    def delete_expense(self, expense_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE expenses SET deleted=1 WHERE id=?", (expense_id,)
            )
            self._conn.commit()

    def can_delete_expense(self, expense_id: int, group_id: int, uid: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT e.created_by_tg_id"
                " FROM expenses e"
                " WHERE e.id=? AND e.group_id=? AND e.deleted=0",
                (expense_id, group_id),
            ).fetchone()
        if row is None:
            return False
        if not self.is_group_member(group_id, uid):
            return False
        return row["created_by_tg_id"] == uid

    def get_expense_shares(self, expense_id: int) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT participant_tg_id, share_cents"
                " FROM expense_participants WHERE expense_id=?",
                (expense_id,),
            ).fetchall()
        return {r["participant_tg_id"]: r["share_cents"] for r in rows}

    def list_group_expenses(self, group_id: int, limit: int, offset: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,payer_tg_id,created_by_tg_id,amount_cents,description,created_at"
                " FROM expenses WHERE group_id=? AND deleted=0"
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                (group_id, limit, offset),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "payer": r["payer_tg_id"],
                "created_by": r["created_by_tg_id"],
                "amount_cents": r["amount_cents"],
                "desc": r["description"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def count_group_expenses(self, group_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM expenses WHERE group_id=? AND deleted=0",
                (group_id,),
            ).fetchone()
        return row[0] if row else 0

    # -- balances --

    def _net_positions(self, group_id: int) -> dict[int, int]:
        """Per-user balance in one group: positive = the group owes them."""
        with self._lock:
            share_rows = self._conn.execute(
                "SELECT e.payer_tg_id AS payer,"
                " p.participant_tg_id AS participant,"
                " p.share_cents AS share"
                " FROM expenses e"
                " JOIN expense_participants p ON p.expense_id = e.id"
                " WHERE e.group_id=? AND e.deleted=0",
                (group_id,),
            ).fetchall()
            settlement_rows = self._conn.execute(
                "SELECT from_tg_id, to_tg_id, amount_cents"
                " FROM settlements WHERE group_id=? AND confirmed_by_to=1",
                (group_id,),
            ).fetchall()

        net: dict[int, int] = {}
        for r in share_rows:
            payer, participant, share = r["payer"], r["participant"], r["share"]
            if participant == payer:
                continue
            net[participant] = net.get(participant, 0) - share
            net[payer] = net.get(payer, 0) + share

        for r in settlement_rows:
            frm, to, amt = r["from_tg_id"], r["to_tg_id"], r["amount_cents"]
            net[frm] = net.get(frm, 0) + amt
            net[to] = net.get(to, 0) - amt

        return {uid: v for uid, v in net.items() if v != 0}

    def compute_group_balances(self, group_id: int) -> dict:
        """Returns {(from_uid, to_uid): amount_cents} for net unpaid debts.

        Debts are simplified through chains, so A owing B while B owes C
        collapses into A paying C directly.
        """
        return settle_net(self._net_positions(group_id))

    def compute_user_debts(self, uid: int) -> dict[int, dict]:
        """What ``uid`` owes and is owed, per counterparty, across groups.

        Returns ``{other_uid: {"net": cents, "by_group": {gid: cents}}}``
        where a positive amount means the counterparty owes ``uid`` and a
        negative one means ``uid`` owes them. Debts in opposite directions
        cancel: owing someone 50 in one group while they owe 50 in another
        nets to zero, and there is genuinely nothing to transfer.

        Only pairs that share a group can appear here, because every entry
        comes from a single group's simplified balances.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT group_id FROM group_members WHERE tg_id=?", (uid,)
            ).fetchall()

        result: dict[int, dict] = {}
        for r in rows:
            gid = r["group_id"]
            for (frm, to), amount in self.compute_group_balances(gid).items():
                if frm == uid:
                    other, delta = to, -amount
                elif to == uid:
                    other, delta = frm, amount
                else:
                    continue
                entry = result.setdefault(other, {"net": 0, "by_group": {}})
                entry["net"] += delta
                entry["by_group"][gid] = entry["by_group"].get(gid, 0) + delta

        return result

    # -- settlements --

    def settle_with_user(self, uid: int, other: int, amount_cents: int) -> bool:
        """Close every debt between ``uid`` and ``other`` in one go.

        ``amount_cents`` is the net that ``uid`` hands over; it must still
        match the live net, so a stale button does nothing. Debts are
        closed in every shared group and in both directions, so the two
        halves of a cross-group offset disappear together instead of one
        being paid twice. A net of zero is allowed and settles nothing but
        the bookkeeping: the debts cancelled out, so no money moves.
        """
        with self._lock:
            entry = self.compute_user_debts(uid).get(other)
            if entry is None:
                return False
            if amount_cents < 0 or amount_cents != -entry["net"]:
                return False

            rows = []
            for gid, delta in entry["by_group"].items():
                if delta < 0:
                    rows.append((gid, uid, other, -delta))
                elif delta > 0:
                    rows.append((gid, other, uid, delta))
            if not rows:
                return False

            ts = now_unix()
            self._conn.executemany(
                "INSERT INTO settlements"
                "(group_id,from_tg_id,to_tg_id,amount_cents,confirmed_by_to,created_at)"
                " VALUES(?,?,?,?,1,?)",
                [(gid, frm, to, amt, ts) for gid, frm, to, amt in rows],
            )
            self._conn.commit()
            return True

    def count_group_settlements(self, group_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM settlements WHERE group_id=?",
                (group_id,),
            ).fetchone()
        return row[0] if row else 0

    def list_group_settlements(self, group_id: int, limit: int, offset: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, from_tg_id, to_tg_id, amount_cents, created_at"
                " FROM settlements WHERE group_id=?"
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                (group_id, limit, offset),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "from": r["from_tg_id"],
                "to": r["to_tg_id"],
                "amount_cents": r["amount_cents"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def can_delete_settlement(self, settlement_id: int, group_id: int, uid: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT from_tg_id FROM settlements WHERE id=? AND group_id=?",
                (settlement_id, group_id),
            ).fetchone()
        if row is None:
            return False
        return row["from_tg_id"] == uid or self.is_group_owner(group_id, uid)

    def delete_settlement(self, settlement_id: int, group_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM settlements WHERE id=? AND group_id=?",
                (settlement_id, group_id),
            )
            self._conn.commit()


# ---------- Keyboards ----------

def main_keyboard() -> ReplyKeyboardMarkup:
    # Ordered by how often each action is used: adding an expense is the
    # everyday action and gets a full-width row, creating a group happens
    # once per trip and sits last.
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🧾 Добавить трату")],
            [KeyboardButton("💰 Долги"), KeyboardButton("👥 Мои группы")],
            [KeyboardButton("🔗 Приглашение"), KeyboardButton("➕ Создать группу")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def inline_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отмена", callback_data="cancel_flow")]]
    )


# ---------- State helpers (stored in context.user_data) ----------

def _get_ae(ctx: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return ctx.user_data.get("add_expense")


def _set_ae(ctx: ContextTypes.DEFAULT_TYPE, state: dict) -> None:
    ctx.user_data["add_expense"] = state


def _del_ae(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("add_expense", None)


def _is_new_group(ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    return ctx.user_data.get("new_group_ask", False)


def _set_new_group(ctx: ContextTypes.DEFAULT_TYPE, v: bool) -> None:
    ctx.user_data["new_group_ask"] = v


# ---------- App ----------

# Labels from keyboards the bot used to send. Clients keep showing them until
# they receive a new keyboard, so they have to keep working.
_LEGACY_DEBT_BUTTONS = {"📊 Балансы", "🔄 Взаимозачёт"}

_TOP_BUTTONS = {
    "➕ Создать группу", "👥 Мои группы", "🔗 Приглашение",
    "🧾 Добавить трату", "💰 Долги",
} | _LEGACY_DEBT_BUTTONS


class App:
    def __init__(self, repo: Repo, bot_username: str = ""):
        self.repo = repo
        self.base = bot_username

    def _best_name(self, user) -> str:
        if user.username:
            return "@" + user.username
        parts = [user.first_name or "", user.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        return name or str(user.id)

    def _groups_page_keyboard(
        self, uid: int, page: int, mode: str
    ) -> tuple[InlineKeyboardMarkup, int]:
        gs = self.repo.list_user_groups(uid)
        gs.sort(key=lambda g: g["id"], reverse=True)
        total = len(gs)
        if total == 0:
            return InlineKeyboardMarkup([]), 0

        start = page * GROUPS_PER_PAGE
        if start >= total:
            page = 0
            start = 0
        end = min(start + GROUPS_PER_PAGE, total)

        rows = []
        for g in gs[start:end]:
            cb = {"mg": f"mgsel|{g['id']}", "inv": f"invsel|{g['id']}", "ae": f"aesel|{g['id']}"}[mode]
            rows.append([InlineKeyboardButton(f"#{g['id']}: {g['title']}", callback_data=cb)])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("« Назад", callback_data=f"{mode}|p:{page - 1}"))
        if end < total:
            nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"{mode}|p:{page + 1}"))
        if nav:
            rows.append(nav)

        return InlineKeyboardMarkup(rows), total

    async def _refresh_keyboard(self, update: Update, uid: int) -> None:
        """Send the current keyboard to a user still holding an older one.

        A reply keyboard only changes when the bot attaches a new one to a
        message, and the screens below mostly carry inline keyboards, which
        cannot double as one. So this sends a short message of its own, once
        per user per layout change.
        """
        if not self.repo.has_stale_keyboard(uid):
            return
        self.repo.mark_keyboard_current(uid)
        await update.effective_chat.send_message(
            "Кнопки внизу обновились: «📊 Балансы» и «🔄 Взаимозачёт»"
            " объединились в «💰 Долги».",
            reply_markup=main_keyboard(),
        )

    async def _edit_or_send(
        self,
        update: Update,
        text: str,
        markup: Optional[InlineKeyboardMarkup] = None,
    ) -> None:
        if update.callback_query and update.callback_query.message:
            await update.callback_query.message.edit_text(
                text, reply_markup=markup
            )
        else:
            await update.effective_chat.send_message(text, reply_markup=markup)

    # ---------- Command handlers ----------

    async def on_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        self.repo.upsert_user(user.id, self._best_name(user))
        self.repo.mark_keyboard_current(user.id)

        raw = (update.effective_message.text or "").strip()
        raw = (
            raw.replace(" ", " ")
            .replace(" ", " ")
            .replace(" ", " ")
            .replace("+", " ")
        )

        code = (ctx.args or [""])[0] if ctx.args else ""
        if not code:
            code = extract_start_code_from_text(raw)

        if code:
            try:
                gid, title = self.repo.join_by_code(code, user.id)
                await update.effective_chat.send_message(
                    f"Вы присоединились к группе #{gid}: {title}",
                    reply_markup=main_keyboard(),
                )
                return
            except Exception:
                pass

        await update.effective_chat.send_message(
            "Привет! Я помогу делить траты в поездках.\nИспользуйте кнопки ниже.",
            reply_markup=main_keyboard(),
        )

    async def on_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._refresh_keyboard(update, update.effective_user.id)
        if _get_ae(ctx) or _is_new_group(ctx):
            _del_ae(ctx)
            _set_new_group(ctx, False)
            await update.effective_chat.send_message(
                "Ок, отменил. Можно начать заново.", reply_markup=main_keyboard()
            )
        else:
            await update.effective_chat.send_message("Нечего отменять.")

    async def on_join(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id
        self.repo.upsert_user(uid, self._best_name(update.effective_user))
        await self._refresh_keyboard(update, uid)
        # PTB strips the /join@botname prefix automatically; ctx.args has the rest
        args = ctx.args or []
        code = ""
        if args:
            code = args[0].lstrip("_")  # handle /join _code or /join_code edge cases

        if not code:
            await update.effective_chat.send_message(
                "Использование: /join <код>\n"
                "Также работает команда: /join_<код> и ссылка /start <код>"
            )
            return

        try:
            gid, title = self.repo.join_by_code(code, uid)
            await update.effective_chat.send_message(
                f"Вы присоединились к группе #{gid}: {title}",
                reply_markup=main_keyboard(),
            )
        except Exception:
            # Bug fix: send user-friendly message instead of propagating exception
            await update.effective_chat.send_message(
                "Неверный или истёкший код приглашения."
            )

    # ---------- Text handler ----------

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        txt = (update.effective_message.text or "").strip()
        uid = update.effective_user.id
        self.repo.upsert_user(uid, self._best_name(update.effective_user))
        await self._refresh_keyboard(update, uid)

        # Block top-level buttons while wizard is active
        if (_is_new_group(ctx) or _get_ae(ctx)) and txt in _TOP_BUTTONS:
            await update.effective_chat.send_message(
                "Сейчас идёт мастер. Отправьте запрошенные данные"
                " или нажмите /cancel (или «❌ Отмена»)."
            )
            return

        in_wizard = _is_new_group(ctx) or _get_ae(ctx) is not None

        if not in_wizard:
            # /start@<code> or /start_<code>
            if txt.startswith("/start@") or txt.startswith("/start_"):
                code = txt.removeprefix("/start@").removeprefix("/start_").strip()
                if code:
                    try:
                        gid, title = self.repo.join_by_code(code, uid)
                        await update.effective_chat.send_message(
                            f"Вы присоединились к группе #{gid}: {title}",
                            reply_markup=main_keyboard(),
                        )
                        return
                    except Exception:
                        pass

            # /join_<code>
            if txt.startswith("/join_"):
                fields = txt.removeprefix("/join_").split()
                if fields:
                    try:
                        gid, title = self.repo.join_by_code(fields[0], uid)
                        await update.effective_chat.send_message(
                            f"Вы присоединились к группе #{gid}: {title}",
                            reply_markup=main_keyboard(),
                        )
                        return
                    except Exception:
                        pass

            # URL containing ?start=<code>
            code = extract_start_code_from_text(txt)
            if code:
                try:
                    gid, title = self.repo.join_by_code(code, uid)
                    await update.effective_chat.send_message(
                        f"Вы присоединились к группе #{gid}: {title}",
                        reply_markup=main_keyboard(),
                    )
                    return
                except Exception:
                    pass

            # Bare invite code
            code = extract_bare_code(txt)
            if code:
                try:
                    gid, title = self.repo.join_by_code(code, uid)
                    await update.effective_chat.send_message(
                        f"Вы присоединились к группе #{gid}: {title}",
                        reply_markup=main_keyboard(),
                    )
                    return
                except Exception:
                    pass

        # New group name input
        if _is_new_group(ctx):
            title = txt.strip()
            if not title or title.startswith("/"):
                await update.effective_chat.send_message(
                    "Название не может быть пустым."
                    " Введите название группы одним сообщением."
                )
                return
            _set_new_group(ctx, False)
            gid, code = self.repo.create_group(title, uid)

            enc = quote(f"/join {code}", safe="")
            share_href = f"https://t.me/share/url?url={enc}"
            html_join = f'<a href="{share_href}">/join {code}</a>'
            text = f"Группа #{gid} создана: {html.escape(title)}\nКоманда: {html_join}"
            await update.effective_chat.send_message(
                text,
                reply_markup=main_keyboard(),
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return

        # Add-expense wizard
        st = _get_ae(ctx)
        if st is not None:
            await self._flow_add_expense(update, ctx, st, txt)
            return

        # Top-level button routing
        if txt == "➕ Создать группу":
            _set_new_group(ctx, True)
            await update.effective_chat.send_message(
                "Введи название новой группы одним сообщением."
            )
        elif txt == "👥 Мои группы":
            markup, total = self._groups_page_keyboard(uid, 0, "mg")
            if total == 0:
                await update.effective_chat.send_message(
                    "У вас нет групп. Нажмите «Создать группу»."
                )
            else:
                await update.effective_chat.send_message(
                    "Выберите группу:", reply_markup=markup
                )
        elif txt == "🔗 Приглашение":
            markup, total = self._groups_page_keyboard(uid, 0, "inv")
            if total == 0:
                await update.effective_chat.send_message(
                    "У вас нет групп. Нажмите «Создать группу»."
                )
            else:
                await update.effective_chat.send_message(
                    "Выберите группу для приглашения:", reply_markup=markup
                )
        elif txt == "🧾 Добавить трату":
            markup, total = self._groups_page_keyboard(uid, 0, "ae")
            if total == 0:
                await update.effective_chat.send_message(
                    "У вас нет групп. Нажмите «Создать группу»."
                )
            else:
                await update.effective_chat.send_message(
                    "Выберите группу для добавления траты:", reply_markup=markup
                )
        elif txt == "💰 Долги" or txt in _LEGACY_DEBT_BUTTONS:
            await self._show_debts(update, ctx)

    # ---------- Add-expense wizard (text steps) ----------

    async def _flow_add_expense(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        st: dict,
        txt: str,
    ) -> None:
        step = st.get("step")

        if step == "await_amount_desc":
            amount_text, description = split_amount_and_description(txt)
            if not amount_text:
                await update.effective_chat.send_message(
                    "Нужно прислать сумму и описание. Пример: 1200 обед",
                    reply_markup=inline_cancel(),
                )
                return
            try:
                amt = cents_from_str(amount_text)
                if amt <= 0:
                    raise ValueError("non-positive")
            except Exception:
                await update.effective_chat.send_message(
                    "Сумма не распознана. Пример: 1200 обед",
                    reply_markup=inline_cancel(),
                )
                return

            st["amount_cents"] = amt
            st["description"] = description or "Без описания"
            st["step"] = "choose_payer"
            _set_ae(ctx, st)
            await self._ask_payer(update, ctx, st)

        elif step == "await_custom_share":
            custom_left: list = st.get("custom_left", [])
            if not custom_left:
                st["step"] = "confirm"
                _set_ae(ctx, st)
                await self._finalize_expense(update, ctx, st)
                return

            try:
                amt = cents_from_str(txt.strip())
                if amt < 0:
                    raise ValueError("negative")
            except Exception:
                await self._edit_or_send(
                    update,
                    "Сумма не распознана. Пришлите число, напр. 350.50",
                    inline_cancel(),
                )
                return

            next_uid = custom_left[0]
            name = self.repo.user_name(next_uid)
            custom_shares: dict = st.setdefault("custom_shares", {})
            remaining = st["amount_cents"] - sum(custom_shares.values())

            if amt > remaining:
                await self._edit_or_send(
                    update,
                    f"Слишком много. Остаток — {format_cents(remaining)}."
                    f" Введите сумму для {name} не больше остатка.",
                    inline_cancel(),
                )
                return
            if len(custom_left) == 1 and amt != remaining:
                amt = remaining

            custom_shares[next_uid] = amt
            st["custom_left"] = custom_left[1:]
            _set_ae(ctx, st)

            progress = self._shares_progress(st)

            if not st["custom_left"]:
                await self._edit_or_send(
                    update, progress + "\nВсе суммы заданы. Сохраняю…", inline_cancel()
                )
                await self._finalize_expense(update, ctx, st)
            else:
                nxt = st["custom_left"][0]
                rem = st["amount_cents"] - sum(custom_shares.values())
                await self._edit_or_send(
                    update,
                    f"{progress}\n\nВведите сумму для участника"
                    f" {self.repo.user_name(nxt)} (остаток — {format_cents(rem)}):",
                    inline_cancel(),
                )

    def _shares_progress(self, st: dict) -> str:
        custom_shares = st.get("custom_shares", {})
        if not custom_shares:
            return ""
        items = sorted(
            f"{self.repo.user_name(pid)}: {format_cents(v)}"
            for pid, v in custom_shares.items()
        )
        return "Назначено:\n• " + "\n• ".join(items)

    async def _ask_next_custom(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, st: dict
    ) -> None:
        custom_left: list = st.get("custom_left", [])
        if not custom_left:
            await self._finalize_expense(update, ctx, st)
            return
        uid = custom_left[0]
        name = self.repo.user_name(uid)
        remaining = st["amount_cents"] - sum(st.get("custom_shares", {}).values())
        progress = self._shares_progress(st)
        prefix = progress + "\n\n" if progress else ""
        await self._edit_or_send(
            update,
            f"{prefix}Введите сумму для участника {name}"
            f" (остаток — {format_cents(remaining)}, максимум — {format_cents(remaining)}):",
            inline_cancel(),
        )

    # ---------- Callback handler ----------

    async def on_callback(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self.repo.upsert_user(update.effective_user.id, self._best_name(update.effective_user))
        query = update.callback_query
        data = query.data
        uid = update.effective_user.id
        await self._refresh_keyboard(update, uid)

        if data == "cancel_flow":
            _del_ae(ctx)
            _set_new_group(ctx, False)
            await self._edit_or_send(update, "Отменено. Что дальше?")
            await query.answer("Отменено")
            return

        # Group list pagination
        for prefix, mode, label in [
            ("mg|p:", "mg", "Выберите группу:"),
            ("inv|p:", "inv", "Выберите группу для приглашения:"),
            ("ae|p:", "ae", "Выберите группу для добавления траты:"),
        ]:
            if data.startswith(prefix):
                page = int(data[len(prefix):])
                markup, _ = self._groups_page_keyboard(uid, page, mode)
                await self._edit_or_send(update, label, markup)
                await query.answer()
                return

        # Group selection
        if data.startswith("mgsel|"):
            gid = int(data[len("mgsel|"):])
            if not self.repo.is_group_member(gid, uid):
                await query.answer("Нет доступа", show_alert=True)
                return
            await self._send_group_details(update, ctx, gid)
            await query.answer()
            return

        if data.startswith("invsel|"):
            gid = int(data[len("invsel|"):])
            if not self.repo.is_group_member(gid, uid):
                await query.answer("Нет доступа", show_alert=True)
                return
            await self._send_invite_for_group(update, gid)
            await query.answer("Выберите чат для отправки")
            return

        if data.startswith("aesel|"):
            gid = int(data[len("aesel|"):])
            if not self.repo.is_group_member(gid, uid):
                await query.answer("Нет доступа", show_alert=True)
                return
            _set_ae(ctx, {
                "group_id": gid,
                "amount_cents": 0,
                "description": "",
                "payer": 0,
                "participants": {},
                "split_mode": "",
                "custom_left": [],
                "custom_shares": {},
                "step": "await_amount_desc",
            })
            await update.effective_chat.send_message(
                f"Группа #{gid} выбрана. Пришлите сумму и описание одним"
                f" сообщением, напр.:\n1500 такси из аэропорта"
            )
            await query.answer("Группа выбрана")
            return

        # Expense list pagination
        if data.startswith("explist|"):
            parts = data.split("|")
            if len(parts) >= 3 and parts[2].startswith("p:"):
                gid = int(parts[1])
                page = int(parts[2][2:])
                if self.repo.is_group_member(gid, uid):
                    await self._send_expenses_page(update, ctx, gid, page)
                else:
                    await query.answer("Нет доступа", show_alert=True)
                    return
            await query.answer()
            return

        # Delete expense
        # Bug fix: parts[0] is "expdel", parts[1] is the expense ID.
        # The Go original used parts[0] stripped of "expdel|", which left
        # the literal string "expdel" and parsed to ID=0, silently doing nothing.
        if data.startswith("expdel|"):
            parts = data.split("|")
            if (
                len(parts) >= 4
                and parts[2].startswith("gid:")
                and parts[3].startswith("p:")
            ):
                eid = int(parts[1])
                gid = int(parts[2][4:])
                page = int(parts[3][2:])
                try:
                    if not self.repo.can_delete_expense(eid, gid, uid):
                        await query.answer("Нет прав на удаление", show_alert=True)
                        return
                    self.repo.delete_expense(eid)
                    await query.answer("Удалено")
                    await self._send_expenses_page(update, ctx, gid, page)
                    return
                except Exception:
                    logger.exception("failed to delete expense")
            await query.answer("Ошибка удаления")
            return

        # Open the unified debts screen
        if data == "debts":
            await self._show_debts(update, ctx)
            await query.answer()
            return

        # Settle everything with one counterparty, across all shared groups
        if data.startswith("paynet|"):
            parts = data.split("|")
            if (
                len(parts) == 3
                and parts[1].startswith("to:")
                and parts[2].startswith("amt:")
            ):
                to = int(parts[1][3:])
                amt = int(parts[2][4:])
                try:
                    if not self.repo.settle_with_user(uid, to, amt):
                        await query.answer("Расчёт уже не актуален", show_alert=True)
                        try:
                            await self._show_debts(update, ctx)
                        except Exception:
                            logger.info("failed to refresh stale debts screen")
                        return
                    from_name = self.repo.user_name(uid)
                    to_name = self.repo.user_name(to)
                    if amt > 0:
                        for chat, text in (
                            (to, f"Вам оплатили {format_cents(amt)} от {from_name}."
                                 f" Все взаимные долги закрыты."),
                            (uid, f"Оплата {format_cents(amt)} пользователю {to_name}"
                                  f" зафиксирована. Все взаимные долги закрыты."),
                        ):
                            try:
                                await ctx.bot.send_message(chat, text)
                            except Exception:
                                logger.info("failed to notify %s about payment", chat)
                        await query.answer("Оплата подтверждена")
                    else:
                        try:
                            await ctx.bot.send_message(
                                to,
                                f"{from_name} закрыл(а) взаимные расчёты с вами:"
                                f" долги погасили друг друга, переводить нечего.",
                            )
                        except Exception:
                            logger.info("failed to notify %s about netting", to)
                        await query.answer("Взаимные долги закрыты")
                    await self._show_debts(update, ctx)
                    return
                except Exception:
                    logger.exception("failed to settle with user")
            await query.answer("Ошибка подтверждения")
            return

        # Delete group (owner only)
        if data.startswith("grpdel|"):
            gid = int(data[len("grpdel|gid:"):])
            if not self.repo.can_delete_group(gid, uid):
                await query.answer("Только владелец может удалить группу")
                return
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("Да, удалить", callback_data=f"grpdelyes|gid:{gid}")],
                [InlineKeyboardButton("Отмена", callback_data=f"mgsel|{gid}")],
            ])
            await self._edit_or_send(
                update,
                f"Точно удалить группу #{gid}? Это удалит все её данные.",
                markup,
            )
            await query.answer()
            return

        if data.startswith("grpdelyes|"):
            gid = int(data[len("grpdelyes|gid:"):])
            if not self.repo.can_delete_group(gid, uid):
                await query.answer("Только владелец может удалить группу", show_alert=True)
                return
            try:
                self.repo.delete_group(gid)
                await query.answer("Группа удалена")
                markup, _ = self._groups_page_keyboard(uid, 0, "mg")
                await self._edit_or_send(update, "Группа удалена. Ваши группы:", markup)
                return
            except Exception:
                logger.exception("failed to delete group")
            await query.answer("Ошибка удаления группы")
            return

        # Members screen
        if data.startswith("members|"):
            parts = data.split("|")
            if len(parts) >= 3 and parts[2].startswith("p:"):
                gid = int(parts[1])
                page = int(parts[2][2:])
                if self.repo.is_group_member(gid, uid):
                    await self._send_members_page(update, gid, page)
                else:
                    await query.answer("Нет доступа", show_alert=True)
                    return
            await query.answer()
            return

        # Settlement history
        if data.startswith("setlist|"):
            parts = data.split("|")
            if len(parts) >= 3 and parts[2].startswith("p:"):
                gid = int(parts[1])
                page = int(parts[2][2:])
                if self.repo.can_view_settlements(gid, uid):
                    await self._send_settlements_page(update, gid, page)
                else:
                    await query.answer("Нет доступа", show_alert=True)
                    return
            await query.answer()
            return

        if data.startswith("setdel|"):
            parts = data.split("|")
            if (
                len(parts) >= 4
                and parts[2].startswith("gid:")
                and parts[3].startswith("p:")
            ):
                settlement_id = int(parts[1])
                gid = int(parts[2][4:])
                page = int(parts[3][2:])
                try:
                    if not self.repo.can_delete_settlement(settlement_id, gid, uid):
                        await query.answer("Нет прав на отмену", show_alert=True)
                        return
                    self.repo.delete_settlement(settlement_id, gid)
                    await query.answer("Платеж отменен")
                    await self._send_settlements_page(update, gid, page)
                    return
                except Exception:
                    logger.exception("failed to delete settlement")
            await query.answer("Ошибка отмены платежа")
            return

        # Add-expense flow callbacks
        st = _get_ae(ctx)
        if st is None:
            await query.answer()
            return
        group_id = st.get("group_id")
        if not self.repo.is_group_member(group_id, uid):
            _del_ae(ctx)
            await query.answer("Нет доступа", show_alert=True)
            return

        if data.startswith("payer|"):
            payer = int(data[len("payer|"):])
            if not self.repo.is_group_member(group_id, payer):
                await query.answer("Нет доступа", show_alert=True)
                return
            st["payer"] = payer
            st["step"] = "choose_participants"
            _set_ae(ctx, st)
            await query.answer("Плательщик выбран")
            await self._ask_participants(update, ctx, st)
            return

        if data.startswith("toggle|"):
            pid = int(data[len("toggle|"):])
            if not self.repo.is_group_member(group_id, pid):
                await query.answer("Нет доступа", show_alert=True)
                return
            st["participants"][pid] = not st["participants"].get(pid, False)
            _set_ae(ctx, st)
            await query.answer()
            await self._ask_participants(update, ctx, st)
            return

        if data == "part_all":
            members = self.repo.list_members(group_id)
            st["participants"] = {m["id"]: True for m in members}
            _set_ae(ctx, st)
            await query.answer("Выбраны все")
            await self._ask_participants(update, ctx, st)
            return

        if data == "part_me_payer":
            participants = {}
            if self.repo.is_group_member(group_id, uid):
                participants[uid] = True
            if self.repo.is_group_member(group_id, st["payer"]):
                participants[st["payer"]] = True
            st["participants"] = participants
            _set_ae(ctx, st)
            await query.answer("Выбраны вы и плательщик")
            await self._ask_participants(update, ctx, st)
            return

        if data == "part_clear":
            st["participants"] = {}
            _set_ae(ctx, st)
            await query.answer("Выбор очищен")
            await self._ask_participants(update, ctx, st)
            return

        if data == "part_done":
            if not any(st["participants"].values()):
                await query.answer("Выберите хотя бы одного участника!", show_alert=True)
                return
            st["step"] = "choose_split"
            _set_ae(ctx, st)
            await query.answer()
            await self._ask_split_mode(update, ctx, st)
            return

        if data == "split|equal":
            st["split_mode"] = "equal"
            st["step"] = "confirm"
            _set_ae(ctx, st)
            await query.answer("Поровну")
            await self._finalize_expense(update, ctx, st)
            return

        if data == "split|custom":
            st["split_mode"] = "custom"
            st["custom_left"] = [pid for pid, on in st["participants"].items() if on]
            st["custom_shares"] = {}
            st["step"] = "await_custom_share"
            _set_ae(ctx, st)
            await query.answer("Свои доли")
            await self._ask_next_custom(update, ctx, st)
            return

        await query.answer()

    # ---------- Screens ----------

    async def _send_group_details(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, gid: int
    ) -> None:
        uid = update.effective_user.id
        try:
            code = self.repo.get_invite_code(gid)
        except Exception:
            logger.exception("failed to load group details for group %s", gid)
            return

        bal = self.repo.compute_group_balances(gid)

        cmd = f"/join {code}"
        enc = quote(cmd, safe="")
        share = f"https://t.me/share/url?url={enc}"
        cmd_html = f'<a href="{share}">{html.escape(cmd)}</a>'

        you_owe, owe_you = [], []
        for (frm, to), v in bal.items():
            if v <= 0:
                continue
            if frm == uid:
                you_owe.append(
                    f"вы → {html.escape(self.repo.user_name(to))}: {format_cents(v)}"
                )
            elif to == uid:
                owe_you.append(
                    f"{html.escape(self.repo.user_name(frm))} → вам: {format_cents(v)}"
                )
        you_owe.sort()
        owe_you.sort()

        lines = [f"Группа #{gid}\n", f"Команда: {cmd_html}\n"]
        if not you_owe and not owe_you:
            lines.append("В этой группе долгов нет 🎉")
        else:
            if you_owe:
                lines.append("Вы должны:\n• " + "\n• ".join(you_owe) + "\n")
            if owe_you:
                lines.append("Вам должны:\n• " + "\n• ".join(owe_you))
            lines.append(
                "\n\nЭто долги только по этой группе. Платить нужно по"
                " итогу всех групп — откройте «💰 Долги»."
            )
        text = "".join(lines)

        # Adding an expense is the reason people open a group, so it leads;
        # the rest is paired up to keep the keyboard short.
        rows = [
            [InlineKeyboardButton("🧾 Добавить трату", callback_data=f"aesel|{gid}")],
            [InlineKeyboardButton("💰 Долги", callback_data="debts")],
            [
                InlineKeyboardButton("📋 Список трат", callback_data=f"explist|{gid}|p:0"),
                InlineKeyboardButton("💸 Платежи", callback_data=f"setlist|{gid}|p:0"),
            ],
            [
                InlineKeyboardButton("👥 Участники", callback_data=f"members|{gid}|p:0"),
                InlineKeyboardButton("🔗 Поделиться /join…", url=share),
            ],
        ]
        if self.repo.is_group_owner(gid, uid):
            rows.append([InlineKeyboardButton(
                "🗑 Удалить группу", callback_data=f"grpdel|gid:{gid}"
            )])
        rows.append([InlineKeyboardButton(
            "« Мои группы", callback_data="mg|p:0"
        )])

        markup = InlineKeyboardMarkup(rows)
        opts = dict(
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=markup,
        )
        if update.callback_query and update.callback_query.message:
            await update.callback_query.message.edit_text(text, **opts)
        else:
            await update.effective_chat.send_message(text, **opts)

    async def _send_invite_for_group(self, update: Update, gid: int) -> None:
        try:
            code = self.repo.get_invite_code(gid)
        except Exception:
            logger.exception("failed to load invite for group %s", gid)
            return

        cmd = f"/join {code}"
        enc = quote(cmd, safe="")
        share = f"https://t.me/share/url?url={enc}"
        html_join = f'<a href="{share}">/join {code}</a>'
        text = f"Приглашение в группу #{gid}:\nКоманда: {html_join}"

        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Поделиться /join…", url=share)]]
        )
        opts = dict(
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=markup,
        )
        if update.callback_query and update.callback_query.message:
            await update.callback_query.message.edit_text(text, **opts)
        else:
            await update.effective_chat.send_message(text, **opts)

    async def _send_expenses_page(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        gid: int,
        page: int,
    ) -> None:
        total = self.repo.count_group_expenses(gid)
        offset = page * EXPENSES_PER_PAGE
        if total > 0 and offset >= total:
            page = 0
            offset = 0

        items = self.repo.list_group_expenses(gid, EXPENSES_PER_PAGE, offset)

        if total == 0:
            await self._edit_or_send(
                update,
                "В группе пока нет трат.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")
                ]]),
            )
            return

        lines = [f"Траты группы #{gid} (страница {page + 1}):\n"]
        for it in items:
            lines.append(
                f"• #{it['id']} {it['desc']} — {format_cents(it['amount_cents'])}"
                f" (плательщик: {self.repo.user_name(it['payer'])},"
                f" создал: {self.repo.user_name(it['created_by'])})\n"
            )

        rows = []
        for it in items:
            if self.repo.can_delete_expense(it["id"], gid, update.effective_user.id):
                rows.append([InlineKeyboardButton(
                    f"Удалить #{it['id']}",
                    callback_data=f"expdel|{it['id']}|gid:{gid}|p:{page}",
                )])
        nav = [InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")]
        if page > 0:
            nav.insert(0, InlineKeyboardButton(
                "« Назад", callback_data=f"explist|{gid}|p:{page - 1}"
            ))
        if offset + EXPENSES_PER_PAGE < total:
            nav.append(InlineKeyboardButton(
                "Вперёд »", callback_data=f"explist|{gid}|p:{page + 1}"
            ))
        rows.append(nav)

        await self._edit_or_send(update, "".join(lines), InlineKeyboardMarkup(rows))

    async def _send_members_page(
        self, update: Update, gid: int, page: int
    ) -> None:
        members = self.repo.list_members_detailed(gid)
        total = len(members)

        if total == 0:
            await self._edit_or_send(
                update,
                "В группе пока нет участников.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")
                ]]),
            )
            return

        start = page * MEMBERS_PER_PAGE
        if start >= total:
            page = 0
            start = 0
        end = min(start + MEMBERS_PER_PAGE, total)

        lines = [f"Участники группы #{gid} ({total} всего), страница {page + 1}:\n"]
        for m in members[start:end]:
            role_mark = " 👑 владелец" if m["role"] == "owner" else ""
            lines.append(f"• {m['name']}{role_mark}\n")

        nav = [InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")]
        if page > 0:
            nav.insert(0, InlineKeyboardButton(
                "« Назад", callback_data=f"members|{gid}|p:{page - 1}"
            ))
        if end < total:
            nav.append(InlineKeyboardButton(
                "Вперёд »", callback_data=f"members|{gid}|p:{page + 1}"
            ))

        await self._edit_or_send(update, "".join(lines), InlineKeyboardMarkup([nav]))

    async def _send_settlements_page(
        self, update: Update, gid: int, page: int
    ) -> None:
        uid = update.effective_user.id
        total = self.repo.count_group_settlements(gid)
        offset = page * SETTLEMENTS_PER_PAGE
        if total > 0 and offset >= total:
            page = 0
            offset = 0

        items = self.repo.list_group_settlements(gid, SETTLEMENTS_PER_PAGE, offset)
        if total == 0:
            await self._edit_or_send(
                update,
                "В группе пока нет платежей.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")
                ]]),
            )
            return

        lines = [f"Платежи группы #{gid} (страница {page + 1}):\n"]
        for item in items:
            lines.append(
                f"• #{item['id']} {self.repo.user_name(item['from'])} → "
                f"{self.repo.user_name(item['to'])}: {format_cents(item['amount_cents'])}"
                f" ({format_time(item['created_at'])})\n"
            )

        rows = []
        for item in items:
            if self.repo.can_delete_settlement(item["id"], gid, uid):
                rows.append([InlineKeyboardButton(
                    f"Отменить #{item['id']}",
                    callback_data=f"setdel|{item['id']}|gid:{gid}|p:{page}",
                )])

        nav = [InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")]
        if page > 0:
            nav.insert(0, InlineKeyboardButton(
                "« Назад", callback_data=f"setlist|{gid}|p:{page - 1}"
            ))
        if offset + SETTLEMENTS_PER_PAGE < total:
            nav.append(InlineKeyboardButton(
                "Вперёд »", callback_data=f"setlist|{gid}|p:{page + 1}"
            ))
        rows.append(nav)

        await self._edit_or_send(update, "".join(lines), InlineKeyboardMarkup(rows))

    # ---------- Add-expense sub-steps ----------

    async def _ask_payer(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, st: dict
    ) -> None:
        members = self.repo.list_members(st["group_id"])
        if not members:
            await self._edit_or_send(update, "В группе пока нет участников.")
            return
        btns = [
            [InlineKeyboardButton(
                f"Плательщик: {m['name']}", callback_data=f"payer|{m['id']}"
            )]
            for m in members
        ]
        btns.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_flow")])
        text = (
            f"Сумма: {format_cents(st['amount_cents'])}\n"
            f"Описание: {st['description']}\n"
            f"Выберите плательщика:"
        )
        await self._edit_or_send(update, text, InlineKeyboardMarkup(btns))

    async def _ask_participants(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, st: dict
    ) -> None:
        members = self.repo.list_members(st["group_id"])
        rows = []
        for m in members:
            on = st["participants"].get(m["id"], False)
            label = ("✅ " if on else "❌ ") + m["name"]
            rows.append([InlineKeyboardButton(label, callback_data=f"toggle|{m['id']}")])
        rows.append([
            InlineKeyboardButton("Все", callback_data="part_all"),
            InlineKeyboardButton("Я и плательщик", callback_data="part_me_payer"),
        ])
        rows.append([
            InlineKeyboardButton("Очистить", callback_data="part_clear"),
            InlineKeyboardButton("Готово →", callback_data="part_done"),
        ])
        rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_flow")])
        await self._edit_or_send(
            update,
            "Выберите участников (нажимайте, чтобы включить/исключить), затем «Готово».",
            InlineKeyboardMarkup(rows),
        )

    async def _ask_split_mode(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, st: dict
    ) -> None:
        btns = [
            [InlineKeyboardButton("Поровну", callback_data="split|equal")],
            [InlineKeyboardButton("Свои доли", callback_data="split|custom")],
        ]
        await self._edit_or_send(update, "Как разделить?", InlineKeyboardMarkup(btns))

    async def _finalize_expense(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, st: dict
    ) -> None:
        uid = update.effective_user.id
        group_id = st["group_id"]
        if not self.repo.is_group_member(group_id, uid):
            _del_ae(ctx)
            await self._edit_or_send(update, "Нет доступа к группе.")
            return

        if st["split_mode"] == "equal":
            participants = [pid for pid, on in st["participants"].items() if on]
            if not participants:
                _del_ae(ctx)
                await self._edit_or_send(
                    update, "Нет участников. Нажмите /cancel и начните заново."
                )
                return
            count = len(participants)
            base_share = st["amount_cents"] // count
            remainder = st["amount_cents"] - base_share * count
            shares = {}
            for pid in participants:
                sh = base_share
                if remainder > 0:
                    sh += 1
                    remainder -= 1
                shares[pid] = sh
        else:
            shares = st.get("custom_shares", {})
            if sum(shares.values()) != st["amount_cents"]:
                _del_ae(ctx)
                await self._edit_or_send(
                    update,
                    "Сумма долей не равна общей сумме. Нажмите /cancel и начните заново.",
                )
                return

        if not self.repo.is_group_member(group_id, st["payer"]) or any(
            not self.repo.is_group_member(group_id, pid) for pid in shares
        ):
            _del_ae(ctx)
            await self._edit_or_send(update, "Участник не найден в группе. Начните заново.")
            return

        try:
            expense_id = self.repo.create_expense(
                group_id,
                uid,
                st["payer"],
                st["description"],
                st["amount_cents"],
                shares,
            )
        except Exception as e:
            logger.exception("failed to create expense")
            await self._edit_or_send(update, f"Ошибка создания траты: {e}")
            return

        _del_ae(ctx)

        # Notify other participants of their share
        expense_shares = self.repo.get_expense_shares(expense_id)
        title = self.repo.get_group_title(group_id)
        for pid, share in expense_shares.items():
            if pid == uid:
                continue
            try:
                await ctx.bot.send_message(
                    pid,
                    f"В группе #{group_id} ({title}) добавлена трата:"
                    f" {st['description']} — {format_cents(st['amount_cents'])}.\n"
                    f"Ваша доля: {format_cents(share)}."
                    f" Плательщик: {self.repo.user_name(st['payer'])}.",
                )
            except Exception:
                logger.info("failed to notify expense participant %s", pid)

        await self._edit_or_send(
            update,
            f"Трата #{expense_id} добавлена."
            f" Сумма {format_cents(st['amount_cents'])},"
            f" плательщик {self.repo.user_name(st['payer'])}.",
        )

    # ---------- Balance screens ----------

    def _debt_block(self, uid: int, entry: dict) -> str:
        """Per-group detail under one counterparty, so the net is not a
        black box: it shows which group each part came from."""
        parts = []
        for gid, delta in sorted(entry["by_group"].items()):
            if delta == 0:
                continue
            title = self.repo.get_group_title(gid)
            side = "вам" if delta > 0 else "вы"
            parts.append(f"    #{gid} {title}: {side} {format_cents(abs(delta))}\n")
        return "".join(parts)

    async def _show_debts(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        uid = update.effective_user.id
        if not self.repo.list_user_groups(uid):
            await update.effective_chat.send_message(
                "У вас нет групп. Нажмите «Создать группу»."
            )
            return

        debts = self.repo.compute_user_debts(uid)
        entries = sorted(
            debts.items(), key=lambda kv: self.repo.user_name(kv[0]).lower()
        )

        you_owe, owe_you, even = [], [], []
        for other, entry in entries:
            detail = self._debt_block(uid, entry)
            if not detail:
                continue
            name = self.repo.user_name(other)
            net = entry["net"]
            if net < 0:
                you_owe.append((other, -net, name, detail))
            elif net > 0:
                owe_you.append((other, net, name, detail))
            else:
                even.append((other, name, detail))

        lines = ["💰 Долги по всем группам\n"]
        if not you_owe and not owe_you and not even:
            lines.append("\nДолгов нет 🎉")
        if you_owe:
            lines.append("\nВы должны:\n")
            for _, amount, name, detail in you_owe:
                lines.append(f"• {name} — {format_cents(amount)}\n{detail}")
        if owe_you:
            lines.append("\nВам должны:\n")
            for _, amount, name, detail in owe_you:
                lines.append(f"• {name} — {format_cents(amount)}\n{detail}")
        if even:
            lines.append("\nВы в расчёте (долги погасили друг друга):\n")
            for _, name, detail in even:
                lines.append(f"• {name}\n{detail}")

        rows = []
        for other, amount, name, _ in you_owe:
            rows.append([InlineKeyboardButton(
                f"Оплатил(а) {name}: {format_cents(amount)}",
                callback_data=f"paynet|to:{other}|amt:{amount}",
            )])
        for other, name, _ in even:
            rows.append([InlineKeyboardButton(
                f"✅ Закрыть расчёты с {name}",
                callback_data=f"paynet|to:{other}|amt:0",
            )])

        markup = InlineKeyboardMarkup(rows) if rows else None
        await self._edit_or_send(update, "".join(lines), markup)

# ---------- Entry point ----------

def main() -> None:
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        raise RuntimeError("BOT_TOKEN env required")

    db_path = os.environ.get("DB_PATH", "./data.db")
    repo = Repo(db_path)
    bot_app = App(repo)

    async def post_init(application: Application) -> None:
        me = await application.bot.get_me()
        bot_app.base = me.username
        logger.info("Starting bot @%s …", me.username)

    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", bot_app.on_start))
    application.add_handler(CommandHandler("join", bot_app.on_join))
    application.add_handler(CommandHandler("cancel", bot_app.on_cancel))
    application.add_handler(CallbackQueryHandler(bot_app.on_callback))
    # Use filters.TEXT (not ~filters.COMMAND) so that /join_<code> and
    # /start_<code> text patterns reach on_text; specific commands above
    # are consumed first within the same handler group.
    application.add_handler(MessageHandler(filters.TEXT, bot_app.on_text))

    try:
        application.run_polling(drop_pending_updates=True)
    finally:
        repo.close()


if __name__ == "__main__":
    main()
