import asyncio
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path

import main


class FakeChat:
    def __init__(self):
        self.sent = []

    async def send_message(self, text, reply_markup=None, **kwargs):
        self.sent.append((text, reply_markup))


class FakeUpdate:
    callback_query = None

    def __init__(self, uid, text):
        self.effective_user = types.SimpleNamespace(
            id=uid, username=f"u{uid}", first_name="U", last_name=None
        )
        self.effective_chat = FakeChat()
        self.effective_message = types.SimpleNamespace(text=text)


class AmountParserTest(unittest.TestCase):
    def test_amount_parsing(self):
        self.assertEqual(main.cents_from_str("12."), 1200)
        self.assertEqual(main.cents_from_str("12"), 1200)
        self.assertEqual(main.cents_from_str("0.1"), 10)
        self.assertEqual(main.cents_from_str("1 200"), 120000)
        self.assertEqual(main.cents_from_str("1_200.50"), 120050)
        self.assertEqual(main.split_amount_and_description("1 200 обед"), ("1 200", "обед"))

    def test_rejects_ambiguous_amounts(self):
        for value in ["1.234", "1,200", "1 20", str(main.MAX_AMOUNT_CENTS // 100 + 1)]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    main.cents_from_str(value)
        self.assertEqual(main.split_amount_and_description("1 20 обед"), ("", ""))


class SettleNetTest(unittest.TestCase):
    def test_chains_debts_through_intermediaries(self):
        # A owes B 100, B is owed 50 net after paying part of C's bill.
        result = main.settle_net({1: -150, 2: 50, 3: 100})
        self.assertEqual(result, {(1, 3): 100, (1, 2): 50})

    def test_pure_chain_removes_the_middleman(self):
        # A owes B 50 and B owes C 50 -> A pays C directly, B drops out.
        self.assertEqual(main.settle_net({1: -50, 2: 0, 3: 50}), {(1, 3): 50})

    def test_mutual_debts_cancel(self):
        self.assertEqual(main.settle_net({}), {})
        self.assertEqual(main.settle_net({1: 0, 2: 0}), {})

    def test_every_debt_is_covered_exactly(self):
        net = {1: -700, 2: -300, 3: 250, 4: 750}
        result = main.settle_net(net)
        paid: dict = {}
        for (frm, to), amount in result.items():
            self.assertGreater(amount, 0)
            paid[frm] = paid.get(frm, 0) - amount
            paid[to] = paid.get(to, 0) + amount
        self.assertEqual(paid, net)


class RepoTest(unittest.TestCase):
    def make_repo(self):
        repo = main.Repo(":memory:")
        repo.upsert_user(1, "owner")
        repo.upsert_user(2, "creator")
        repo.upsert_user(3, "member")
        repo.upsert_user(4, "outsider")
        gid, code = repo.create_group("trip", 1)
        repo.join_by_code(code, 2)
        repo.join_by_code(code, 3)
        return repo, gid

    def test_migrates_old_database_and_backfills_creator(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "old.db"
            schema = (main.MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")
            conn = sqlite3.connect(db_path)
            conn.executescript(schema)
            conn.execute("INSERT INTO users(tg_id,name) VALUES(1,'owner')")
            conn.execute("INSERT INTO groups(id,title,owner_tg_id,invite_code,created_at) VALUES(1,'g',1,'abc',0)")
            conn.execute("INSERT INTO group_members(group_id,tg_id,role) VALUES(1,1,'owner')")
            conn.execute(
                "INSERT INTO expenses(id,group_id,payer_tg_id,description,amount_cents,created_at)"
                " VALUES(1,1,1,'old',100,0)"
            )
            conn.commit()
            conn.close()

            repo = main.Repo(str(db_path))
            row = repo._conn.execute(
                "SELECT created_by_tg_id FROM expenses WHERE id=1"
            ).fetchone()
            self.assertEqual(row["created_by_tg_id"], 1)
            repo.close()

    def test_expense_delete_rights_use_creator(self):
        repo, gid = self.make_repo()
        expense_id = repo.create_expense(gid, 2, 1, "hotel", 900, {1: 300, 2: 300, 3: 300})

        self.assertTrue(repo.can_delete_expense(expense_id, gid, 2))
        self.assertFalse(repo.can_delete_expense(expense_id, gid, 1))
        self.assertFalse(repo.can_delete_expense(expense_id, gid, 3))
        self.assertFalse(repo.can_delete_expense(expense_id, gid, 4))

    def test_payment_confirmation_is_idempotent(self):
        repo, gid = self.make_repo()
        repo.create_expense(gid, 2, 1, "hotel", 1000, {1: 500, 2: 500})

        self.assertEqual(repo.compute_group_balances(gid), {(2, 1): 500})
        self.assertTrue(repo.settle_with_user(2, 1, 500))
        self.assertEqual(repo.compute_group_balances(gid), {})
        self.assertFalse(repo.settle_with_user(2, 1, 500))
        self.assertEqual(repo.compute_group_balances(gid), {})

    def test_chained_debts_are_simplified_across_the_group(self):
        repo, gid = self.make_repo()
        # 2 pays 200 split with 1 -> 1 owes 2 one hundred.
        repo.create_expense(gid, 2, 2, "ужин", 20000, {1: 10000, 2: 10000})
        # 3 pays 150 split three ways -> 1 and 2 owe 3 fifty each.
        repo.create_expense(gid, 3, 3, "такси", 15000, {1: 5000, 2: 5000, 3: 5000})

        # 2 owes nothing: their debt to 3 is carried by 1, who now owes 3 more.
        self.assertEqual(
            repo.compute_group_balances(gid),
            {(1, 3): 10000, (1, 2): 5000},
        )

    def test_chain_settles_with_a_single_payment(self):
        repo, gid = self.make_repo()
        # 1 owes 2, and 2 owes 3 the same amount: 2 should drop out entirely.
        repo.create_expense(gid, 2, 2, "обед", 1000, {1: 500, 2: 500})
        repo.create_expense(gid, 3, 3, "кофе", 1000, {2: 500, 3: 500})

        self.assertEqual(repo.compute_group_balances(gid), {(1, 3): 500})
        self.assertFalse(repo.settle_with_user(1, 2, 500))
        self.assertTrue(repo.settle_with_user(1, 3, 500))
        self.assertEqual(repo.compute_group_balances(gid), {})

    def make_two_groups(self):
        """Users 1 and 2 share two groups; user 3 only shares the second."""
        repo, g1 = self.make_repo()
        _, code2 = repo.create_group("flat", 1)
        repo.join_by_code(code2, 2)
        repo.join_by_code(code2, 3)
        g2 = max(g["id"] for g in repo.list_user_groups(1))
        return repo, g1, g2

    def test_opposite_debts_in_two_groups_cancel_out(self):
        repo, g1, g2 = self.make_two_groups()
        # 2 pays in the first group, 1 pays the same amount in the second.
        repo.create_expense(g1, 2, 2, "бензин", 1000, {1: 500, 2: 500})
        repo.create_expense(g2, 1, 1, "интернет", 1000, {1: 500, 2: 500})

        self.assertEqual(repo.compute_group_balances(g1), {(1, 2): 500})
        self.assertEqual(repo.compute_group_balances(g2), {(2, 1): 500})

        debts = repo.compute_user_debts(1)
        self.assertEqual(debts[2]["net"], 0)
        self.assertEqual(debts[2]["by_group"], {g1: -500, g2: 500})

        # Nothing to transfer, but closing the books clears both groups.
        self.assertFalse(repo.settle_with_user(1, 2, 500))
        self.assertTrue(repo.settle_with_user(1, 2, 0))
        self.assertEqual(repo.compute_group_balances(g1), {})
        self.assertEqual(repo.compute_group_balances(g2), {})
        self.assertEqual(repo.compute_user_debts(1), {})

    def test_partially_offsetting_debts_settle_in_one_payment(self):
        repo, g1, g2 = self.make_two_groups()
        # 1 owes 2 fifty in g1; 2 owes 1 twenty in g2 -> net thirty.
        repo.create_expense(g1, 2, 2, "бензин", 1000, {1: 500, 2: 500})
        repo.create_expense(g2, 1, 1, "интернет", 400, {1: 200, 2: 200})

        self.assertEqual(repo.compute_user_debts(1)[2]["net"], -300)
        self.assertFalse(repo.settle_with_user(1, 2, 500))
        self.assertTrue(repo.settle_with_user(1, 2, 300))
        self.assertEqual(repo.compute_user_debts(1), {})
        self.assertEqual(repo.compute_group_balances(g1), {})
        self.assertEqual(repo.compute_group_balances(g2), {})

    def test_debts_never_pair_users_without_a_shared_group(self):
        repo, _ = self.make_repo()
        _, code2 = repo.create_group("solo", 1)
        repo.join_by_code(code2, 4)
        g_a = max(g["id"] for g in repo.list_user_groups(2))
        g_b = max(g["id"] for g in repo.list_user_groups(1))

        # 1 owes 2 in one group; 4 owes 1 in another. 2 and 4 never meet.
        repo.create_expense(g_a, 2, 2, "обед", 1000, {1: 500, 2: 500})
        repo.create_expense(g_b, 1, 1, "такси", 1000, {1: 500, 4: 500})

        debts = repo.compute_user_debts(1)
        self.assertEqual(debts[2]["net"], -500)
        self.assertEqual(debts[4]["net"], 500)
        self.assertNotIn(4, repo.compute_user_debts(2))
        self.assertNotIn(2, repo.compute_user_debts(4))

    def test_settlement_history_and_cancel(self):
        repo, gid = self.make_repo()
        repo.create_expense(gid, 2, 1, "hotel", 1000, {1: 500, 2: 500})
        self.assertTrue(repo.settle_with_user(2, 1, 500))

        items = repo.list_group_settlements(gid, 10, 0)
        self.assertEqual(len(items), 1)
        settlement_id = items[0]["id"]
        self.assertTrue(repo.can_delete_settlement(settlement_id, gid, 2))
        self.assertTrue(repo.can_delete_settlement(settlement_id, gid, 1))
        self.assertFalse(repo.can_delete_settlement(settlement_id, gid, 4))

        repo.delete_settlement(settlement_id, gid)
        self.assertEqual(repo.count_group_settlements(gid), 0)
        self.assertEqual(repo.compute_group_balances(gid), {(2, 1): 500})


class KeyboardTest(unittest.TestCase):
    def labels(self):
        return [b.text for row in main.main_keyboard().keyboard for b in row]

    def send(self, app, uid, text):
        update = FakeUpdate(uid, text)
        ctx = types.SimpleNamespace(user_data={}, bot=None)
        asyncio.run(app.on_text(update, ctx))
        return update.effective_chat.sent

    def test_every_keyboard_button_gets_a_reply(self):
        """A button the router forgot about leaves the bot silent, which is
        how the old «Балансы»/«Взаимозачёт» buttons broke."""
        repo = main.Repo(":memory:")
        app = main.App(repo, "bot")
        for label in self.labels():
            with self.subTest(label=label):
                self.assertTrue(self.send(app, 1, label), f"{label} got no reply")

    def test_keyboard_buttons_are_blocked_during_a_wizard(self):
        for label in self.labels():
            self.assertIn(label, main._TOP_BUTTONS)

    def test_legacy_buttons_still_open_the_debts_screen(self):
        """Old clients keep showing the previous keyboard until the bot sends
        a new one, so its labels have to keep working."""
        repo = main.Repo(":memory:")
        repo.upsert_user(1, "@me")
        repo.create_group("trip", 1)
        app = main.App(repo, "bot")
        for label in main._LEGACY_DEBT_BUTTONS:
            with self.subTest(label=label):
                self.assertNotIn(label, self.labels())
                self.assertIn(label, main._TOP_BUTTONS)
                sent = self.send(app, 1, label)
                self.assertTrue(any("Долги" in t for t, _ in sent), sent)

    def test_stale_keyboard_is_pushed_once(self):
        repo = main.Repo(":memory:")
        app = main.App(repo, "bot")
        repo.upsert_user(1, "@me")
        repo._conn.execute("UPDATE users SET keyboard_version=0 WHERE tg_id=1")
        repo._conn.commit()
        self.assertTrue(repo.has_stale_keyboard(1))

        sent = self.send(app, 1, "📊 Балансы")
        notice, markup = sent[0]
        self.assertIn("обновились", notice)
        self.assertEqual(
            [[b.text for b in row] for row in markup.keyboard],
            [[b.text for b in row] for row in main.main_keyboard().keyboard],
        )
        self.assertFalse(repo.has_stale_keyboard(1))

        # The notice is not repeated on the next message.
        again = self.send(app, 1, "💰 Долги")
        self.assertFalse(any("обновились" in t for t, _ in again), again)

    def test_new_users_are_not_told_the_keyboard_changed(self):
        repo = main.Repo(":memory:")
        repo.upsert_user(7, "@new")
        self.assertFalse(repo.has_stale_keyboard(7))

    def test_old_database_gets_the_keyboard_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "old.db"
            schema = (main.MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")
            conn = sqlite3.connect(db_path)
            conn.executescript(schema)
            conn.execute("INSERT INTO users(tg_id,name) VALUES(1,'owner')")
            conn.commit()
            conn.close()

            repo = main.Repo(str(db_path))
            self.assertTrue(repo._column_exists("users", "keyboard_version"))
            self.assertTrue(repo.has_stale_keyboard(1))
            repo.close()


if __name__ == "__main__":
    unittest.main()
