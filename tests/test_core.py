import asyncio
import io
import sqlite3
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import main


class FakePhoto:
    def __init__(self, file_id):
        self.file_id = file_id


class FakeBot:
    """Messages the bot sends to people other than the one who acted."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))


class FakeChat:
    def __init__(self):
        self.sent = []
        self.documents = []
        self.photos = []

    async def send_message(self, text, reply_markup=None, **kwargs):
        self.sent.append((text, reply_markup))

    async def send_document(self, document, filename=None, caption=None, **kwargs):
        self.documents.append((document, filename, caption))

    async def send_photo(self, photo, caption=None, **kwargs):
        self.photos.append((photo, caption))


class FakeQuery:
    """A pressed inline button: it only has to answer and carry its data."""

    def __init__(self, data):
        self.data = data
        self.message = None
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text or "", show_alert))


class FakeUpdate:
    def __init__(self, uid, text, callback_data=None, photo=None):
        self.effective_user = types.SimpleNamespace(
            id=uid, username=f"u{uid}", first_name="U", last_name=None
        )
        self.effective_chat = FakeChat()
        self.effective_message = types.SimpleNamespace(
            text=text, photo=[FakePhoto(f) for f in (photo or [])]
        )
        self.callback_query = FakeQuery(callback_data) if callback_data else None


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

    def settle(self, repo, uid, other, amount, currency="RUB"):
        """The full flow: the payer claims, the person owed confirms."""
        batch = repo.request_settlement(uid, other, currency, amount)
        return bool(batch) and repo.confirm_settlement(batch, other)

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
        self.assertTrue(self.settle(repo, 2, 1, 500))
        self.assertEqual(repo.compute_group_balances(gid), {})
        self.assertFalse(self.settle(repo, 2, 1, 500))
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
        self.assertFalse(self.settle(repo, 1, 2, 500))
        self.assertTrue(self.settle(repo, 1, 3, 500))
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
        self.assertEqual(debts[2]["RUB"]["net"], 0)
        self.assertEqual(debts[2]["RUB"]["by_group"], {g1: -500, g2: 500})

        # Nothing to transfer, but closing the books clears both groups.
        self.assertFalse(self.settle(repo, 1, 2, 500))
        self.assertTrue(self.settle(repo, 1, 2, 0))
        self.assertEqual(repo.compute_group_balances(g1), {})
        self.assertEqual(repo.compute_group_balances(g2), {})
        self.assertEqual(repo.compute_user_debts(1), {})

    def test_partially_offsetting_debts_settle_in_one_payment(self):
        repo, g1, g2 = self.make_two_groups()
        # 1 owes 2 fifty in g1; 2 owes 1 twenty in g2 -> net thirty.
        repo.create_expense(g1, 2, 2, "бензин", 1000, {1: 500, 2: 500})
        repo.create_expense(g2, 1, 1, "интернет", 400, {1: 200, 2: 200})

        self.assertEqual(repo.compute_user_debts(1)[2]["RUB"]["net"], -300)
        self.assertFalse(self.settle(repo, 1, 2, 500))
        self.assertTrue(self.settle(repo, 1, 2, 300))
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
        self.assertEqual(debts[2]["RUB"]["net"], -500)
        self.assertEqual(debts[4]["RUB"]["net"], 500)
        self.assertNotIn(4, repo.compute_user_debts(2))
        self.assertNotIn(2, repo.compute_user_debts(4))

    def test_settlement_history_and_cancel(self):
        repo, gid = self.make_repo()
        repo.create_expense(gid, 2, 1, "hotel", 1000, {1: 500, 2: 500})
        self.assertTrue(self.settle(repo, 2, 1, 500))

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
        ctx = types.SimpleNamespace(user_data={}, bot=FakeBot())
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


class ExportTest(unittest.TestCase):
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

    def make_repo(self):
        repo = main.Repo(":memory:")
        repo.upsert_user(1, "@ivan")
        repo.upsert_user(2, "@olya")
        repo.upsert_user(3, "@petr")
        gid, code = repo.create_group("Поездка", 1)
        repo.join_by_code(code, 2)
        repo.join_by_code(code, 3)
        # 1 pays for everyone, 2 pays for a dinner without 3.
        repo.create_expense(gid, 1, 1, "такси", 150000, {1: 50000, 2: 50000, 3: 50000})
        repo.create_expense(gid, 2, 2, 'ужин "У моря" & вино', 90000, {1: 45000, 2: 45000})
        return repo, gid

    def col_index(self, letters: str) -> int:
        index = 0
        for ch in letters:
            index = index * 26 + (ord(ch) - ord("A") + 1)
        return index - 1

    def sheets(self, blob):
        """Read the workbook back as {sheet name: [[cell, …], …]}.

        Parsed straight from the XML so the test needs no Excel library and
        fails if the file stops being well-formed.
        """
        archive = zipfile.ZipFile(io.BytesIO(blob))
        book = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = {
            r.get("Id"): r.get("Target")
            for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        }
        rid = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

        result = {}
        for sheet in book.iter(f"{self.NS}sheet"):
            part = "xl/" + rels[sheet.get(rid)]
            rows = []
            for row in ET.fromstring(archive.read(part)).iter(f"{self.NS}row"):
                cells = {}
                for c in row.iter(f"{self.NS}c"):
                    column = "".join(ch for ch in c.get("r") if ch.isalpha())
                    if c.get("t") == "inlineStr":
                        cells[column] = c.find(f"{self.NS}is/{self.NS}t").text
                    else:
                        cells[column] = float(c.find(f"{self.NS}v").text)
                last = max((self.col_index(col) for col in cells), default=-1)
                rows.append([cells.get(main._col_letter(i)) for i in range(last + 1)])
            # Empty cells are left out of the file, so pad the short rows back
            # to a rectangle and let the tests index columns directly.
            width = max((len(r) for r in rows), default=0)
            result[sheet.get("name")] = [r + [None] * (width - len(r)) for r in rows]
        return result

    def test_file_is_a_workbook_with_every_screen(self):
        repo, gid = self.make_repo()
        blob = main.build_group_workbook(repo.export_group(gid))

        archive = zipfile.ZipFile(io.BytesIO(blob))
        self.assertIsNone(archive.testzip())
        for part in ["[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                     "xl/_rels/workbook.xml.rels", "xl/styles.xml"]:
            self.assertIn(part, archive.namelist())
        for part in archive.namelist():
            ET.fromstring(archive.read(part))  # every part is well-formed XML

        self.assertEqual(
            list(self.sheets(blob)),
            ["Траты", "Итоги по людям", "Кто кому платит", "Платежи",
             "Как проверить"],
        )

    def test_every_expense_lands_with_its_shares(self):
        repo, gid = self.make_repo()
        rows = self.sheets(main.build_group_workbook(repo.export_group(gid)))["Траты"]

        header, taxi, dinner, totals = rows
        self.assertEqual(
            header[:5],
            ["№", "Дата", "Описание", "Сумма, RUB", "Сумма долей, RUB"],
        )
        self.assertEqual(header[12:], ["Доля: @ivan", "Доля: @olya", "Доля: @petr"])

        # Amounts are numbers, not text, or nobody can sum the column.
        self.assertEqual(taxi[2:5], ["такси", 1500.0, 1500.0])
        self.assertEqual(taxi[12:], [500.0, 500.0, 500.0])
        # Quotes and ampersands survive the XML escaping.
        self.assertEqual(dinner[2:5], ['ужин "У моря" & вино', 900.0, 900.0])
        # 3 was not at the dinner, so their cell stays empty rather than 0.
        self.assertEqual(dinner[12:], [450.0, 450.0, None])

        self.assertEqual(totals[2:5], ["ИТОГО", 2400.0, 2400.0])
        self.assertEqual(totals[12:], [950.0, 950.0, 500.0])

    def test_balances_add_up_to_zero_and_match_the_bot(self):
        repo, gid = self.make_repo()
        batch = repo.request_settlement(3, 1, "RUB", 50000)
        repo.confirm_settlement(batch, 1)
        book = self.sheets(main.build_group_workbook(repo.export_group(gid)))

        by_name = {row[0]: row for row in book["Итоги по людям"][1:]}
        # paid − share + sent − received, the formula the sheet explains.
        self.assertEqual(by_name["@ivan"][1:6], [1500.0, 950.0, 0.0, 500.0, 50.0])
        self.assertEqual(by_name["@olya"][1:6], [900.0, 950.0, 0.0, 0.0, -50.0])
        self.assertEqual(by_name["@petr"][1:6], [0.0, 500.0, 500.0, 0.0, 0.0])
        self.assertEqual(by_name["ИТОГО"][5], 0.0)

        transfers = book["Кто кому платит"][1:]
        self.assertEqual(transfers, [["@olya", "@ivan", 50.0]])
        self.assertEqual(
            repo.compute_group_balances(gid), {(2, 1): 5000}
        )

        payments = book["Платежи"][1:]
        self.assertEqual(
            [p[2:] for p in payments], [["@petr", "@ivan", 500.0, "подтверждён"]]
        )

    def test_deleted_expenses_stay_out_of_the_export(self):
        repo, gid = self.make_repo()
        eid = repo.create_expense(gid, 3, 3, "отменённая", 30000, {1: 10000, 2: 10000, 3: 10000})
        repo.delete_expense(eid)

        rows = self.sheets(main.build_group_workbook(repo.export_group(gid)))["Траты"]
        self.assertNotIn("отменённая", [r[2] for r in rows])
        self.assertEqual(rows[-1][3], 2400.0)

    def test_foreign_currency_receipt_and_edits_reach_the_file(self):
        repo, gid = self.make_repo()
        eid = repo.create_expense(
            gid, 1, 1, "паром", 60000, {1: 30000, 2: 30000},
            orig_currency="EUR", orig_amount_cents=1000,
        )
        repo.set_receipt(eid, gid, "file-1")
        repo.update_expense(
            eid, gid, 1, "паром", 60000, {1: 30000, 2: 30000},
            orig_currency="EUR", orig_amount_cents=1000,
        )

        book = self.sheets(main.build_group_workbook(repo.export_group(gid)))
        header = book["Траты"][0]
        self.assertEqual(
            header[7:12],
            ["Оплачено в валюте", "Сумма в валюте", "Курс к RUB", "Чек", "Изменена"],
        )
        ferry = [r for r in book["Траты"] if r[2] == "паром"][0]
        self.assertEqual(ferry[7:9], ["EUR", 10.0])
        self.assertEqual(ferry[9], 60.0)  # 600 RUB for one 10 EUR unit
        self.assertEqual(ferry[10], "да")
        self.assertTrue(ferry[11])

    def test_unconfirmed_payments_are_marked_as_such(self):
        repo, gid = self.make_repo()
        repo.request_settlement(2, 1, "RUB", 5000)

        book = self.sheets(main.build_group_workbook(repo.export_group(gid)))
        self.assertEqual(book["Платежи"][1][5], "ждёт подтверждения")
        # …and it changes nothing on the balances sheet yet.
        totals = {row[0]: row for row in book["Итоги по людям"][1:]}
        self.assertEqual(totals["@olya"][3], 0.0)

    def test_button_sends_the_file_to_a_member_only(self):
        repo, gid = self.make_repo()
        app = main.App(repo, "bot")

        update = FakeUpdate(2, "", callback_data=f"xlsx|{gid}")
        asyncio.run(app.on_callback(update, types.SimpleNamespace(user_data={}, bot=None)))
        (document, filename, caption), = update.effective_chat.documents
        self.assertTrue(filename.endswith(".xlsx"), filename)
        self.assertIn("Поездка", filename)
        self.assertIn(f"#{gid}", caption)
        self.assertEqual(document.getvalue()[:2], b"PK")

        outsider = FakeUpdate(9, "", callback_data=f"xlsx|{gid}")
        asyncio.run(app.on_callback(outsider, types.SimpleNamespace(user_data={}, bot=None)))
        self.assertEqual(outsider.effective_chat.documents, [])
        self.assertIn("Нет доступа", outsider.callback_query.answers[0][0])

    def test_empty_group_says_so_instead_of_sending_a_file(self):
        repo = main.Repo(":memory:")
        repo.upsert_user(1, "@ivan")
        gid, _ = repo.create_group("Пустая", 1)
        app = main.App(repo, "bot")

        update = FakeUpdate(1, "", callback_data=f"xlsx|{gid}")
        asyncio.run(app.on_callback(update, types.SimpleNamespace(user_data={}, bot=None)))
        self.assertEqual(update.effective_chat.documents, [])
        self.assertIn("нет трат", update.effective_chat.sent[-1][0])


class MessageSplitTest(unittest.TestCase):
    def test_short_text_stays_one_message(self):
        self.assertEqual(main.split_message("привет"), ["привет"])

    def test_long_screen_is_cut_on_line_boundaries(self):
        text = "".join(f"строка {i}\n" for i in range(2000))
        chunks = main.split_message(text)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), main.MESSAGE_LIMIT)
        self.assertEqual("".join(chunks), text)
        # No line is torn in half.
        for chunk in chunks:
            self.assertTrue(chunk.endswith("\n"))

    def test_one_endless_line_is_split_anyway(self):
        text = "x" * (main.MESSAGE_LIMIT * 2 + 5)
        chunks = main.split_message(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(c) <= main.MESSAGE_LIMIT for c in chunks))

    def test_buttons_ride_on_the_last_chunk(self):
        """Telegram refuses an oversized message outright, so a debts screen
        that outgrew the limit would never arrive at all."""
        repo = main.Repo(":memory:")
        app = main.App(repo, "bot")
        update = FakeUpdate(1, "")
        markup = main.InlineKeyboardMarkup([[
            main.InlineKeyboardButton("ок", callback_data="noop")
        ]])

        text = "".join(f"строка {i}\n" for i in range(2000))
        asyncio.run(app._edit_or_send(update, text, markup))

        sent = update.effective_chat.sent
        self.assertGreater(len(sent), 1)
        self.assertIsNone(sent[0][1])
        self.assertIs(sent[-1][1], markup)


class TimezoneTest(unittest.TestCase):
    def test_offsets_are_read_in_the_shapes_people_type(self):
        for raw, minutes in [
            ("+3", 180), ("3", 180), ("-5", -300), ("+03:00", 180),
            ("-05:30", -330), ("0", 0), ("UTC+2", 120), ("+0245", 165),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(main.parse_tz_offset(raw), minutes)

    def test_nonsense_offsets_are_refused(self):
        for raw in ["", "завтра", "+25", "-15:00", "3:0:0"]:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    main.parse_tz_offset(raw)

    def test_timestamps_follow_the_viewer(self):
        # 2021-01-01 00:00 UTC
        ts = 1609459200
        self.assertEqual(main.format_time(ts, 0), "2021-01-01 00:00")
        self.assertEqual(main.format_time(ts, 180), "2021-01-01 03:00")
        self.assertEqual(main.format_time(ts, -330), "2020-12-31 18:30")
        self.assertEqual(main.tz_label(-330), "UTC-05:30")

    def test_command_stores_the_offset(self):
        repo = main.Repo(":memory:")
        app = main.App(repo, "bot")
        update = FakeUpdate(1, "/tz +3")
        ctx = types.SimpleNamespace(user_data={}, bot=FakeBot(), args=["+3"])

        asyncio.run(app.on_tz(update, ctx))
        self.assertEqual(repo.user_tz(1), 180)
        self.assertIn("UTC+03:00", update.effective_chat.sent[-1][0])

    def test_old_database_gets_the_timezone_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "old.db"
            schema = (main.MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")
            conn = sqlite3.connect(db_path)
            conn.executescript(schema)
            conn.execute("INSERT INTO users(tg_id,name) VALUES(1,'owner')")
            conn.commit()
            conn.close()

            repo = main.Repo(str(db_path))
            self.assertTrue(repo._column_exists("users", "tz_offset_min"))
            self.assertEqual(repo.user_tz(1), 0)
            repo.close()


class CurrencyTest(unittest.TestCase):
    def make_repo(self):
        repo = main.Repo(":memory:")
        repo.upsert_user(1, "@ivan")
        repo.upsert_user(2, "@olya")
        gid_ru, code_ru = repo.create_group("Сочи", 1)
        gid_tr, code_tr = repo.create_group("Стамбул", 1, "TRY")
        repo.join_by_code(code_ru, 2)
        repo.join_by_code(code_tr, 2)
        return repo, gid_ru, gid_tr

    def test_currency_is_read_off_the_amount(self):
        self.assertEqual(
            main.split_amount_currency_desc("100 EUR ужин"), ("100", "EUR", "ужин")
        )
        self.assertEqual(
            main.split_amount_currency_desc("1500€ такси"), ("1500", "EUR", "такси")
        )
        # An ordinary three-letter word is a description, not a currency.
        self.assertEqual(
            main.split_amount_currency_desc("1500 gas station"),
            ("1500", "", "gas station"),
        )
        self.assertEqual(
            main.split_amount_currency_desc("1200 обед"), ("1200", "", "обед")
        )

    def test_debts_in_different_currencies_never_cancel(self):
        repo, gid_ru, gid_tr = self.make_repo()
        # 2 owes 1 in roubles, 1 owes 2 the same number of lira.
        repo.create_expense(gid_ru, 1, 1, "такси", 10000, {1: 5000, 2: 5000})
        repo.create_expense(gid_tr, 2, 2, "кофе", 10000, {1: 5000, 2: 5000})

        debts = repo.compute_user_debts(2)
        self.assertEqual(debts[1]["RUB"]["net"], -5000)
        self.assertEqual(debts[1]["TRY"]["net"], 5000)

        # Paying the roubles leaves the lira exactly where they were.
        batch = repo.request_settlement(2, 1, "RUB", 5000)
        self.assertTrue(repo.confirm_settlement(batch, 1))
        self.assertEqual(repo.compute_user_debts(2)[1]["TRY"]["net"], 5000)
        self.assertNotIn("RUB", repo.compute_user_debts(2)[1])

    def test_paying_in_a_currency_with_no_debt_is_refused(self):
        repo, gid_ru, _ = self.make_repo()
        repo.create_expense(gid_ru, 1, 1, "такси", 10000, {1: 5000, 2: 5000})
        self.assertEqual(repo.request_settlement(2, 1, "TRY", 5000), "")
        self.assertEqual(repo.request_settlement(2, 1, "НЕТ", 5000), "")

    def test_expense_keeps_what_was_actually_paid(self):
        repo, _, gid_tr = self.make_repo()
        eid = repo.create_expense(
            gid_tr, 1, 1, "ужин", 30000, {1: 15000, 2: 15000},
            orig_currency="EUR", orig_amount_cents=1000,
        )
        item = repo.get_expense(eid, gid_tr)
        self.assertEqual(item["orig_currency"], "EUR")
        self.assertEqual(item["orig_amount_cents"], 1000)
        # The debt itself stays in the group's own currency.
        self.assertEqual(repo.compute_group_balances(gid_tr), {(2, 1): 15000})
        self.assertEqual(
            main.format_rate(30000, 1000, "TRY", "EUR"), "1 EUR = 30.0000 TRY"
        )

    def test_currency_is_locked_once_money_is_recorded(self):
        repo, gid_ru, _ = self.make_repo()
        self.assertTrue(repo.can_change_currency(gid_ru))
        self.assertTrue(repo.set_group_currency(gid_ru, "usd"))
        self.assertEqual(repo.group_currency(gid_ru), "USD")

        repo.create_expense(gid_ru, 1, 1, "такси", 10000, {1: 5000, 2: 5000})
        self.assertFalse(repo.can_change_currency(gid_ru))
        self.assertFalse(repo.set_group_currency(gid_ru, "EUR"))
        self.assertEqual(repo.group_currency(gid_ru), "USD")

    def test_old_database_gets_a_currency(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "old.db"
            schema = (main.MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")
            conn = sqlite3.connect(db_path)
            conn.executescript(schema)
            conn.execute("INSERT INTO users(tg_id,name) VALUES(1,'owner')")
            conn.execute(
                "INSERT INTO groups(id,title,owner_tg_id,invite_code,created_at)"
                " VALUES(1,'g',1,'abc',0)"
            )
            conn.commit()
            conn.close()

            repo = main.Repo(str(db_path))
            self.assertEqual(repo.group_currency(1), "RUB")
            repo.close()


class SettlementFlowTest(unittest.TestCase):
    def make_repo(self):
        repo = main.Repo(":memory:")
        for uid, name in [(1, "@ivan"), (2, "@olya"), (3, "@petr")]:
            repo.upsert_user(uid, name)
        gid, code = repo.create_group("Сочи", 1)
        repo.join_by_code(code, 2)
        repo.join_by_code(code, 3)
        repo.create_expense(gid, 1, 1, "отель", 30000, {1: 10000, 2: 10000, 3: 10000})
        return repo, gid

    def test_a_claim_changes_nothing_until_it_is_confirmed(self):
        repo, gid = self.make_repo()
        batch = repo.request_settlement(2, 1, "RUB", 10000)

        self.assertTrue(batch)
        self.assertEqual(repo.compute_group_balances(gid)[(2, 1)], 10000)
        info = repo.batch_info(batch)
        self.assertEqual((info["from"], info["to"], info["amount_cents"]), (2, 1, 10000))
        self.assertFalse(info["confirmed"])

        self.assertTrue(repo.confirm_settlement(batch, 1))
        self.assertNotIn((2, 1), repo.compute_group_balances(gid))

    def test_only_the_person_owed_the_money_can_confirm(self):
        repo, _ = self.make_repo()
        batch = repo.request_settlement(2, 1, "RUB", 10000)

        self.assertFalse(repo.confirm_settlement(batch, 2))  # the payer
        self.assertFalse(repo.confirm_settlement(batch, 3))  # a bystander
        self.assertTrue(repo.confirm_settlement(batch, 1))
        self.assertFalse(repo.confirm_settlement(batch, 1))  # not twice

    def test_rejecting_leaves_the_debt_standing(self):
        repo, gid = self.make_repo()
        batch = repo.request_settlement(2, 1, "RUB", 10000)

        self.assertFalse(repo.reject_settlement(batch, 3))
        self.assertTrue(repo.reject_settlement(batch, 1))
        self.assertIsNone(repo.batch_info(batch))
        self.assertEqual(repo.compute_group_balances(gid)[(2, 1)], 10000)

    def test_the_payer_can_withdraw_their_own_claim(self):
        repo, _ = self.make_repo()
        batch = repo.request_settlement(2, 1, "RUB", 10000)
        self.assertTrue(repo.reject_settlement(batch, 2))
        self.assertIsNone(repo.batch_info(batch))

    def test_one_open_claim_at_a_time(self):
        repo, _ = self.make_repo()
        batch = repo.request_settlement(2, 1, "RUB", 4000)
        self.assertTrue(batch)
        # Without this guard an impatient tap pays the same debt twice.
        self.assertEqual(repo.request_settlement(2, 1, "RUB", 4000), "")
        repo.confirm_settlement(batch, 1)
        self.assertTrue(repo.request_settlement(2, 1, "RUB", 6000))

    def test_partial_payment_leaves_the_rest_owed(self):
        repo, gid = self.make_repo()
        batch = repo.request_settlement(2, 1, "RUB", 4000)
        repo.confirm_settlement(batch, 1)

        self.assertEqual(repo.compute_group_balances(gid)[(2, 1)], 6000)
        self.assertEqual(repo.compute_user_debts(2)[1]["RUB"]["net"], -6000)

    def test_partial_payment_fills_the_biggest_debt_first(self):
        repo, g1 = self.make_repo()
        _, code2 = repo.create_group("Дача", 1)
        repo.join_by_code(code2, 2)
        g2 = max(g["id"] for g in repo.list_user_groups(2))
        repo.create_expense(g2, 1, 1, "дрова", 4000, {1: 2000, 2: 2000})

        # 2 owes 100 in g1 and 20 in g2; paying 110 clears g1 first.
        batch = repo.request_settlement(2, 1, "RUB", 11000)
        repo.confirm_settlement(batch, 1)

        self.assertNotIn((2, 1), repo.compute_group_balances(g1))
        self.assertEqual(repo.compute_group_balances(g2), {(2, 1): 1000})

    def test_paying_more_than_owed_is_refused(self):
        repo, _ = self.make_repo()
        self.assertEqual(repo.request_settlement(2, 1, "RUB", 10001), "")
        self.assertEqual(repo.request_settlement(2, 1, "RUB", -5), "")

    def test_pending_claims_are_listed_for_both_sides(self):
        repo, _ = self.make_repo()
        batch = repo.request_settlement(2, 1, "RUB", 10000)
        for uid in (1, 2):
            with self.subTest(uid=uid):
                self.assertEqual(
                    [i["batch"] for i in repo.list_pending_settlements(uid)], [batch]
                )
        repo.confirm_settlement(batch, 1)
        self.assertEqual(repo.list_pending_settlements(1), [])

    def test_buttons_walk_the_whole_flow(self):
        repo, _ = self.make_repo()
        app = main.App(repo, "bot")
        bot = FakeBot()

        # The debtor claims the payment from the debts screen.
        payer = FakeUpdate(2, "", callback_data="paynet|to:1|cur:RUB|amt:10000")
        asyncio.run(app.on_callback(payer, types.SimpleNamespace(user_data={}, bot=bot)))
        batch = repo.list_pending_settlements(2)[0]["batch"]
        self.assertTrue(any("Подтвердите" in text for _, text, _ in bot.sent))

        # Until the creditor confirms, the screen says it is still waiting.
        screen = payer.effective_chat.sent[-1][0]
        self.assertIn("Ожидают подтверждения", screen)

        creditor = FakeUpdate(1, "", callback_data=f"paycfm|{batch}")
        asyncio.run(app.on_callback(creditor, types.SimpleNamespace(user_data={}, bot=bot)))
        self.assertTrue(repo.batch_info(batch)["confirmed"])

        # The settled debt is gone from the screen; the third member's is not.
        screen = creditor.effective_chat.sent[-1][0]
        self.assertNotIn("@olya", screen)
        self.assertIn("@petr", screen)
        self.assertTrue(any("подтвердил" in text for _, text, _ in bot.sent))

    def test_partial_payment_wizard_asks_for_an_amount(self):
        repo, _ = self.make_repo()
        app = main.App(repo, "bot")
        ctx = types.SimpleNamespace(user_data={}, bot=FakeBot())

        start = FakeUpdate(2, "", callback_data="paypart|to:1|cur:RUB")
        asyncio.run(app.on_callback(start, ctx))
        self.assertIn("Сколько вы отдали", start.effective_chat.sent[-1][0])

        too_much = FakeUpdate(2, "500")
        asyncio.run(app.on_text(too_much, ctx))
        self.assertIn("больше долга", too_much.effective_chat.sent[0][0])
        self.assertEqual(repo.list_pending_settlements(2), [])

        ok = FakeUpdate(2, "40")
        asyncio.run(app.on_text(ok, ctx))
        pending = repo.list_pending_settlements(2)
        self.assertEqual([i["amount_cents"] for i in pending], [4000])


class ExpenseEditTest(unittest.TestCase):
    def make_repo(self):
        repo = main.Repo(":memory:")
        for uid, name in [(1, "@ivan"), (2, "@olya"), (3, "@petr")]:
            repo.upsert_user(uid, name)
        gid, code = repo.create_group("Сочи", 1)
        repo.join_by_code(code, 2)
        repo.join_by_code(code, 3)
        eid = repo.create_expense(gid, 2, 1, "ужин", 30000, {1: 10000, 2: 10000, 3: 10000})
        return repo, gid, eid

    def test_editing_rewrites_the_split_in_place(self):
        repo, gid, eid = self.make_repo()
        repo.update_expense(eid, gid, 1, "ужин с вином", 40000, {1: 20000, 2: 20000})

        item = repo.get_expense(eid, gid)
        self.assertEqual(item["id"], eid)
        self.assertEqual(item["desc"], "ужин с вином")
        self.assertEqual(item["amount_cents"], 40000)
        self.assertEqual(item["shares"], {1: 20000, 2: 20000})
        self.assertTrue(item["updated_at"])
        # 3 was dropped from the split, so 3 owes nothing any more.
        self.assertEqual(repo.compute_group_balances(gid), {(2, 1): 20000})

    def test_only_the_creator_may_edit(self):
        repo, gid, eid = self.make_repo()
        self.assertTrue(repo.can_edit_expense(eid, gid, 2))
        self.assertFalse(repo.can_edit_expense(eid, gid, 1))
        self.assertFalse(repo.can_edit_expense(eid, gid, 3))

    def test_editing_refuses_a_stranger_as_participant(self):
        repo, gid, eid = self.make_repo()
        with self.assertRaises(ValueError):
            repo.update_expense(eid, gid, 1, "ужин", 30000, {9: 30000})
        self.assertEqual(repo.get_expense(eid, gid)["amount_cents"], 30000)

    def test_receipt_is_stored_and_cleared(self):
        repo, gid, eid = self.make_repo()
        repo.set_receipt(eid, gid, "file-123")
        self.assertEqual(repo.get_expense(eid, gid)["receipt"], "file-123")
        repo.set_receipt(eid, gid, "")
        self.assertEqual(repo.get_expense(eid, gid)["receipt"], "")

    def test_card_shows_the_split_and_edit_flow_saves_it(self):
        repo, gid, eid = self.make_repo()
        app = main.App(repo, "bot")
        ctx = types.SimpleNamespace(user_data={}, bot=FakeBot())

        card = FakeUpdate(2, "", callback_data=f"expcard|{eid}|gid:{gid}|p:0")
        asyncio.run(app.on_callback(card, ctx))
        text = card.effective_chat.sent[-1][0]
        self.assertIn(f"Трата #{eid}", text)
        self.assertIn("@petr: 100.00 RUB", text)

        # A member who did not add it gets no edit buttons.
        other = FakeUpdate(3, "", callback_data=f"expcard|{eid}|gid:{gid}|p:0")
        asyncio.run(app.on_callback(other, ctx))
        labels = [
            b.text
            for row in other.effective_chat.sent[-1][1].inline_keyboard
            for b in row
        ]
        self.assertNotIn("✏️ Изменить", labels)

        start = FakeUpdate(2, "", callback_data=f"expedit|{eid}|gid:{gid}|p:0")
        asyncio.run(app.on_callback(start, ctx))
        self.assertIn("Меняем трату", start.effective_chat.sent[-1][0])

        asyncio.run(app.on_text(FakeUpdate(2, "500 ужин с вином"), ctx))
        asyncio.run(app.on_callback(FakeUpdate(2, "", callback_data="payer|1"), ctx))
        asyncio.run(app.on_callback(FakeUpdate(2, "", callback_data="part_all"), ctx))
        asyncio.run(app.on_callback(FakeUpdate(2, "", callback_data="part_done"), ctx))
        asyncio.run(app.on_callback(FakeUpdate(2, "", callback_data="split|equal"), ctx))

        item = repo.get_expense(eid, gid)
        self.assertEqual(item["desc"], "ужин с вином")
        self.assertEqual(item["amount_cents"], 50000)
        self.assertTrue(item["updated_at"])
        # Still one expense: editing must not leave a duplicate behind.
        self.assertEqual(repo.count_group_expenses(gid), 1)

    def test_photo_attaches_only_to_the_expense_that_was_picked(self):
        repo, gid, eid = self.make_repo()
        app = main.App(repo, "bot")
        ctx = types.SimpleNamespace(user_data={}, bot=FakeBot())

        stray = FakeUpdate(2, "", photo=["small", "big"])
        asyncio.run(app.on_photo(stray, ctx))
        self.assertIn("Приложить чек", stray.effective_chat.sent[-1][0])
        self.assertEqual(repo.get_expense(eid, gid)["receipt"], "")

        asyncio.run(app.on_callback(
            FakeUpdate(2, "", callback_data=f"exprcpt|{eid}|gid:{gid}|p:0"), ctx
        ))
        asyncio.run(app.on_photo(FakeUpdate(2, "", photo=["small", "big"]), ctx))
        self.assertEqual(repo.get_expense(eid, gid)["receipt"], "big")

    def test_foreign_currency_wizard_asks_for_the_amount_in_group_money(self):
        repo, gid, _ = self.make_repo()
        app = main.App(repo, "bot")
        ctx = types.SimpleNamespace(user_data={}, bot=FakeBot())

        asyncio.run(app.on_callback(
            FakeUpdate(2, "", callback_data=f"aesel|{gid}"), ctx
        ))
        asked = FakeUpdate(2, "100 EUR паром")
        asyncio.run(app.on_text(asked, ctx))
        self.assertIn("сколько это в RUB", asked.effective_chat.sent[-1][0])

        asyncio.run(app.on_text(FakeUpdate(2, "9500"), ctx))
        asyncio.run(app.on_callback(FakeUpdate(2, "", callback_data="payer|2"), ctx))
        asyncio.run(app.on_callback(FakeUpdate(2, "", callback_data="part_all"), ctx))
        asyncio.run(app.on_callback(FakeUpdate(2, "", callback_data="part_done"), ctx))
        asyncio.run(app.on_callback(FakeUpdate(2, "", callback_data="split|equal"), ctx))

        added = [e for e in repo.list_group_expenses(gid, 10, 0) if e["desc"] == "паром"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["amount_cents"], 950000)
        self.assertEqual(added[0]["orig_currency"], "EUR")
        self.assertEqual(added[0]["orig_amount_cents"], 10000)


class GroupAdminTest(unittest.TestCase):
    def make_repo(self):
        repo = main.Repo(":memory:")
        for uid, name in [(1, "@owner"), (2, "@second"), (3, "@third")]:
            repo.upsert_user(uid, name)
        gid, code = repo.create_group("Сочи", 1)
        repo.join_by_code(code, 2)
        repo.join_by_code(code, 3)
        return repo, gid

    def test_rename_keeps_the_group(self):
        repo, gid = self.make_repo()
        self.assertTrue(repo.rename_group(gid, "  Сочи 2026  "))
        self.assertEqual(repo.get_group_title(gid), "Сочи 2026")
        self.assertFalse(repo.rename_group(gid, "   "))

    def test_a_member_with_an_open_balance_stays_put(self):
        repo, gid = self.make_repo()
        repo.create_expense(gid, 1, 1, "такси", 30000, {1: 10000, 2: 10000, 3: 10000})

        self.assertEqual(repo.remove_member(gid, 2), "has_debt")
        self.assertEqual(repo.leave_group(gid, 2), "has_debt")
        self.assertTrue(repo.is_group_member(gid, 2))

        batch = repo.request_settlement(2, 1, "RUB", 10000)
        repo.confirm_settlement(batch, 1)
        self.assertEqual(repo.leave_group(gid, 2), "")
        self.assertFalse(repo.is_group_member(gid, 2))
        # Their past expenses stay in the ledger.
        self.assertEqual(repo.count_group_expenses(gid), 1)

    def test_the_owner_hands_the_group_to_whoever_joined_first(self):
        repo, gid = self.make_repo()
        self.assertEqual(repo.next_owner(gid, 1), 2)
        self.assertEqual(repo.leave_group(gid, 1), "")
        self.assertTrue(repo.is_group_owner(gid, 2))
        self.assertFalse(repo.is_group_member(gid, 1))

    def test_the_last_member_leaving_takes_the_group_with_them(self):
        repo = main.Repo(":memory:")
        repo.upsert_user(1, "@owner")
        gid, _ = repo.create_group("Соло", 1)
        self.assertEqual(repo.leave_group(gid, 1), "last")
        self.assertEqual(repo.list_user_groups(1), [])

    def test_only_the_owner_removes_members(self):
        repo, gid = self.make_repo()
        app = main.App(repo, "bot")
        ctx = types.SimpleNamespace(user_data={}, bot=FakeBot())

        outsider = FakeUpdate(2, "", callback_data=f"memdel|3|gid:{gid}|p:0")
        asyncio.run(app.on_callback(outsider, ctx))
        self.assertTrue(repo.is_group_member(gid, 3))
        self.assertIn("владелец", outsider.callback_query.answers[-1][0])

        owner = FakeUpdate(1, "", callback_data=f"memdel|3|gid:{gid}|p:0")
        asyncio.run(app.on_callback(owner, ctx))
        self.assertFalse(repo.is_group_member(gid, 3))

    def test_settings_screen_renames_through_text(self):
        repo, gid = self.make_repo()
        app = main.App(repo, "bot")
        ctx = types.SimpleNamespace(user_data={}, bot=FakeBot())

        asyncio.run(app.on_callback(
            FakeUpdate(1, "", callback_data=f"grename|{gid}"), ctx
        ))
        asyncio.run(app.on_text(FakeUpdate(1, "Сочи 2026"), ctx))
        self.assertEqual(repo.get_group_title(gid), "Сочи 2026")

    def test_leaving_with_a_debt_is_blocked_in_the_ui(self):
        repo, gid = self.make_repo()
        repo.create_expense(gid, 1, 1, "такси", 30000, {1: 10000, 2: 10000, 3: 10000})
        app = main.App(repo, "bot")
        ctx = types.SimpleNamespace(user_data={}, bot=FakeBot())

        update = FakeUpdate(2, "", callback_data=f"gleave|{gid}")
        asyncio.run(app.on_callback(update, ctx))
        self.assertIn("закройте долги", update.callback_query.answers[-1][0])
        self.assertTrue(repo.is_group_member(gid, 2))


if __name__ == "__main__":
    unittest.main()
