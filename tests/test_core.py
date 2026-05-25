import sqlite3
import tempfile
import unittest
from pathlib import Path

import main


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
        self.assertTrue(repo.add_settlement_if_current(gid, 2, 1, 500))
        self.assertEqual(repo.compute_group_balances(gid), {})
        self.assertFalse(repo.add_settlement_if_current(gid, 2, 1, 500))
        self.assertEqual(repo.compute_group_balances(gid), {})

    def test_settlement_history_and_cancel(self):
        repo, gid = self.make_repo()
        repo.create_expense(gid, 2, 1, "hotel", 1000, {1: 500, 2: 500})
        self.assertTrue(repo.add_settlement_if_current(gid, 2, 1, 500))

        items = repo.list_group_settlements(gid, 10, 0)
        self.assertEqual(len(items), 1)
        settlement_id = items[0]["id"]
        self.assertTrue(repo.can_delete_settlement(settlement_id, gid, 2))
        self.assertTrue(repo.can_delete_settlement(settlement_id, gid, 1))
        self.assertFalse(repo.can_delete_settlement(settlement_id, gid, 4))

        repo.delete_settlement(settlement_id, gid)
        self.assertEqual(repo.count_group_settlements(gid), 0)
        self.assertEqual(repo.compute_group_balances(gid), {(2, 1): 500})


if __name__ == "__main__":
    unittest.main()
