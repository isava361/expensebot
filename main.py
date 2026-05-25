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
            self._conn.execute(
                "INSERT INTO users(tg_id,name) VALUES(?,?)"
                " ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name",
                (tg_id, name),
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

    def compute_group_balances(self, group_id: int) -> dict:
        """Returns {(from_uid, to_uid): amount_cents} for net unpaid debts."""
        with self._lock:
            expense_rows = self._conn.execute(
                "SELECT id, payer_tg_id, amount_cents"
                " FROM expenses WHERE group_id=? AND deleted=0",
                (group_id,),
            ).fetchall()

        bal: dict = {}

        for exp in expense_rows:
            with self._lock:
                share_rows = self._conn.execute(
                    "SELECT participant_tg_id, share_cents"
                    " FROM expense_participants WHERE expense_id=?",
                    (exp["id"],),
                ).fetchall()
            for sr in share_rows:
                pid, cents = sr["participant_tg_id"], sr["share_cents"]
                if pid == exp["payer_tg_id"]:
                    continue
                key = (pid, exp["payer_tg_id"])
                bal[key] = bal.get(key, 0) + cents

        with self._lock:
            settlement_rows = self._conn.execute(
                "SELECT from_tg_id, to_tg_id, amount_cents"
                " FROM settlements WHERE group_id=? AND confirmed_by_to=1",
                (group_id,),
            ).fetchall()

        for sr in settlement_rows:
            frm, to, amt = sr["from_tg_id"], sr["to_tg_id"], sr["amount_cents"]
            k = (frm, to)
            cur = bal.get(k, 0)
            if cur >= amt:
                bal[k] = cur - amt
                if bal[k] == 0:
                    del bal[k]
            else:
                over = amt - cur
                bal.pop(k, None)
                if over > 0:
                    inv = (to, frm)
                    bal[inv] = bal.get(inv, 0) + over

        # Mutual netting
        for k in list(bal.keys()):
            if k not in bal:
                continue
            inv = (k[1], k[0])
            if inv not in bal:
                continue
            v1, v2 = bal[k], bal[inv]
            if v1 >= v2:
                bal[k] = v1 - v2
                del bal[inv]
                if bal[k] == 0:
                    del bal[k]
            else:
                bal[inv] = v2 - v1
                del bal[k]

        return bal

    def compute_cross_group_net(self, uid: int) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT group_id FROM group_members WHERE tg_id=?", (uid,)
            ).fetchall()
        gids = [r["group_id"] for r in rows]

        all_bal: dict = {}
        for gid in gids:
            for k, v in self.compute_group_balances(gid).items():
                all_bal[k] = all_bal.get(k, 0) + v

        # Mutual netting (bug fix: also delete zero-value pairs, missing in Go)
        for k in list(all_bal.keys()):
            if k not in all_bal:
                continue
            inv = (k[1], k[0])
            if inv not in all_bal:
                continue
            v1, v2 = all_bal[k], all_bal[inv]
            if v1 >= v2:
                all_bal[k] = v1 - v2
                del all_bal[inv]
                if all_bal[k] == 0:
                    del all_bal[k]
            else:
                all_bal[inv] = v2 - v1
                del all_bal[k]

        return all_bal

    # -- settlements --

    def add_settlement_if_current(
        self, group_id: int, from_uid: int, to_uid: int, amount_cents: int
    ) -> bool:
        with self._lock:
            if amount_cents <= 0:
                return False
            if not self.is_group_member(group_id, from_uid):
                return False
            if not self.is_group_member(group_id, to_uid):
                return False

            current = self.compute_group_balances(group_id).get((from_uid, to_uid), 0)
            if current != amount_cents:
                return False

            self._conn.execute(
                "INSERT INTO settlements"
                "(group_id,from_tg_id,to_tg_id,amount_cents,confirmed_by_to,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (group_id, from_uid, to_uid, amount_cents, 1, now_unix()),
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
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Создать группу"), KeyboardButton("👥 Мои группы")],
            [KeyboardButton("🔗 Приглашение"), KeyboardButton("🧾 Добавить трату")],
            [KeyboardButton("📊 Балансы"), KeyboardButton("🔄 Взаимозачёт")],
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

_TOP_BUTTONS = {
    "➕ Создать группу", "👥 Мои группы", "🔗 Приглашение",
    "🧾 Добавить трату", "📊 Балансы", "🔄 Взаимозачёт",
}


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
        elif txt == "📊 Балансы":
            await self._show_balances_all(update, ctx)
        elif txt == "🔄 Взаимозачёт":
            await self._show_cross_net(update, ctx)

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

        # Confirm payment
        if data.startswith("pay|"):
            parts = data.split("|")
            if (
                len(parts) == 4
                and parts[1].startswith("gid:")
                and parts[2].startswith("to:")
                and parts[3].startswith("amt:")
            ):
                gid = int(parts[1][4:])
                to = int(parts[2][3:])
                amt = int(parts[3][4:])
                try:
                    if not self.repo.add_settlement_if_current(gid, uid, to, amt):
                        await query.answer("Оплата уже не актуальна", show_alert=True)
                        if self.repo.is_group_member(gid, uid):
                            await self._send_group_details(update, ctx, gid)
                        return
                    from_name = self.repo.user_name(uid)
                    title = self.repo.get_group_title(gid)
                    try:
                        await ctx.bot.send_message(
                            to,
                            f"Вам оплатили {format_cents(amt)} от {from_name}"
                            f" в группе #{gid} ({title}).",
                        )
                    except Exception:
                        logger.info("failed to notify payment receiver %s", to)
                    try:
                        await ctx.bot.send_message(
                            uid,
                            f"Оплата {format_cents(amt)} пользователю"
                            f" {self.repo.user_name(to)} в группе #{gid}"
                            f" ({title}) зафиксирована.",
                        )
                    except Exception:
                        logger.info("failed to notify payment sender %s", uid)
                    await query.answer("Оплата подтверждена")
                    await self._send_group_details(update, ctx, gid)
                    return
                except Exception:
                    logger.exception("failed to confirm payment")
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
        text = "".join(lines)

        rows = [
            [InlineKeyboardButton("Поделиться /join…", url=share)],
            [InlineKeyboardButton("👥 Участники", callback_data=f"members|{gid}|p:0")],
            [InlineKeyboardButton("Список трат", callback_data=f"explist|{gid}|p:0")],
            [InlineKeyboardButton("Платежи", callback_data=f"setlist|{gid}|p:0")],
        ]
        if self.repo.is_group_owner(gid, uid):
            rows.append([InlineKeyboardButton(
                "🗑 Удалить группу", callback_data=f"grpdel|gid:{gid}"
            )])
        rows.append([InlineKeyboardButton(
            "Назад к моим группам", callback_data="mg|p:0"
        )])

        confirm_rows = []
        for (frm, to), v in bal.items():
            if v > 0 and frm == uid:
                confirm_rows.append([InlineKeyboardButton(
                    f"Подтвердить оплату → {self.repo.user_name(to)} ({format_cents(v)})",
                    callback_data=f"pay|gid:{gid}|to:{to}|amt:{v}",
                )])
        if confirm_rows:
            rows = confirm_rows + rows

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

    async def _show_balances_all(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        uid = update.effective_user.id
        gs = self.repo.list_user_groups(uid)
        if not gs:
            await update.effective_chat.send_message(
                "У вас нет групп. Нажмите «Создать группу»."
            )
            return

        lines = ["Ваши долги по группам:\n"]
        for g in gs:
            bal = self.repo.compute_group_balances(g["id"])
            count = 0
            for (frm, to), v in bal.items():
                if v > 0 and frm == uid:
                    if count == 0:
                        lines.append(f"— #{g['id']} {g['title']}\n")
                    lines.append(f"   вы → {self.repo.user_name(to)}: {format_cents(v)}\n")
                    count += 1
            if count == 0:
                lines.append(f"— #{g['id']} {g['title']}: долгов нет\n")

        await update.effective_chat.send_message("".join(lines))

    async def _show_cross_net(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        uid = update.effective_user.id
        all_bal = self.repo.compute_cross_group_net(uid)

        if not all_bal:
            await update.effective_chat.send_message(
                "По всем группам взаимозачёт = 0. Никто никому не должен ✨"
            )
            return

        you_owe, owe_you = [], []
        for (frm, to), v in all_bal.items():
            if v <= 0:
                continue
            if frm == uid:
                you_owe.append(f"вы → {self.repo.user_name(to)}: {format_cents(v)}")
            elif to == uid:
                owe_you.append(f"{self.repo.user_name(frm)} → вам: {format_cents(v)}")
        you_owe.sort()
        owe_you.sort()

        lines = ["Взаимозачёт по всем группам:\n"]
        if not you_owe and not owe_you:
            lines.append("Никто никому не должен 🎉")
        else:
            if you_owe:
                lines.append("Вы должны:\n• " + "\n• ".join(you_owe) + "\n")
            if owe_you:
                lines.append("Вам должны:\n• " + "\n• ".join(owe_you))

        await update.effective_chat.send_message("".join(lines))


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
