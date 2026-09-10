"""History, queue reconciliation and Telegram-only file storage at the HTTP boundary."""

import hashlib
import hmac
import json
import time
import unittest
import zipfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from telegram.error import BadRequest
from miniapp import create_web_app, RECEIPT_LIMIT
from repository import Repo

TOKEN = "123456:features-test-only"
PHOTO = b"\xff\xd8\xff" + b"photo" * 10000


def signed(uid):
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": uid, "first_name": f"User {uid}"}),
    }
    secret = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    fields["hash"] = hmac.new(
        secret,
        "\n".join(f"{k}={v}" for k, v in sorted(fields.items())).encode(),
        hashlib.sha256,
    ).hexdigest()
    return urlencode(fields)


class MiniAppFeaturesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = Repo(":memory:")
        for uid in (1, 2, 3):
            self.repo.upsert_user(uid, f"User {uid}")
        self.gid, code = self.repo.create_group("Trip", 1, "RUB")
        self.repo.join_by_code(code, 2)
        self.eid = self.repo.create_expense(
            self.gid, 1, 1, "Dinner", 10000, {1: 8000, 2: 2000}
        )
        self.revision = self.repo.get_expense(self.eid, self.gid)["revision"]
        self.remote = SimpleNamespace(
            file_size=len(PHOTO),
            download_as_bytearray=AsyncMock(return_value=bytearray(PHOTO)),
        )
        self.bot = SimpleNamespace(
            send_photo=AsyncMock(
                return_value=SimpleNamespace(
                    photo=[SimpleNamespace(file_id="telegram-photo-id")]
                )
            ),
            get_file=AsyncMock(return_value=self.remote),
            send_document=AsyncMock(),
        )
        self.client = TestClient(
            TestServer(
                create_web_app(
                    self.repo, TOKEN, "https://test.example", telegram_bot=self.bot
                )
            )
        )
        await self.client.start_server()
        self.group = f"/api/groups/{self.gid}"
        self.expense = f"{self.group}/expenses/{self.eid}"

    async def asyncTearDown(self):
        await self.client.close()
        self.repo.close()

    async def request(self, method, path, uid=1, **kwargs):
        headers = {"Authorization": "tma " + signed(uid), **kwargs.pop("headers", {})}
        return await self.client.request(method, path, headers=headers, **kwargs)

    async def upload(self, uid=1, revision=None, data=PHOTO):
        revision = self.revision if revision is None else revision
        return await self.request(
            "POST",
            self.expense + f"/receipt?revision={revision}",
            uid,
            data=data,
            headers={"Content-Type": "image/jpeg"},
        )

    async def test_photo_is_sent_as_bytes_and_only_telegram_id_is_stored(self):
        self.assertEqual((await self.upload()).status, 200)
        self.assertEqual(self.bot.send_photo.call_args.kwargs["chat_id"], 1)
        self.assertEqual(self.bot.send_photo.call_args.kwargs["photo"], PHOTO)
        self.assertEqual(
            self.repo.get_expense(self.eid, self.gid)["receipt"], "telegram-photo-id"
        )
        card = await (await self.request("GET", self.expense, uid=2)).json()
        self.assertTrue(card["has_receipt"])
        self.assertNotIn("receipt", card)
        response = await self.request("GET", self.expense + "/receipt", uid=2)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), PHOTO)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn(TOKEN, str(response.headers))
        self.assertEqual(
            (
                await self.request(
                    "DELETE", self.expense + f"/receipt?revision={self.revision + 1}"
                )
            ).status,
            200,
        )
        self.assertFalse(self.repo.get_expense(self.eid, self.gid)["receipt"])

    async def test_receipt_permissions_size_type_and_revision_are_checked_before_sending(
        self,
    ):
        for uid in (2, 3):
            self.assertEqual((await self.upload(uid=uid)).status, 403)
        self.assertEqual(
            (await self.request("GET", self.expense + "/receipt", uid=3)).status, 403
        )
        self.assertEqual((await self.upload(revision=5)).status, 400)
        self.assertEqual(
            (await self.upload(data=b"<svg>not a photo</svg>")).status, 415
        )
        self.assertEqual(
            (await self.upload(data=b"\xff\xd8\xff" + b"x" * RECEIPT_LIMIT)).status, 413
        )
        self.bot.send_photo.assert_not_awaited()

    async def test_upload_race_does_not_overwrite_a_newer_receipt_or_revision(self):
        async def changed(**kwargs):
            self.repo.set_receipt(self.eid, self.gid, "newer-photo", actor=1)
            return SimpleNamespace(photo=[SimpleNamespace(file_id="late-photo")])

        self.bot.send_photo.side_effect = changed
        response = await self.upload()
        self.assertEqual(response.status, 400)
        self.assertIn("уже изменилась", (await response.json())["error"])
        self.assertEqual(
            self.repo.get_expense(self.eid, self.gid)["receipt"], "newer-photo"
        )

    async def test_telegram_errors_never_expose_download_urls_or_tokens(self):
        self.bot.send_photo.side_effect = BadRequest(
            f"https://api.telegram.org/file/bot{TOKEN}/secret"
        )
        response = await self.upload()
        self.assertEqual(response.status, 502)
        self.assertNotIn(TOKEN, await response.text())
        self.assertFalse(self.repo.get_expense(self.eid, self.gid)["receipt"])

    async def test_history_deleted_listing_and_author_only_restore(self):
        self.repo.update_expense(
            self.eid, self.gid, 1, "Dinner edited", 20000, {1: 15000, 2: 5000}, actor=1
        )
        self.repo.set_receipt(self.eid, self.gid, "telegram-photo-id", actor=1)
        self.assertEqual((await self.request("DELETE", self.expense)).status, 200)
        self.assertEqual(self.repo.member_balance(self.gid, 2), 0)
        listing = await (
            await self.request("GET", self.group + "/expenses?deleted=1", uid=2)
        ).json()
        self.assertEqual((listing["total"], listing["sum_cents"]), (1, 20000))
        card = await (await self.request("GET", self.expense)).json()
        self.assertTrue(card["deleted"])
        self.assertTrue(card["can_restore"])
        self.assertFalse(card["can_edit"])
        response = await self.request("GET", self.expense + "/history", uid=2)
        text = await response.text()
        self.assertNotIn("telegram-photo-id", text)
        history = json.loads(text)
        self.assertEqual(
            [e["action"] for e in history["history"]],
            ["delete", "receipt", "edit", "create"],
        )
        edit = history["history"][2]
        self.assertEqual(edit["before"]["amount_cents"], 10000)
        self.assertEqual(edit["after"]["shares"], {"1": 15000, "2": 5000})
        self.assertIn("1", history["names"])
        self.assertEqual(
            (await self.request("GET", self.expense + "/history", uid=3)).status, 403
        )
        self.assertEqual(
            (
                await self.request("POST", self.expense + "/restore", uid=2, json={})
            ).status,
            403,
        )
        self.assertEqual(
            (await self.request("POST", self.expense + "/restore", json={})).status, 200
        )
        self.assertEqual(self.repo.member_balance(self.gid, 2), -5000)
        self.assertEqual(
            self.repo.get_expense(self.eid, self.gid)["receipt"], "telegram-photo-id"
        )

    async def test_history_paginates_without_repeating_events(self):
        for i in range(23):
            self.repo.set_receipt(self.eid, self.gid, f"photo-{i}", actor=1)
        first = await (await self.request("GET", self.expense + "/history")).json()
        second = await (
            await self.request("GET", self.expense + "/history?offset=20")
        ).json()
        self.assertTrue(first["more"])
        self.assertFalse(second["more"])
        self.assertEqual(len(first["history"]) + len(second["history"]), 24)
        self.assertFalse(
            {e["id"] for e in first["history"]} & {e["id"] for e in second["history"]}
        )

    async def test_export_download_and_delivery_require_membership(self):
        response = await self.request("GET", self.group + "/export", uid=2)
        self.assertEqual(response.status, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        with zipfile.ZipFile(BytesIO(await response.read())) as book:
            self.assertIn("xl/workbook.xml", book.namelist())
            self.assertIn("Dinner", book.read("xl/worksheets/sheet1.xml").decode())
        self.assertEqual(
            (await self.request("POST", self.group + "/export", uid=2, json={})).status,
            200,
        )
        self.assertEqual(self.bot.send_document.call_args.kwargs["chat_id"], 2)
        self.assertIsInstance(
            self.bot.send_document.call_args.kwargs["document"], bytes
        )
        for method in ("GET", "POST"):
            self.assertEqual(
                (await self.request(method, self.group + "/export", uid=3)).status, 403
            )

    async def test_offline_expense_is_not_reinterpreted_after_group_currency_changes(
        self,
    ):
        gid, _ = self.repo.create_group("Empty", 1, "RUB")
        self.repo.set_group_currency(gid, "USD")
        payload = {
            "amount_cents": 100,
            "description": "Queued",
            "payer": 1,
            "participants": [1],
            "operation_id": "queue-old-currency-123",
            "group_currency": "RUB",
        }
        response = await self.request(
            "POST", f"/api/groups/{gid}/expenses", json=payload
        )
        self.assertEqual(response.status, 400)
        self.assertIn("Валюта группы изменилась", (await response.json())["error"])
        self.assertEqual(self.repo.count_group_expenses(gid), 0)

    async def test_settings_rejection_does_not_partially_rename_group(self):
        response = await self.request(
            "POST",
            self.group + "/settings",
            json={"title": "New title", "currency": "USD"},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.repo.get_group_title(self.gid), "Trip")
        self.assertEqual(self.repo.group_currency(self.gid), "RUB")

    async def test_lost_response_is_reconciled_without_duplicating_or_ignoring_edits(
        self,
    ):
        payload = {
            "amount_cents": 100,
            "description": "Queued",
            "payer": 1,
            "participants": [1, 2],
            "operation_id": "queue-operation-12345",
        }
        path = self.group + "/expenses"
        first = await (await self.request("POST", path, json=payload)).json()
        self.assertEqual(
            await (await self.request("POST", path, json=payload)).json(), first
        )
        response = await self.request(
            "POST", path, json={**payload, "description": "Edited offline"}
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(self.repo.count_group_expenses(self.gid), 2)
        lookup = self.group + "/operations/" + payload["operation_id"]
        self.assertEqual(
            (await (await self.request("GET", lookup)).json())["expense"]["id"],
            first["id"],
        )
        self.assertIsNone(
            (await (await self.request("GET", lookup, uid=2)).json())["expense"]
        )
        self.assertEqual((await self.request("GET", lookup, uid=3)).status, 403)


if __name__ == "__main__":
    unittest.main()
