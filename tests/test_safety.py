import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import main
import repository
from storage import restore_backup, verify_backup
from test_core import FakeBot, FakeUpdate


class SafetyTest(unittest.TestCase):
    def setUp(self):
        self.repo = main.Repo(":memory:")
        self.addCleanup(self.repo.close)
        for uid in (1, 2, 3, 4):
            self.repo.upsert_user(uid, f"user{uid}")
        self.gid, self.code = self.repo.create_group("Trip", 1)
        for uid in (2, 3):
            self.repo.join_by_code(self.code, uid)
        self.app = main.App(self.repo, "bot")
        self.ctx = types.SimpleNamespace(user_data={}, bot=FakeBot())

    def click(self, data, uid=1):
        update = FakeUpdate(uid, "", callback_data=data)
        asyncio.run(self.app.on_callback(update, self.ctx))
        return update

    def message(self, text, uid=1):
        update = FakeUpdate(uid, text)
        asyncio.run(self.app.on_text(update, self.ctx))
        return update

    def begin(self):
        self.click(f"aesel|{self.gid}")
        self.message("100 lunch")
        self.click("payer|1")
        self.click("part_all")
        self.click("part_done")

    def save(self, uid=1):
        token = self.ctx.user_data["add_expense"]["confirm_token"]
        return self.click(f"expsave|{token}", uid)

    def expense(self):
        return self.repo.create_expense(
            self.gid, 1, 1, "lunch", 10000, {1: 5000, 2: 5000}
        )

    def offset(self, first=100000, second=40000):
        gid2, code2 = self.repo.create_group("Flat", 1)
        self.repo.join_by_code(code2, 2)
        self.repo.create_expense(self.gid, 2, 2, "a", first, {1: first})
        self.repo.create_expense(gid2, 1, 1, "b", second, {2: second})

    def test_offset_pending_and_confirmation_show_actual_transfer(self):
        self.offset()
        batch = self.repo.request_settlement(1, 2, "RUB", 60000)
        self.assertEqual(self.repo.batch_info(batch)["amount_cents"], 60000)
        screen = self.click("debts", 2)
        self.assertIn("600.00 RUB", screen.effective_chat.sent[-1][0])
        self.assertNotIn("1000.00 RUB — подтвердите", screen.effective_chat.sent[-1][0])
        self.click(f"paycfm|{batch}", 2)
        self.assertIn("600.00 RUB", self.ctx.bot.sent[-1][1])
        self.assertEqual(self.repo.compute_user_debts(1), {})

    def test_zero_offset_keeps_initiator_when_reverse_direction_requested(self):
        self.offset(second=100000)
        batch = self.repo.request_settlement(2, 1, "RUB", 0)
        info = self.repo.batch_info(batch)
        self.assertEqual((info["from"], info["to"], info["amount_cents"]), (2, 1, 0))
        self.assertFalse(self.repo.confirm_settlement(batch, 2))
        self.assertTrue(self.repo.confirm_settlement(batch, 1))

    def test_legacy_offset_without_initiator_still_shows_net(self):
        self.offset()
        batch = self.repo.request_settlement(1, 2, "RUB", 60000)
        with self.repo._conn:
            self.repo._conn.execute(
                "UPDATE settlements SET requested_by=NULL, requested_to=NULL"
            )
        self.assertEqual(self.repo.batch_info(batch)["amount_cents"], 60000)

    def test_preview_does_not_write_and_double_save_does_not_duplicate(self):
        self.begin()
        preview = self.click("split|equal")
        self.assertIn("Проверьте", preview.effective_chat.sent[-1][0])
        self.assertEqual(self.repo.count_group_expenses(self.gid), 0)
        self.assertEqual(self.ctx.bot.sent, [])
        token = self.ctx.user_data["add_expense"]["confirm_token"]
        self.save()
        self.click(f"expsave|{token}")
        self.assertEqual(self.repo.count_group_expenses(self.gid), 1)
        item = self.repo.list_group_expenses(self.gid, 10, 0)[0]
        self.assertEqual(sum(self.repo.get_expense_shares(item["id"]).values()), 10000)

    def test_old_preview_cannot_save_after_editing_draft(self):
        self.begin()
        self.click("split|equal")
        token = self.ctx.user_data["add_expense"]["confirm_token"]
        self.click(f"review|amount|{token}")
        self.message("150 dinner")
        self.click(f"expsave|{token}")
        self.assertEqual(self.repo.count_group_expenses(self.gid), 0)
        self.click("payer|1")
        self.click("part_done")
        self.click("split|equal")
        self.save()
        self.assertEqual(
            self.repo.list_group_expenses(self.gid, 1, 0)[0]["amount_cents"], 15000
        )

    def test_failed_save_keeps_a_working_review_and_retry(self):
        self.begin()
        self.click("split|equal")
        with patch.object(
            self.repo,
            "create_expense",
            side_effect=sqlite3.OperationalError("disk full"),
        ):
            with self.assertLogs("handlers", level="ERROR"):
                update = self.save()
        self.assertEqual(self.repo.count_group_expenses(self.gid), 0)
        self.assertIn("Проверьте", update.effective_chat.sent[-1][0])
        self.save()
        self.assertEqual(self.repo.count_group_expenses(self.gid), 1)

    def test_last_custom_share_is_never_silently_replaced(self):
        self.begin()
        self.click("split|custom")
        self.message("20")
        self.message("30")
        last = self.ctx.user_data["add_expense"]["custom_left"][0]
        self.message("10")
        state = self.ctx.user_data["add_expense"]
        self.assertEqual(state["custom_left"], [last])
        self.assertNotIn(last, state["custom_shares"])
        self.assertEqual(self.repo.count_group_expenses(self.gid), 0)
        self.click(f"customremain|{last}")
        self.assertEqual(state["custom_shares"][last], 5000)
        self.assertEqual(self.repo.count_group_expenses(self.gid), 0)
        self.save()
        self.assertEqual(self.repo.count_group_expenses(self.gid), 1)

    def test_edit_notifies_removed_person_and_old_payer(self):
        eid = self.repo.create_expense(self.gid, 1, 3, "lunch", 10000, {2: 10000})
        self.click(f"expedit|{eid}|gid:{self.gid}|p:0")
        self.message("50 lunch")
        self.click("payer|1")
        self.click("part_me_payer")
        self.click("part_done")
        self.click("split|equal")
        self.assertEqual(self.repo.get_expense(eid, self.gid)["shares"], {2: 10000})
        self.save()
        notices = {pid: text for pid, text, _ in self.ctx.bot.sent}
        self.assertIn(2, notices)
        self.assertIn(3, notices)
        self.assertIn("100.00 RUB → 0.00 RUB", notices[2])

    def test_history_contains_before_after_actor_and_receipt(self):
        eid = self.expense()
        self.repo.update_expense(
            eid, self.gid, 1, "dinner", 12000, {1: 6000, 2: 6000}, actor=1
        )
        self.repo.set_receipt(eid, self.gid, "receipt-file", actor=1)
        history = self.repo.expense_history(eid, self.gid, 2)
        self.assertEqual([h["action"] for h in history], ["receipt", "edit", "create"])
        edit = history[1]
        self.assertEqual(edit["actor_tg_id"], 1)
        self.assertEqual(json.loads(edit["before_json"])["amount_cents"], 10000)
        self.assertEqual(json.loads(edit["after_json"])["amount_cents"], 12000)
        self.assertEqual(
            json.loads(history[0]["after_json"])["receipt"], "receipt-file"
        )
        screen = self.click(f"exphist|{eid}|{self.gid}|0", 2)
        self.assertIn("100.00 RUB → 120.00 RUB", screen.effective_chat.sent[-1][0])
        with self.assertRaises(ValueError):
            self.repo.expense_history(eid, self.gid, 4)
        denied = self.click(f"exphist|{eid}|{self.gid}|0", 4)
        self.assertEqual(denied.effective_chat.sent, [])

    def test_delete_and_restore_keep_history_receipt_and_balances(self):
        eid = self.expense()
        self.repo.set_receipt(eid, self.gid, "file")
        balances = self.repo.compute_group_balances(self.gid)
        deletion = self.click(f"expdel|{eid}|gid:{self.gid}|p:0")
        self.assertIn("восстановить", deletion.effective_chat.sent[-1][0])
        self.assertEqual(self.repo.compute_group_balances(self.gid), {})
        self.assertIsNone(self.repo.get_expense(eid, self.gid))
        self.click(f"exprestore|{eid}|{self.gid}|0", 2)
        self.assertIsNone(self.repo.get_expense(eid, self.gid))
        self.click(f"exprestore|{eid}|{self.gid}|0")
        self.assertEqual(self.repo.compute_group_balances(self.gid), balances)
        self.assertEqual(self.repo.get_expense(eid, self.gid)["receipt"], "file")
        self.assertFalse(self.repo.restore_expense(eid, self.gid, 1))
        self.assertEqual(
            [h["action"] for h in self.repo.expense_history(eid, self.gid, 2)],
            ["restore", "delete", "receipt", "create"],
        )

    def test_restore_refuses_to_recreate_debt_for_departed_member(self):
        eid = self.expense()
        self.repo.delete_expense(eid)
        self.assertEqual(self.repo.leave_group(self.gid, 2), "")
        with self.assertRaises(ValueError):
            self.repo.restore_expense(eid, self.gid, 1)
        self.assertIsNone(self.repo.get_expense(eid, self.gid))

    def test_deleted_list_remains_accessible_when_no_active_expenses(self):
        eid = self.expense()
        self.repo.delete_expense(eid)
        active = self.click(f"explist|{self.gid}|p:0")
        callbacks = [
            button.callback_data
            for row in active.effective_chat.sent[-1][1].inline_keyboard
            for button in row
        ]
        self.assertIn(f"expdeleted|{self.gid}|p:0", callbacks)
        deleted = self.click(f"expdeleted|{self.gid}|p:0")
        self.assertIn(f"#{eid}", deleted.effective_chat.sent[-1][0])
        denied = self.click(f"expdeleted|{self.gid}|p:0", 4)
        self.assertEqual(denied.effective_chat.sent, [])

    def test_history_pages_keep_earlier_revisions(self):
        eid = self.expense()
        for index in range(6):
            self.repo.set_receipt(eid, self.gid, f"file{index}")
        first = self.click(f"exphist|{eid}|{self.gid}|0")
        callbacks = [
            button.callback_data
            for row in first.effective_chat.sent[-1][1].inline_keyboard
            for button in row
        ]
        self.assertIn(f"exphist|{eid}|{self.gid}|1", callbacks)
        second = self.click(f"exphist|{eid}|{self.gid}|1")
        self.assertIn("Добавлена", second.effective_chat.sent[-1][0])

    def test_delete_refuses_to_reopen_settled_debt_for_departed_member(self):
        eid = self.expense()
        batch = self.repo.request_settlement(2, 1, "RUB", 5000)
        self.repo.confirm_settlement(batch, 1)
        self.assertEqual(self.repo.leave_group(self.gid, 2), "")
        with self.assertRaises(ValueError):
            self.repo.delete_expense(eid)
        self.assertIsNotNone(self.repo.get_expense(eid, self.gid))
        self.assertEqual(self.repo.compute_group_balances(self.gid), {})

    def test_invalid_amounts_are_rejected_at_repository_boundary(self):
        for amount, shares in [
            (100, {1: 99}),
            (0, {1: 0}),
            (100, {1: -1, 2: 101}),
            (100.0, {1: 100}),
            (100, {1: 100.0}),
        ]:
            with (
                self.subTest(amount=amount, shares=shares),
                self.assertRaises(ValueError),
            ):
                self.repo.create_expense(self.gid, 1, 1, "bad", amount, shares)
        self.assertEqual(self.repo.count_group_expenses(self.gid), 0)

    def test_database_triggers_reject_invalid_direct_writes(self):
        eid = self.expense()
        with self.assertRaises(sqlite3.IntegrityError), self.repo._conn:
            self.repo._conn.execute(
                "UPDATE expenses SET amount_cents=-1 WHERE id=?", (eid,)
            )
        with self.assertRaises(sqlite3.IntegrityError), self.repo._conn:
            self.repo._conn.execute(
                "UPDATE expense_participants SET share_cents=-1 WHERE expense_id=?",
                (eid,),
            )
        self.assertEqual(self.repo.get_expense(eid, self.gid)["amount_cents"], 10000)

    def test_failed_share_write_cannot_be_committed_by_next_action(self):
        self.repo._conn.execute(
            "CREATE TEMP TRIGGER fail_share BEFORE INSERT ON expense_participants WHEN NEW.participant_tg_id=2 BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.expense()
        self.repo.upsert_user(1, "changed")
        self.assertEqual(self.repo.count_group_expenses(self.gid), 0)
        self.assertEqual(
            self.repo._conn.execute(
                "SELECT COUNT(*) FROM expense_participants"
            ).fetchone()[0],
            0,
        )
        self.assertFalse(self.repo._conn.in_transaction)

    def test_history_failure_rolls_back_entire_edit(self):
        eid = self.expense()
        before = self.repo.get_expense(eid, self.gid)
        with patch.object(
            self.repo,
            "_record_history",
            side_effect=sqlite3.OperationalError("disk full"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.repo.update_expense(eid, self.gid, 2, "changed", 30000, {1: 30000})
        self.repo.upsert_user(1, "changed")
        self.assertEqual(self.repo.get_expense(eid, self.gid), before)

    def test_wrong_group_and_stale_revision_do_not_change_shares(self):
        eid = self.expense()
        other_gid, _ = self.repo.create_group("Other", 1)
        with self.assertRaises(ValueError):
            self.repo.update_expense(eid, other_gid, 1, "bad", 100, {1: 100})
        self.repo.set_receipt(eid, self.gid, "new")
        with self.assertRaises(ValueError):
            self.repo.update_expense(
                eid, self.gid, 1, "bad", 100, {1: 100}, expected_revision=1
            )
        self.assertEqual(self.repo.get_expense_shares(eid), {1: 5000, 2: 5000})

    def test_replayed_operation_has_one_expense_and_one_history_record(self):
        args = (self.gid, 1, 1, "lunch", 100, {1: 100})
        eid = self.repo.create_expense(*args, operation_id="replay")
        self.assertEqual(self.repo.create_expense(*args, operation_id="replay"), eid)
        self.assertEqual(self.repo.count_group_expenses(self.gid), 1)
        self.assertEqual(len(self.repo.expense_history(eid, self.gid, 1)), 1)


class BackupTest(unittest.TestCase):
    def test_backup_and_restore_cli_do_not_require_a_bot_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            live, backup, restored = (
                path / "live.db",
                path / "backup.db",
                path / "restored.db",
            )
            repo = main.Repo(str(live))
            repo.upsert_user(1, "owner")
            gid, _ = repo.create_group("Trip", 1)
            repo.create_expense(gid, 1, 1, "food", 100, {1: 100})
            repo.close()
            env = os.environ.copy()
            env.pop("BOT_TOKEN", None)
            env["DB_PATH"] = str(live)
            entrypoint = str(Path(main.__file__).resolve())
            for arguments in [
                ["backup", "--output", str(backup)],
                ["restore", "--backup", str(backup), "--output", str(restored)],
            ]:
                result = subprocess.run(
                    [sys.executable, "-B", entrypoint, *arguments],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            verify_backup(restored)

    def test_verified_snapshot_can_restore_balances_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            repo = main.Repo(str(path / "live.db"))
            try:
                repo.upsert_user(1, "A")
                repo.upsert_user(2, "B")
                gid, code = repo.create_group("Trip", 1)
                repo.join_by_code(code, 2)
                eid = repo.create_expense(gid, 1, 1, "food", 1000, {2: 1000})
                balances = repo.compute_group_balances(gid)
                backup = repo.backup(path / "snapshot.db")
                verify_backup(backup)
                repo.delete_expense(eid)
                restored = restore_backup(backup, path / "restored.db")
                with self.assertRaises(FileExistsError):
                    restore_backup(backup, restored)
            finally:
                repo.close()
            recovered = main.Repo(str(restored))
            try:
                self.assertEqual(recovered.compute_group_balances(gid), balances)
                self.assertEqual(
                    recovered.expense_history(eid, gid, 1)[0]["action"], "create"
                )
            finally:
                recovered.close()

    def test_invalid_backup_does_not_create_restore_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            source, target = path / "bad.db", path / "restore.db"
            source.write_bytes(b"not a database")
            with self.assertRaises(sqlite3.DatabaseError):
                restore_backup(source, target)
            self.assertFalse(target.exists())

    def test_failed_migration_rolls_back_schema_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            live = path / "live.db"
            main.Repo(str(live)).close()
            migrations = path / "migrations"
            shutil.copytree(main.MIGRATIONS_DIR, migrations)
            (migrations / "010_failure.sql").write_text(
                "ALTER TABLE expenses ADD COLUMN must_rollback TEXT;\n"
                "INSERT INTO table_does_not_exist VALUES(1);\n",
                encoding="utf-8",
            )
            with patch.object(repository, "MIGRATIONS_DIR", migrations):
                with self.assertRaises(sqlite3.OperationalError):
                    main.Repo(str(live))
            repo = main.Repo(str(live))
            try:
                columns = {
                    r[1] for r in repo._conn.execute("PRAGMA table_info(expenses)")
                }
                self.assertNotIn("must_rollback", columns)
                self.assertIsNone(
                    repo._conn.execute(
                        "SELECT 1 FROM schema_migrations WHERE version='010_failure'"
                    ).fetchone()
                )
                self.assertTrue(list((path / "backups").glob("*.db")))
            finally:
                repo.close()


if __name__ == "__main__":
    unittest.main()
