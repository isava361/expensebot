import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

from miniapp import create_web_app, validate_init_data, validate_url
from repository import Repo

TOKEN = "123456:test-token-for-unit-tests"
URL = "https://expense.ivansavelyev.ru:8443/"


def signed(uid=1, token=TOKEN, **changes):
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "test-query",
        "user": json.dumps(
            {"id": uid, "first_name": "Иван + & =", "last_name": "Тест"},
            ensure_ascii=False,
        ),
    }
    fields.update(changes)
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class InitDataTest(unittest.TestCase):
    def test_unicode_roundtrip_and_signature_field(self):
        self.assertEqual(
            validate_init_data(signed(signature="new-telegram-field"), TOKEN)["name"],
            "Иван + & = Тест",
        )

    def test_tampering_other_bot_expiry_future_duplicates_and_bad_user(self):
        cases = [
            "",
            signed() + "&auth_date=1",
            signed().replace("test-query", "forged"),
            signed(token="different-bot"),
            signed(auth_date="10"),
            signed(auth_date=str(int(time.time()) + 120)),
            signed(auth_date="invalid"),
            signed(user="[]"),
            signed(user='{"id":true}'),
            signed(user='{"id":0}'),
            signed(user='{"id":1,"first_name":null}'),
            "user=%FF&hash=a",
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_init_data(raw, TOKEN)

    def test_age_boundary(self):
        raw = signed(auth_date="100000")
        self.assertEqual(validate_init_data(raw, TOKEN, now=103600)["id"], 1)
        with self.assertRaises(ValueError):
            validate_init_data(raw, TOKEN, now=103601)

    def test_https_at_root(self):
        self.assertEqual(validate_url(URL), URL.rstrip("/"))
        for url in [
            "http://example.com",
            "https://example.com/miniapp/",
            "https://u:p@example.com",
            "https://example.com/?token=secret",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)


class MiniAppTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = Repo(":memory:")
        for uid in (1, 2, 3):
            self.repo.upsert_user(uid, f"User {uid}")
        self.gid, code = self.repo.create_group("Trip", 1)
        self.repo.join_by_code(code, 2)
        self.client = TestClient(TestServer(create_web_app(self.repo, TOKEN, URL)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.repo.close()

    async def request(self, method, path, uid=1, **kwargs):
        return await self.client.request(
            method, path, headers={"Authorization": "tma " + signed(uid)}, **kwargs
        )

    def expense(self, **changes):
        body = {
            "amount_cents": 10001,
            "description": "Dinner",
            "payer": 1,
            "participants": [1, 2],
            "operation_id": "operation-123456789",
        }
        body.update(changes)
        return body

    async def test_public_root_health_and_no_secrets(self):
        for path in ("/", "/app.js", "/app.css", "/healthz"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
            self.assertNotIn(TOKEN, await response.text())
        for path in ("/.env", "/main.py", "/data.db"):
            self.assertEqual((await self.client.get(path)).status, 404)
        response = await self.client.get("/api/me")
        self.assertEqual(response.status, 401)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_every_api_requires_auth_and_origin(self):
        for method, path in [
            ("GET", "/api/groups"),
            ("POST", "/api/groups"),
            ("GET", "/api/debts"),
            ("DELETE", f"/api/groups/{self.gid}/expenses/1"),
        ]:
            self.assertEqual((await self.client.request(method, path)).status, 401)
        response = await self.client.get(
            "/api/me",
            headers={
                "Authorization": "tma " + signed(),
                "Origin": "https://evil.example",
            },
        )
        self.assertEqual(response.status, 403)
        for raw in (signed(auth_date="1"), signed(token="wrong")):
            response = await self.client.get(
                "/api/me", headers={"Authorization": "tma " + raw}
            )
            self.assertEqual(response.status, 401)

    async def test_membership_and_identity_cannot_be_spoofed(self):
        for suffix in ("", "/expenses", "/expenses/1"):
            self.assertEqual(
                (
                    await self.request("GET", f"/api/groups/{self.gid}{suffix}", uid=3)
                ).status,
                403,
            )
        response = await self.request(
            "POST",
            f"/api/groups/{self.gid}/expenses",
            uid=3,
            json=self.expense(user_id=1),
        )
        self.assertEqual(response.status, 403)
        response = await self.request("GET", "/api/groups", uid=3)
        self.assertEqual((await response.json())["groups"], [])

    async def test_equal_split_idempotency_and_author_permissions(self):
        path = f"/api/groups/{self.gid}/expenses"
        first = await self.request("POST", path, json=self.expense(created_by=2))
        self.assertEqual(first.status, 200)
        eid = (await first.json())["id"]
        second = await self.request("POST", path, json=self.expense())
        self.assertEqual((await second.json())["id"], eid)
        self.assertEqual(self.repo.count_group_expenses(self.gid), 1)
        self.assertEqual(self.repo.get_expense_shares(eid), {1: 5001, 2: 5000})
        self.assertEqual(self.repo.get_expense(eid, self.gid)["created_by"], 1)
        self.assertEqual(
            (await self.request("DELETE", f"{path}/{eid}", uid=2)).status, 403
        )
        self.assertEqual((await self.request("DELETE", f"{path}/{eid}")).status, 200)
        self.assertEqual(self.repo.count_group_expenses(self.gid), 0)
        self.assertTrue(
            self.repo.get_expense(eid, self.gid, include_deleted=True)["deleted"]
        )

    async def test_invalid_amounts_shares_and_participants_write_nothing(self):
        for changes in (
            {"amount_cents": True},
            {"amount_cents": 1.5},
            {"amount_cents": -10},
            {"amount_cents": 1000000001},
            {"shares": [1, 2]},
            {"payer": 3},
            {"participants": [1, 3]},
            {"participants": [1, 1]},
            {"participants": []},
            {"shares": [10002, -1]},
            {"operation_id": ""},
        ):
            with self.subTest(changes=changes):
                response = await self.request(
                    "POST",
                    f"/api/groups/{self.gid}/expenses",
                    json=self.expense(**changes),
                )
                self.assertEqual(response.status, 400)
        self.assertEqual(self.repo.count_group_expenses(self.gid), 0)

    async def test_payment_requires_recipient_confirmation(self):
        self.repo.create_expense(self.gid, 1, 1, "Dinner", 10000, {2: 10000})
        response = await self.request(
            "POST",
            "/api/payments",
            uid=2,
            json={"other": 1, "currency": "RUB", "amount_cents": 4000},
        )
        self.assertEqual(response.status, 200)
        batch = (await response.json())["batch"]
        self.assertEqual(self.repo.compute_user_debts(2)[1]["RUB"]["net"], -10000)
        for uid in (2, 3):
            self.assertEqual(
                (
                    await self.request(
                        "POST", f"/api/payments/{batch}/confirm", uid=uid, json={}
                    )
                ).status,
                403,
            )
        self.assertEqual(
            (
                await self.request("POST", f"/api/payments/{batch}/confirm", json={})
            ).status,
            200,
        )
        self.assertEqual(self.repo.compute_user_debts(2)[1]["RUB"]["net"], -6000)

    async def test_group_creation_join_and_input_limits(self):
        response = await self.request(
            "POST", "/api/groups", json={"title": "Home", "currency": "EUR"}
        )
        gid = (await response.json())["id"]
        response = await self.request(
            "POST", "/api/join", uid=3, json={"code": self.repo.get_invite_code(gid)}
        )
        self.assertEqual((await response.json())["id"], gid)
        response = await self.request("GET", f"/api/groups/{gid}", uid=3)
        self.assertEqual((await response.json())["currency"], "EUR")
        for body in (
            {"title": "X", "currency": "XYZ"},
            {"title": "", "currency": "RUB"},
            [],
        ):
            self.assertEqual(
                (await self.request("POST", "/api/groups", json=body)).status, 400
            )
        response = await self.client.post(
            "/api/groups",
            headers={
                "Authorization": "tma " + signed(),
                "Content-Type": "application/json",
            },
            data='{"title":"' + "x" * 40000 + '"}',
        )
        self.assertEqual(response.status, 413)


if __name__ == "__main__":
    unittest.main()
