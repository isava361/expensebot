"""SQLite ledger, permissions, migrations and expense history."""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional
from storage import atomic, copy_database
from core import (
    MAX_AMOUNT_CENTS,
    KEYBOARD_VERSION,
    MIGRATIONS_DIR,
    now_unix,
    normalize_currency,
    default_currency,
    default_tz_offset_min,
    rand_code,
    settle_net,
)


class Repo:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        try:
            if (
                self.db_path != ":memory:"
                and self._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='expenses'"
                ).fetchone()
            ):
                self.backup()
            self._apply_migrations()
        except BaseException:
            self._conn.close()
            raise

    def backup(self, destination=None):
        if destination is None:
            folder = Path(
                os.environ.get(
                    "BACKUP_DIR", str(Path(self.db_path).resolve().parent / "backups")
                )
            )
            destination = folder / f"expensebot-{time.time_ns()}.db"
        with self._lock:
            return copy_database(self._conn, destination)

    def _apply_migrations(self) -> None:
        if not MIGRATIONS_DIR.exists():
            raise RuntimeError(f"migrations directory not found: {MIGRATIONS_DIR}")

        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
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
                # executescript commits before executing, which would separate
                # schema changes from their migration marker after a crash.
                statement = ""
                for line in sql.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        self._conn.execute(statement)
                        statement = ""
                if statement.strip() and not all(
                    not line.strip() or line.lstrip().startswith("--")
                    for line in statement.splitlines()
                ):
                    raise ValueError(f"Incomplete migration: {version}")
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                    (version, now_unix()),
                )

    def _column_exists(self, table: str, column: str) -> bool:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row["name"] == column for row in rows)

    def _migrate_expense_created_by(self) -> None:
        if not self._column_exists("expenses", "created_by_tg_id"):
            self._conn.execute(
                "ALTER TABLE expenses ADD COLUMN created_by_tg_id INTEGER"
            )
        self._conn.execute(
            "UPDATE expenses SET created_by_tg_id=payer_tg_id "
            "WHERE created_by_tg_id IS NULL"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- users --

    @atomic
    def upsert_user(self, tg_id: int, name: str) -> None:
        with self._lock:
            # New users are stamped with the current keyboard: they are shown
            # it by /start, so they must not be told it changed.
            self._conn.execute(
                "INSERT INTO users(tg_id,name,keyboard_version,tz_offset_min)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name",
                (tg_id, name, KEYBOARD_VERSION, default_tz_offset_min()),
            )

    def has_stale_keyboard(self, uid: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT keyboard_version FROM users WHERE tg_id=?", (uid,)
            ).fetchone()
        return row is not None and row["keyboard_version"] < KEYBOARD_VERSION

    @atomic
    def mark_keyboard_current(self, uid: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET keyboard_version=? WHERE tg_id=?",
                (KEYBOARD_VERSION, uid),
            )

    def names_for(self, ids) -> dict[int, str]:
        """Names for a whole screen in one query instead of one per row."""
        wanted = list({int(i) for i in ids})
        if not wanted:
            return {}
        placeholders = ",".join("?" * len(wanted))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT tg_id, name FROM users WHERE tg_id IN ({placeholders})",
                wanted,
            ).fetchall()
        found = {r["tg_id"]: r["name"] for r in rows}
        return {uid: found.get(uid, str(uid)) for uid in wanted}

    def user_tz(self, uid: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT tz_offset_min FROM users WHERE tg_id=?", (uid,)
            ).fetchone()
        return row["tz_offset_min"] if row else default_tz_offset_min()

    @atomic
    def set_user_tz(self, uid: int, offset_min: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET tz_offset_min=? WHERE tg_id=?", (offset_min, uid)
            )

    def user_name(self, uid: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM users WHERE tg_id=?", (uid,)
            ).fetchone()
        return row["name"] if row else str(uid)

    # -- groups --

    @atomic
    def create_group(
        self, title: str, owner: int, currency: str = ""
    ) -> tuple[int, str]:
        code = rand_code()
        currency = normalize_currency(currency) or default_currency()
        with self._lock:
            cur = self._conn.execute(
                'INSERT INTO "groups"(title,owner_tg_id,invite_code,created_at,currency)'
                " VALUES(?,?,?,?,?)",
                (title, owner, code, now_unix(), currency),
            )
            gid = cur.lastrowid
            self._conn.execute(
                "INSERT INTO group_members(group_id,tg_id,role,joined_at)"
                " VALUES(?,?,?,?)",
                (gid, owner, "owner", now_unix()),
            )
        return gid, code

    @atomic
    def join_by_code(self, code: str, uid: int) -> tuple[int, str]:
        with self._lock:
            row = self._conn.execute(
                'SELECT id,title FROM "groups" WHERE invite_code=?', (code,)
            ).fetchone()
            if row is None:
                raise ValueError("invalid invite code")
            gid, title = row["id"], row["title"]
            self._conn.execute(
                "INSERT INTO group_members(group_id,tg_id,role,joined_at)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(group_id,tg_id) DO NOTHING",
                (gid, uid, "member", now_unix()),
            )
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

    def group_currency(self, group_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                'SELECT currency FROM "groups" WHERE id=?', (group_id,)
            ).fetchone()
        return row["currency"] if row else default_currency()

    def group_titles(self, group_ids) -> dict[int, str]:
        """Titles for a screen that spans groups, in one query."""
        ids = list({int(i) for i in group_ids})
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f'SELECT id, title FROM "groups" WHERE id IN ({placeholders})', ids
            ).fetchall()
        found = {r["id"]: r["title"] for r in rows}
        return {gid: found.get(gid, "") for gid in ids}

    def group_currencies(self, group_ids) -> dict[int, str]:
        """One lookup for a screen that spans groups."""
        ids = list(group_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f'SELECT id, currency FROM "groups" WHERE id IN ({placeholders})',
                ids,
            ).fetchall()
        return {r["id"]: r["currency"] for r in rows}

    def can_change_currency(self, group_id: int) -> bool:
        """Only while the group is empty.

        Every stored amount is already in the base currency; swapping it
        later would silently reinterpret every past trace.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT"
                " (SELECT COUNT(*) FROM expenses WHERE group_id=? AND deleted=0)"
                " + (SELECT COUNT(*) FROM settlements WHERE group_id=?)",
                (group_id, group_id),
            ).fetchone()
        return bool(row) and row[0] == 0

    @atomic
    def set_group_currency(self, group_id: int, currency: str) -> bool:
        code = normalize_currency(currency)
        if not code or not self.can_change_currency(group_id):
            return False
        with self._lock:
            self._conn.execute(
                'UPDATE "groups" SET currency=? WHERE id=?', (code, group_id)
            )
        return True

    def is_group_owner(self, group_id: int, uid: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                'SELECT owner_tg_id FROM "groups" WHERE id=?', (group_id,)
            ).fetchone()
        return row is not None and row["owner_tg_id"] == uid

    def can_delete_group(self, group_id: int, uid: int) -> bool:
        return self.is_group_owner(group_id, uid)

    @atomic
    def rename_group(self, group_id: int, title: str) -> bool:
        clean = title.strip()
        if not clean:
            return False
        with self._lock:
            self._conn.execute(
                'UPDATE "groups" SET title=? WHERE id=?', (clean[:100], group_id)
            )
        return True

    def member_balance(self, group_id: int, uid: int) -> int:
        """What this person is up or down in one group, in its currency."""
        return self._net_positions(group_id).get(uid, 0)

    def member_balances(self, group_id: int) -> dict[int, int]:
        """Everyone's balance at once: the members screen needs them all,
        and each call walks every expense in the group."""
        return self._net_positions(group_id)

    @atomic
    def remove_member(self, group_id: int, uid: int) -> str:
        """Take someone out of a group. Returns "" or why it cannot happen.

        Somebody who is still owed money — or still owes it — cannot be
        dropped: their share of every past expense stays in the ledger, but
        the debt screen only walks the groups a person belongs to, so the
        debt would keep existing for the other side alone.
        """
        if not self.is_group_member(group_id, uid):
            return "not_member"
        if self.member_balance(group_id, uid) != 0:
            return "has_debt"
        if self.is_group_owner(group_id, uid):
            return "owner"
        with self._lock:
            self._conn.execute(
                "DELETE FROM group_members WHERE group_id=? AND tg_id=?",
                (group_id, uid),
            )
        return ""

    @atomic
    def leave_group(self, group_id: int, uid: int) -> str:
        """Leave a group, handing ownership over if the owner walks out.

        Returns "" on success, "last" if the group was removed with its
        last member, or the reason it could not happen.
        """
        if not self.is_group_member(group_id, uid):
            return "not_member"
        if self.member_balance(group_id, uid) != 0:
            return "has_debt"

        if not self.is_group_owner(group_id, uid):
            with self._lock:
                self._conn.execute(
                    "DELETE FROM group_members WHERE group_id=? AND tg_id=?",
                    (group_id, uid),
                )
            return ""

        with self._lock:
            heir = self._conn.execute(
                "SELECT tg_id FROM group_members"
                " WHERE group_id=? AND tg_id<>?"
                " ORDER BY joined_at, tg_id LIMIT 1",
                (group_id, uid),
            ).fetchone()
        if heir is None:
            # Nobody is left to own it, so the group goes with them.
            self.delete_group(group_id)
            return "last"

        with self._lock:
            self._conn.execute(
                'UPDATE "groups" SET owner_tg_id=? WHERE id=?',
                (heir["tg_id"], group_id),
            )
            self._conn.execute(
                "UPDATE group_members SET role='owner' WHERE group_id=? AND tg_id=?",
                (group_id, heir["tg_id"]),
            )
            self._conn.execute(
                "DELETE FROM group_members WHERE group_id=? AND tg_id=?",
                (group_id, uid),
            )
        return ""

    def next_owner(self, group_id: int, leaving: int) -> int:
        """Who would inherit the group — for warning the leaver in advance."""
        with self._lock:
            row = self._conn.execute(
                "SELECT tg_id FROM group_members"
                " WHERE group_id=? AND tg_id<>?"
                " ORDER BY joined_at, tg_id LIMIT 1",
                (group_id, leaving),
            ).fetchone()
        return row["tg_id"] if row else 0

    @atomic
    def delete_group(self, group_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM settlements WHERE group_id=?", (group_id,))
            self._conn.execute('DELETE FROM "groups" WHERE id=?', (group_id,))

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

    @atomic
    def create_expense(
        self,
        group_id: int,
        created_by: int,
        payer: int,
        description: str,
        amount_cents: int,
        shares: dict,
        orig_currency: str = "",
        orig_amount_cents: int = 0,
        operation_id: str = "",
    ) -> int:
        self._validate_amounts(amount_cents, shares, orig_currency, orig_amount_cents)
        with self._lock:
            if operation_id:
                existing = self._conn.execute(
                    "SELECT id, group_id, created_by_tg_id FROM expenses WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if existing:
                    if (
                        existing["group_id"] != group_id
                        or existing["created_by_tg_id"] != created_by
                    ):
                        raise ValueError("Invalid operation")
                    return existing["id"]
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
                "group_id,created_by_tg_id,payer_tg_id,description,amount_cents,"
                "created_at,orig_currency,orig_amount_cents,operation_id"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    group_id,
                    created_by,
                    payer,
                    description,
                    amount_cents,
                    now_unix(),
                    orig_currency or None,
                    orig_amount_cents or None,
                    operation_id or None,
                ),
            )
            expense_id = cur.lastrowid
            self._conn.executemany(
                "INSERT INTO expense_participants(expense_id,participant_tg_id,share_cents)"
                " VALUES(?,?,?)",
                [(expense_id, pid, cents) for pid, cents in shares.items()],
            )
            self._record_history(expense_id, group_id, created_by, "create", None)
        return expense_id

    def get_expense(
        self, expense_id: int, group_id: int, include_deleted=False
    ) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, group_id, payer_tg_id, created_by_tg_id, description,"
                " amount_cents, created_at, updated_at, orig_currency,"
                " orig_amount_cents, receipt_file_id, revision, deleted"
                " FROM expenses WHERE id=? AND group_id=? AND (deleted=0 OR ?)",
                (expense_id, group_id, include_deleted),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "group_id": row["group_id"],
            "payer": row["payer_tg_id"],
            "created_by": row["created_by_tg_id"],
            "desc": row["description"],
            "amount_cents": row["amount_cents"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"] or 0,
            "orig_currency": row["orig_currency"] or "",
            "orig_amount_cents": row["orig_amount_cents"] or 0,
            "receipt": row["receipt_file_id"] or "",
            "revision": row["revision"],
            "deleted": bool(row["deleted"]),
            "shares": self.get_expense_shares(expense_id),
        }

    @atomic
    def update_expense(
        self,
        expense_id: int,
        group_id: int,
        payer: int,
        description: str,
        amount_cents: int,
        shares: dict,
        orig_currency: str = "",
        orig_amount_cents: int = 0,
        actor: Optional[int] = None,
        expected_revision: Optional[int] = None,
    ) -> None:
        """Rewrite an expense in place, keeping its number and its receipt."""
        self._validate_amounts(amount_cents, shares, orig_currency, orig_amount_cents)
        with self._lock:
            before = self._editable_expense(expense_id, group_id, actor)
            actor = before["created_by"] if actor is None else actor
            if (
                expected_revision is not None
                and before["revision"] != expected_revision
            ):
                raise ValueError("Трата уже изменена. Откройте её заново.")
            rows = self._conn.execute(
                "SELECT tg_id FROM group_members WHERE group_id=?", (group_id,)
            ).fetchall()
            members = {r["tg_id"] for r in rows}
            if payer not in members:
                raise ValueError("payer is not a group member")
            if not set(shares).issubset(members):
                raise ValueError("expense participant is not a group member")

            self._conn.execute(
                "UPDATE expenses SET payer_tg_id=?, description=?, amount_cents=?,"
                " orig_currency=?, orig_amount_cents=?, updated_at=?, revision=revision+1"
                " WHERE id=? AND group_id=? AND deleted=0",
                (
                    payer,
                    description,
                    amount_cents,
                    orig_currency or None,
                    orig_amount_cents or None,
                    now_unix(),
                    expense_id,
                    group_id,
                ),
            )
            self._conn.execute(
                "DELETE FROM expense_participants WHERE expense_id=?", (expense_id,)
            )
            self._conn.executemany(
                "INSERT INTO expense_participants(expense_id,participant_tg_id,share_cents)"
                " VALUES(?,?,?)",
                [(expense_id, pid, cents) for pid, cents in shares.items()],
            )
            self._record_history(expense_id, group_id, actor, "edit", before)

    @atomic
    def set_receipt(
        self, expense_id: int, group_id: int, file_id: str, actor=None
    ) -> None:
        with self._lock:
            before = self._editable_expense(expense_id, group_id, actor)
            self._conn.execute(
                "UPDATE expenses SET receipt_file_id=?, updated_at=?, revision=revision+1 WHERE id=? AND group_id=?",
                (file_id or None, now_unix(), expense_id, group_id),
            )
            self._record_history(
                expense_id,
                group_id,
                actor if actor is not None else before["created_by"],
                "receipt",
                before,
            )

    @atomic
    def delete_expense(self, expense_id: int, actor=None) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT group_id FROM expenses WHERE id=?", (expense_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Трата не найдена.")
            gid = row["group_id"]
            before = self._editable_expense(expense_id, gid, actor)
            self._conn.execute(
                "UPDATE expenses SET deleted=1, updated_at=?, revision=revision+1 WHERE id=?",
                (now_unix(), expense_id),
            )
            self._record_history(
                expense_id,
                gid,
                actor if actor is not None else before["created_by"],
                "delete",
                before,
            )

    @staticmethod
    def _validate_amounts(amount, shares, orig_currency="", orig_amount=0):
        if type(amount) is not int or not 0 < amount <= MAX_AMOUNT_CENTS:
            raise ValueError("Некорректная сумма траты.")
        if not shares or any(type(v) is not int or v < 0 for v in shares.values()):
            raise ValueError("Некорректные доли.")
        if sum(shares.values()) != amount:
            raise ValueError("Сумма долей должна совпадать с суммой траты.")
        if orig_currency:
            if (
                not normalize_currency(orig_currency)
                or type(orig_amount) is not int
                or not 0 < orig_amount <= MAX_AMOUNT_CENTS
            ):
                raise ValueError("Некорректная сумма в исходной валюте.")
        elif orig_amount:
            raise ValueError("Не указана исходная валюта.")

    def _editable_expense(self, eid, gid, actor, include_deleted=False):
        item = self.get_expense(eid, gid, include_deleted)
        if item is None:
            raise ValueError("Трата не найдена.")
        actor = item["created_by"] if actor is None else actor
        if actor != item["created_by"] or not self.is_group_member(gid, actor):
            raise ValueError("Менять трату может только её автор — участник группы.")
        return item

    def _check_departed_members(self, before, after):
        """An old expense must not recreate debts for someone who left."""
        members = {m["id"] for m in self.list_members(before["group_id"])}
        affected = {
            before["payer"],
            *before["shares"],
            after["payer"],
            *after["shares"],
        }

        def contribution(item, uid):
            if item["deleted"]:
                return 0
            return (item["amount_cents"] if item["payer"] == uid else 0) - item[
                "shares"
            ].get(uid, 0)

        if any(
            contribution(before, uid) != contribution(after, uid)
            for uid in affected - members
        ):
            raise ValueError(
                "Изменение затронет баланс вышедшего участника. Сначала верните его в группу."
            )

    def _record_history(self, eid, gid, actor, action, before):
        after = self.get_expense(eid, gid, include_deleted=True)
        if before:
            self._check_departed_members(before, after)
        self._conn.execute(
            "INSERT INTO expense_history(expense_id,actor_tg_id,action,created_at,before_json,after_json) VALUES(?,?,?,?,?,?)",
            (
                eid,
                actor,
                action,
                now_unix(),
                json.dumps(before, ensure_ascii=False) if before else None,
                json.dumps(after, ensure_ascii=False),
            ),
        )

    def expense_history(self, eid, gid, uid, limit=10, offset=0):
        if not self.is_group_member(gid, uid):
            raise ValueError("Нет доступа к группе.")
        with self._lock:
            rows = self._conn.execute(
                "SELECT h.* FROM expense_history h JOIN expenses e ON e.id=h.expense_id "
                "WHERE e.id=? AND e.group_id=? ORDER BY h.id DESC LIMIT ? OFFSET ?",
                (eid, gid, limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    @atomic
    def restore_expense(self, eid, gid, actor):
        before = self._editable_expense(eid, gid, actor, include_deleted=True)
        if not before["deleted"]:
            return False
        members = {m["id"] for m in self.list_members(gid)}
        if not ({before["payer"]} | set(before["shares"])).issubset(members):
            raise ValueError("Для восстановления верните участников траты в группу.")
        self._conn.execute(
            "UPDATE expenses SET deleted=0, updated_at=?, revision=revision+1 WHERE id=? AND group_id=?",
            (now_unix(), eid, gid),
        )
        self._record_history(eid, gid, actor, "restore", before)
        return True

    def can_edit_expense(self, expense_id: int, group_id: int, uid: int) -> bool:
        """Same rule as deleting: the person who entered it owns it."""
        return self.can_delete_expense(expense_id, group_id, uid)

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

    def list_group_expenses(
        self, group_id: int, limit: int, offset: int, deleted=False
    ) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,payer_tg_id,created_by_tg_id,amount_cents,description,"
                "created_at,updated_at,orig_currency,orig_amount_cents,"
                "receipt_file_id"
                " FROM expenses WHERE group_id=? AND deleted=?"
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                (group_id, deleted, limit, offset),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "payer": r["payer_tg_id"],
                "created_by": r["created_by_tg_id"],
                "amount_cents": r["amount_cents"],
                "desc": r["description"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"] or 0,
                "orig_currency": r["orig_currency"] or "",
                "orig_amount_cents": r["orig_amount_cents"] or 0,
                "receipt": r["receipt_file_id"] or "",
            }
            for r in rows
        ]

    def count_group_expenses(self, group_id: int, deleted=False) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM expenses WHERE group_id=? AND deleted=?",
                (group_id, deleted),
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

    def compute_user_debts(self, uid: int) -> dict[int, dict[str, dict]]:
        """What ``uid`` owes and is owed, per counterparty, across groups.

        Returns ``{other_uid: {currency: {"net": cents, "by_group": {gid: cents}}}}``
        where a positive amount means the counterparty owes ``uid`` and a
        negative one means ``uid`` owes them. Debts in opposite directions
        cancel: owing someone 50 in one group while they owe 50 in another
        nets to zero, and there is genuinely nothing to transfer.

        Currencies are kept apart, because a debt in lira is not repaid by
        a credit in roubles — netting them would invent an exchange rate
        nobody agreed on.

        Only pairs that share a group can appear here, because every entry
        comes from a single group's simplified balances.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.group_id AS gid, g.currency AS currency"
                " FROM group_members m"
                ' JOIN "groups" g ON g.id = m.group_id'
                " WHERE m.tg_id=?",
                (uid,),
            ).fetchall()

        result: dict[int, dict[str, dict]] = {}
        for r in rows:
            gid, currency = r["gid"], r["currency"]
            for (frm, to), amount in self.compute_group_balances(gid).items():
                if frm == uid:
                    other, delta = to, -amount
                elif to == uid:
                    other, delta = frm, amount
                else:
                    continue
                entry = result.setdefault(other, {}).setdefault(
                    currency, {"net": 0, "by_group": {}}
                )
                entry["net"] += delta
                entry["by_group"][gid] = entry["by_group"].get(gid, 0) + delta

        return result

    # -- settlements --

    @atomic
    def request_settlement(
        self, uid: int, other: int, currency: str, amount_cents: int
    ) -> str:
        """Record a payment ``uid`` says they made, awaiting confirmation.

        Returns the batch id, or "" if the request does not match the live
        debt. Nothing here changes any balance: the rows land unconfirmed,
        and only the person who was supposed to receive the money can turn
        them into a settled debt.

        Paying the whole net closes the debt in every shared group and in
        both directions, so the two halves of a cross-group offset
        disappear together instead of one being paid twice. A smaller
        amount is spread over the groups where ``uid`` owes, largest debt
        first, and leaves the rest standing — the export shows exactly
        which group each part went to.
        """
        currency = normalize_currency(currency)
        if amount_cents < 0 or not currency:
            return ""

        with self._lock:
            if self.pending_batch_between(uid, other, currency):
                return ""  # one open claim at a time, or people double-pay
            entry = self.compute_user_debts(uid).get(other, {}).get(currency)
            if entry is None:
                return ""
            owed = -entry["net"]
            if amount_cents > owed:
                return ""

            rows = []
            if amount_cents == owed:
                for gid, delta in entry["by_group"].items():
                    if delta < 0:
                        rows.append((gid, uid, other, -delta))
                    elif delta > 0:
                        rows.append((gid, other, uid, delta))
            else:
                left = amount_cents
                debts = sorted(
                    (
                        (gid, -delta)
                        for gid, delta in entry["by_group"].items()
                        if delta < 0
                    ),
                    key=lambda pair: (-pair[1], pair[0]),
                )
                for gid, debt in debts:
                    if left <= 0:
                        break
                    part = min(left, debt)
                    rows.append((gid, uid, other, part))
                    left -= part
                if left > 0:
                    return ""
            if not rows:
                return ""

            batch = rand_code()
            ts = now_unix()
            self._conn.executemany(
                "INSERT INTO settlements"
                "(group_id,from_tg_id,to_tg_id,amount_cents,confirmed_by_to,"
                "created_at,batch,requested_by,requested_to)"
                " VALUES(?,?,?,?,0,?,?,?,?)",
                [
                    (gid, frm, to, amt, ts, batch, uid, other)
                    for gid, frm, to, amt in rows
                ],
            )
            return batch

    def pending_batch_between(self, uid: int, other: int, currency: str) -> str:
        """The open claim between two people in one currency, if any."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.batch, g.currency"
                " FROM settlements s"
                ' JOIN "groups" g ON g.id = s.group_id'
                " WHERE s.confirmed_by_to=0 AND s.batch IS NOT NULL"
                " AND ((s.from_tg_id=? AND s.to_tg_id=?)"
                "      OR (s.from_tg_id=? AND s.to_tg_id=?))",
                (uid, other, other, uid),
            ).fetchall()
        for r in rows:
            if r["currency"] == currency:
                return r["batch"]
        return ""

    def batch_info(self, batch: str) -> Optional[dict]:
        """Who owes whom, how much, and in which groups — for one claim."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.group_id, s.from_tg_id, s.to_tg_id, s.amount_cents,"
                " s.confirmed_by_to, s.created_at, g.currency, s.requested_by, s.requested_to"
                " FROM settlements s"
                ' JOIN "groups" g ON g.id = s.group_id'
                " WHERE s.batch=?",
                (batch,),
            ).fetchall()
        if not rows:
            return None
        # The payer is the side that hands money over; a full settlement can
        # also carry the opposite half of a cross-group offset, which is
        # bookkeeping rather than a transfer.
        paid = {}
        for r in rows:
            key = (r["from_tg_id"], r["to_tg_id"])
            paid[key] = paid.get(key, 0) + r["amount_cents"]
        (frm, to), amount = max(paid.items(), key=lambda kv: kv[1])
        if rows[0]["requested_by"] is not None:
            frm, to = rows[0]["requested_by"], rows[0]["requested_to"]
        amount = paid.get((frm, to), 0) - paid.get((to, frm), 0)
        return {
            "batch": batch,
            "from": frm,
            "to": to,
            "amount_cents": amount,
            "currency": rows[0]["currency"],
            "confirmed": bool(rows[0]["confirmed_by_to"]),
            "created_at": rows[0]["created_at"],
            "groups": sorted({r["group_id"] for r in rows}),
        }

    @atomic
    def confirm_settlement(self, batch: str, uid: int) -> bool:
        """Only the person the money was owed to can confirm it arrived."""
        info = self.batch_info(batch)
        if info is None or info["confirmed"] or info["to"] != uid:
            return False
        with self._lock:
            self._conn.execute(
                "UPDATE settlements SET confirmed_by_to=1"
                " WHERE batch=? AND confirmed_by_to=0",
                (batch,),
            )
        return True

    @atomic
    def reject_settlement(self, batch: str, uid: int) -> bool:
        """Drop an unconfirmed claim.

        The recipient rejects a payment they never got; the payer withdraws
        a claim they sent by mistake. Either way the debt simply stays.
        """
        info = self.batch_info(batch)
        if info is None or info["confirmed"] or uid not in (info["from"], info["to"]):
            return False
        with self._lock:
            self._conn.execute(
                "DELETE FROM settlements WHERE batch=? AND confirmed_by_to=0",
                (batch,),
            )
        return True

    def list_pending_settlements(self, uid: int) -> list[dict]:
        """Open claims this person sent or has to answer, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT batch FROM settlements"
                " WHERE confirmed_by_to=0 AND batch IS NOT NULL"
                " AND (from_tg_id=? OR to_tg_id=?)"
                " ORDER BY created_at DESC",
                (uid, uid),
            ).fetchall()
        infos = [self.batch_info(r["batch"]) for r in rows]
        return [i for i in infos if i is not None]

    def count_group_settlements(self, group_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM settlements WHERE group_id=?",
                (group_id,),
            ).fetchone()
        return row[0] if row else 0

    def list_group_settlements(
        self, group_id: int, limit: int, offset: int
    ) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, from_tg_id, to_tg_id, amount_cents, created_at,"
                " confirmed_by_to, batch"
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
                "confirmed": bool(r["confirmed_by_to"]),
                "batch": r["batch"] or "",
            }
            for r in rows
        ]

    def can_delete_settlement(
        self, settlement_id: int, group_id: int, uid: int
    ) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT from_tg_id FROM settlements WHERE id=? AND group_id=?",
                (settlement_id, group_id),
            ).fetchone()
        if row is None:
            return False
        return row["from_tg_id"] == uid or self.is_group_owner(group_id, uid)

    @atomic
    def delete_settlement(self, settlement_id: int, group_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM settlements WHERE id=? AND group_id=?",
                (settlement_id, group_id),
            )

    # -- export --

    def export_group(self, group_id: int) -> dict:
        """Every row a member would need to re-check the group's maths.

        Deleted expenses are left out: they do not affect any debt, so
        showing them would only invite people to add them back in by hand.
        """
        with self._lock:
            expense_rows = self._conn.execute(
                "SELECT id, payer_tg_id, created_by_tg_id, description,"
                " amount_cents, created_at, updated_at, orig_currency,"
                " orig_amount_cents, receipt_file_id"
                " FROM expenses WHERE group_id=? AND deleted=0"
                " ORDER BY created_at, id",
                (group_id,),
            ).fetchall()
            share_rows = self._conn.execute(
                "SELECT p.expense_id, p.participant_tg_id, p.share_cents"
                " FROM expense_participants p"
                " JOIN expenses e ON e.id = p.expense_id"
                " WHERE e.group_id=? AND e.deleted=0",
                (group_id,),
            ).fetchall()
            settlement_rows = self._conn.execute(
                "SELECT id, from_tg_id, to_tg_id, amount_cents,"
                " confirmed_by_to, created_at"
                " FROM settlements WHERE group_id=? ORDER BY created_at, id",
                (group_id,),
            ).fetchall()

        shares: dict[int, dict[int, int]] = {}
        for r in share_rows:
            shares.setdefault(r["expense_id"], {})[r["participant_tg_id"]] = r[
                "share_cents"
            ]

        expenses = [
            {
                "id": r["id"],
                "payer": r["payer_tg_id"],
                "created_by": r["created_by_tg_id"],
                "desc": r["description"],
                "amount_cents": r["amount_cents"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"] or 0,
                "orig_currency": r["orig_currency"] or "",
                "orig_amount_cents": r["orig_amount_cents"] or 0,
                "receipt": r["receipt_file_id"] or "",
                "shares": shares.get(r["id"], {}),
            }
            for r in expense_rows
        ]
        settlements = [
            {
                "id": r["id"],
                "from": r["from_tg_id"],
                "to": r["to_tg_id"],
                "amount_cents": r["amount_cents"],
                "counted": bool(r["confirmed_by_to"]),
                "created_at": r["created_at"],
            }
            for r in settlement_rows
        ]

        members = self.list_members_detailed(group_id)
        names = {m["id"]: m["name"] for m in members}
        for e in expenses:
            for uid in [e["payer"], e["created_by"], *e["shares"]]:
                if uid and uid not in names:
                    names[uid] = self.user_name(uid)
        for s in settlements:
            for uid in (s["from"], s["to"]):
                if uid not in names:
                    names[uid] = self.user_name(uid)

        return {
            "group_id": group_id,
            "title": self.get_group_title(group_id),
            "currency": self.group_currency(group_id),
            "members": members,
            "names": names,
            "expenses": expenses,
            "settlements": settlements,
            "balances": self.compute_group_balances(group_id),
        }
