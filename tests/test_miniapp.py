import hashlib
import hmac
import json
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

from miniapp import create_web_app, validate_init_data, validate_url
from repository import Repo

TOKEN = "123456:test-token-for-unit-tests"
URL = "https://expense.ivansavelyev.ru:8443/"
AGENT_KEY = "agent-key-for-unit-tests-0123456789abcdef"
FORBIDDEN = "Нет доступа к этому действию."


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

    async def test_agent_key_is_refused_while_the_mode_is_off(self):
        for auth in ("agent " + AGENT_KEY, "agent ", "agent"):
            response = await self.client.get("/api/me", headers={"Authorization": auth})
            self.assertEqual(response.status, 401, auth)

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


class AgentSettingsTest(unittest.TestCase):
    def test_half_set_or_malformed_settings_stop_the_start(self):
        repo = Repo(":memory:")
        self.addCleanup(repo.close)
        for key, user_id in [
            (AGENT_KEY, ""),
            ("", "1"),
            (AGENT_KEY[:31], "1"),
            (AGENT_KEY[:20] + " " + AGENT_KEY[20:], "1"),
            (AGENT_KEY + "é", "1"),
            (AGENT_KEY, "ivan"),
            (AGENT_KEY, "0"),
            (AGENT_KEY, "-5"),
            (AGENT_KEY, "1.5"),
            (AGENT_KEY, str(2**52)),
        ]:
            with self.subTest(key=len(key), user_id=user_id):
                with self.assertRaises(ValueError) as caught:
                    create_web_app(
                        repo, TOKEN, URL, agent_key=key, agent_user_id=user_id
                    )
                if key:
                    self.assertNotIn(key, str(caught.exception))
        create_web_app(repo, TOKEN, URL, agent_key="", agent_user_id="")
        create_web_app(repo, TOKEN, URL, agent_key=AGENT_KEY, agent_user_id=" 1 ")


class AgentAccessTest(unittest.IsolatedAsyncioTestCase):
    """The agent key acts for user 1 only, and not in everything."""

    async def asyncSetUp(self):
        self.repo = Repo(":memory:")
        for uid in (1, 2, 3):
            self.repo.upsert_user(uid, f"User {uid}")
        self.gid, code = self.repo.create_group("Trip", 1)
        self.repo.join_by_code(code, 2)
        # Cleanups run last in, first out: the clients close before the ledger.
        self.addCleanup(self.repo.close)
        self.client = await self.serve("1")

    async def serve(self, agent_user_id):
        client = TestClient(
            TestServer(
                create_web_app(
                    self.repo,
                    TOKEN,
                    URL,
                    agent_key=AGENT_KEY,
                    agent_user_id=agent_user_id,
                )
            )
        )
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def agent(self, method, path, key=AGENT_KEY, **kwargs):
        return await self.client.request(
            method, path, headers={"Authorization": "agent " + key}, **kwargs
        )

    async def human(self, method, path, uid=1, **kwargs):
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

    async def test_key_acts_as_its_user_and_keeps_the_stored_name(self):
        response = await self.agent("GET", "/api/me")
        self.assertEqual(response.status, 200)
        text = await response.text()
        self.assertEqual(json.loads(text)["user"], {"id": 1, "name": "User 1"})
        self.assertNotIn(AGENT_KEY, text + str(response.headers))
        self.assertEqual(self.repo.user_name(1), "User 1")
        # Telegram stays the source of the name; the agent only reads it.
        await self.human("GET", "/api/me")
        response = await self.agent("GET", "/api/groups")
        self.assertEqual(
            [g["id"] for g in (await response.json())["groups"]], [self.gid]
        )
        self.assertEqual(self.repo.user_name(1), "Иван + & = Тест")
        self.assertEqual(
            (await (await self.agent("GET", "/api/me")).json())["user"]["name"],
            "Иван + & = Тест",
        )

    async def test_wrong_or_malformed_key_is_unauthorized(self):
        for auth in (
            "agent " + AGENT_KEY[:-1] + "x",
            "agent " + AGENT_KEY + "x",
            "agent " + AGENT_KEY[:-1],
            "agent " + "é" * len(AGENT_KEY),
            "agent ",
            "agent",
            "agent" + AGENT_KEY,
            "Agent " + AGENT_KEY,
            "Bearer " + AGENT_KEY,
            "tma " + AGENT_KEY,
            AGENT_KEY,
        ):
            response = await self.client.get("/api/me", headers={"Authorization": auth})
            self.assertEqual(response.status, 401, auth)
            self.assertNotIn(AGENT_KEY, await response.text())

    async def test_foreign_origin_is_refused_with_a_valid_key(self):
        response = await self.client.get(
            "/api/me",
            headers={
                "Authorization": "agent " + AGENT_KEY,
                "Origin": "https://evil.example",
            },
        )
        self.assertEqual(response.status, 403)

    async def test_user_unknown_to_the_ledger_is_refused_and_not_created(self):
        self.client = await self.serve("99")
        response = await self.agent("GET", "/api/me")
        self.assertEqual(response.status, 403)
        self.assertEqual(self.repo.names_for([99]), {99: "99"})
        self.assertIsNone(self.repo.stored_user_name(99))
        self.assertEqual((await self.agent("GET", "/api/me", key="x" * 40)).status, 401)

    async def test_agent_cannot_manage_groups_or_settle_payments(self):
        solo, _ = self.repo.create_group("Solo", 1)
        other, other_code = self.repo.create_group("Other", 2)
        self.repo.create_expense(self.gid, 1, 1, "Dinner", 10000, {2: 10000})
        confirm = self.repo.request_settlement(2, 1, "RUB", 4000)
        cases = [
            ("POST", "/api/groups", {"title": "Home", "currency": "EUR"}),
            ("POST", "/api/join", {"code": other_code}),
            ("POST", f"/api/groups/{solo}/settings", {"title": "Renamed"}),
            ("POST", f"/api/groups/{solo}/settings/5", {"title": "Renamed"}),
            ("POST", f"/api/groups/{solo}/leave", {}),
            ("POST", f"/api/groups/{solo}/leave/5", {}),
            ("POST", f"/api/payments/{confirm}/reject", {}),
            ("POST", f"/api/payments/{confirm}/confirm", {}),
        ]
        for method, path, body in cases:
            response = await self.agent(method, path, json=body)
            self.assertEqual(response.status, 403, path)
            self.assertEqual((await response.json())["error"], FORBIDDEN)
        self.assertEqual(len(self.repo.list_user_groups(1)), 2)
        self.assertEqual(self.repo.get_group_title(solo), "Solo")
        self.assertEqual(self.repo.compute_user_debts(2)[1]["RUB"]["net"], -10000)
        self.assertEqual(len(self.repo.list_pending_settlements(1)), 1)
        # The same user, signed by Telegram, is held to the old rules only.
        for method, path, body in cases:
            if path.endswith(("/5", "/reject")):
                continue
            response = await self.human(method, path, json=body)
            self.assertEqual(response.status, 200, path)
        self.assertEqual(len(self.repo.list_user_groups(1)), 3)
        self.assertTrue(self.repo.is_group_member(other, 1))
        self.assertEqual(self.repo.compute_user_debts(2)[1]["RUB"]["net"], -6000)

        reject = self.repo.request_settlement(2, 1, "RUB", 1000)
        path = f"/api/payments/{reject}/reject"
        self.assertEqual((await self.agent("POST", path, json={})).status, 403)
        self.assertEqual((await self.human("POST", path, json={})).status, 200)

    async def test_agent_cannot_remove_a_member(self):
        path = f"/api/groups/{self.gid}/members/2"
        response = await self.agent("DELETE", path)
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())["error"], FORBIDDEN)
        self.assertTrue(self.repo.is_group_member(self.gid, 2))
        self.assertEqual((await self.human("DELETE", path)).status, 200)
        self.assertFalse(self.repo.is_group_member(self.gid, 2))

    async def test_agent_keeps_the_ledger_as_its_user(self):
        group = f"/api/groups/{self.gid}"
        response = await self.agent("POST", f"{group}/expenses", json=self.expense())
        self.assertEqual(response.status, 200)
        eid = (await response.json())["id"]
        stored = self.repo.get_expense(eid, self.gid)
        self.assertEqual(stored["created_by"], 1)
        path = f"{group}/expenses/{eid}"
        card = await (await self.agent("GET", path)).json()
        self.assertTrue(card["can_edit"])
        response = await self.agent(
            "POST",
            path,
            json=self.expense(amount_cents=30000, revision=stored["revision"]),
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.repo.get_expense(eid, self.gid)["amount_cents"], 30000)
        self.assertEqual((await self.agent("DELETE", path)).status, 200)
        self.assertEqual(
            (await self.agent("POST", f"{path}/restore", json={})).status, 200
        )
        self.assertFalse(self.repo.get_expense(eid, self.gid)["deleted"])
        history = (await (await self.agent("GET", f"{path}/history")).json())["history"]
        self.assertEqual({record["actor"] for record in history}, {1})
        self.assertEqual(len(history), 4)
        for read in ("", "/expenses", "/export"):
            self.assertEqual((await self.agent("GET", group + read)).status, 200, read)
        self.assertEqual((await self.agent("GET", "/api/debts")).status, 200)

        # Somebody else's expense stays theirs, and a claim to have paid is allowed.
        theirs = self.repo.create_expense(self.gid, 2, 2, "Taxi", 80000, {1: 80000})
        self.assertEqual(
            (await self.agent("DELETE", f"{group}/expenses/{theirs}")).status, 403
        )
        response = await self.agent(
            "POST",
            "/api/payments",
            json={"other": 2, "currency": "RUB", "amount_cents": 1000},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.repo.user_name(1), "User 1")


class WebModuleTest(unittest.TestCase):
    """The Mini App's own money arithmetic, checked by node when it is here."""

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_money_module(self):
        suite = Path(__file__).with_name("test_web.js")
        result = subprocess.run(
            [shutil.which("node"), "--test", str(suite)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class MiniAppStaticTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = Repo(":memory:")
        self.client = TestClient(TestServer(create_web_app(self.repo, TOKEN, URL)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.repo.close()

    async def test_modules_are_served_and_nothing_else_is(self):
        for path in ("/", "/app.js", "/app.css", "/lib/money.js", "/expense.js"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200, path)
            self.assertNotIn(TOKEN, await response.text())
        for path in (
            "/../miniapp.py",
            "/lib/../../miniapp.py",
            "/lib/money.js/",
            "/Lib/money.js",
            "/lib/money.py",
            "/.env",
            "/web/app.js",
            "/lib/",
        ):
            self.assertEqual((await self.client.get(path)).status, 404, path)


class MiniAppEditingTest(unittest.IsolatedAsyncioTestCase):
    """Editing, group settings and the currency endpoint the Mini App added."""

    class Rates:
        async def convert(self, amount_cents, orig, base):
            if (orig, base) != ("TRY", "RUB"):
                return None
            return amount_cents * 2, 2.0, 1700000000

    async def asyncSetUp(self):
        self.repo = Repo(":memory:")
        for uid in (1, 2, 3):
            self.repo.upsert_user(uid, f"User {uid}")
        self.gid, code = self.repo.create_group("Trip", 1, "RUB")
        self.repo.join_by_code(code, 2)
        self.client = TestClient(
            TestServer(
                create_web_app(self.repo, TOKEN, URL, rates=self.Rates(), bot="thebot")
            )
        )
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.repo.close()

    async def request(self, method, path, uid=1, **kwargs):
        return await self.client.request(
            method, path, headers={"Authorization": "tma " + signed(uid)}, **kwargs
        )

    async def test_me_carries_the_bot_username_for_invite_links(self):
        response = await self.request("GET", "/api/me")
        self.assertEqual((await response.json())["bot"], "thebot")

    async def test_rate_converts_and_reports_a_missing_pair(self):
        response = await self.request("GET", "/api/rate?from=try&to=rub&amount=1000")
        self.assertEqual(
            {k: v for k, v in (await response.json()).items() if k != "updated"},
            {"converted": 2000, "rate": 2.0},
        )
        response = await self.request("GET", "/api/rate?from=EUR&to=RUB&amount=1000")
        self.assertIsNone((await response.json())["converted"])
        for query in ("from=XXX&to=RUB&amount=1", "from=TRY&to=RUB&amount=0"):
            self.assertEqual(
                (await self.request("GET", f"/api/rate?{query}")).status, 400
            )
        self.assertEqual((await self.client.get("/api/rate")).status, 401)

    async def test_foreign_currency_expense_round_trips(self):
        response = await self.request(
            "POST",
            f"/api/groups/{self.gid}/expenses",
            json={
                "amount_cents": 20000,
                "orig_currency": "try",
                "orig_amount_cents": 10000,
                "description": "Museum",
                "payer": 1,
                "participants": [1, 2],
                "operation_id": "operation-123456789",
            },
        )
        eid = (await response.json())["id"]
        stored = self.repo.get_expense(eid, self.gid)
        self.assertEqual(
            (stored["orig_currency"], stored["orig_amount_cents"]), ("TRY", 10000)
        )
        shown = await (
            await self.request("GET", f"/api/groups/{self.gid}/expenses/{eid}")
        ).json()
        self.assertEqual(shown["orig_currency"], "TRY")
        self.assertTrue(shown["can_edit"])

    async def test_edit_belongs_to_the_author_and_checks_the_revision(self):
        eid = self.repo.create_expense(
            self.gid, 1, 1, "Dinner", 10000, {1: 5000, 2: 5000}
        )
        revision = self.repo.get_expense(eid, self.gid)["revision"]
        body = {
            "amount_cents": 30000,
            "description": "Dinner for three",
            "payer": 2,
            "participants": [1, 2],
            "shares": [10000, 20000],
            "revision": revision,
        }
        path = f"/api/groups/{self.gid}/expenses/{eid}"
        self.assertEqual(
            (await self.request("POST", path, uid=2, json=body)).status, 403
        )
        self.assertEqual((await self.request("POST", path, json=body)).status, 200)
        updated = self.repo.get_expense(eid, self.gid)
        self.assertEqual(updated["amount_cents"], 30000)
        self.assertEqual(updated["payer"], 2)
        self.assertEqual(self.repo.get_expense_shares(eid), {1: 10000, 2: 20000})
        # The stale revision is the one the client already saw.
        self.assertEqual((await self.request("POST", path, json=body)).status, 400)
        for broken in ({"shares": [1, 2]}, {"participants": [1, 3]}, {"payer": 3}):
            self.assertEqual(
                (
                    await self.request(
                        "POST", path, json={**body, **broken, "revision": revision + 1}
                    )
                ).status,
                400,
            )
        self.assertEqual(self.repo.get_expense(eid, self.gid)["amount_cents"], 30000)

    async def test_settings_rename_and_currency_are_owner_only(self):
        path = f"/api/groups/{self.gid}/settings"
        self.assertEqual(
            (await self.request("POST", path, uid=2, json={"title": "Hijack"})).status,
            403,
        )
        self.assertEqual(
            (await self.request("POST", path, json={"title": "Trip 2026"})).status, 200
        )
        self.assertEqual(self.repo.get_group_title(self.gid), "Trip 2026")
        self.assertEqual(
            (await self.request("POST", path, json={"currency": "EUR"})).status, 200
        )
        self.assertEqual(self.repo.group_currency(self.gid), "EUR")
        # Once money is recorded the base currency is frozen, but resending
        # the current one must stay a no-op rather than an error.
        self.repo.create_expense(self.gid, 1, 1, "Dinner", 10000, {1: 10000})
        self.assertEqual(
            (await self.request("POST", path, json={"currency": "EUR"})).status, 200
        )
        self.assertEqual(
            (await self.request("POST", path, json={"currency": "USD"})).status, 400
        )
        self.assertEqual(self.repo.group_currency(self.gid), "EUR")

    async def test_removing_a_member_and_leaving_respect_open_debts(self):
        self.assertEqual(
            (
                await self.request("DELETE", f"/api/groups/{self.gid}/members/2", uid=2)
            ).status,
            403,
        )
        self.repo.create_expense(self.gid, 1, 1, "Dinner", 10000, {1: 5000, 2: 5000})
        self.assertEqual(
            (await self.request("DELETE", f"/api/groups/{self.gid}/members/2")).status,
            400,
        )
        self.assertEqual(
            (
                await self.request("POST", f"/api/groups/{self.gid}/leave", json={})
            ).status,
            400,
        )
        self.repo.delete_expense(
            self.repo.list_group_expenses(self.gid, 1, 0)[0]["id"], actor=1
        )
        self.assertEqual(
            (await self.request("DELETE", f"/api/groups/{self.gid}/members/2")).status,
            200,
        )
        self.assertEqual([m["id"] for m in self.repo.list_members(self.gid)], [1])
        response = await self.request("POST", f"/api/groups/{self.gid}/leave", json={})
        self.assertTrue((await response.json())["deleted"])
        self.assertEqual(self.repo.list_user_groups(1), [])

    async def test_expense_list_searches_filters_and_totals(self):
        self.repo.create_expense(self.gid, 1, 1, "Ужин у моря", 42000, {1: 42000})
        self.repo.create_expense(self.gid, 1, 2, "УЖИН в отеле", 10000, {2: 10000})
        self.repo.create_expense(self.gid, 1, 2, "Такси", 5000, {2: 5000})
        path = f"/api/groups/{self.gid}/expenses"

        async def listing(query=""):
            return await (await self.request("GET", path + query)).json()

        everything = await listing()
        self.assertEqual((everything["total"], everything["sum_cents"]), (3, 57000))
        # Case folding has to reach Cyrillic, which SQLite does not fold itself.
        found = await listing("?q=ужин")
        self.assertEqual((found["total"], found["sum_cents"]), (2, 52000))
        self.assertEqual(
            {e["desc"] for e in found["expenses"]}, {"Ужин у моря", "УЖИН в отеле"}
        )
        by_payer = await listing("?payer=2")
        self.assertEqual((by_payer["total"], by_payer["sum_cents"]), (2, 15000))
        both = await listing("?q=ужин&payer=2")
        self.assertEqual((both["total"], both["sum_cents"]), (1, 10000))
        # A wildcard is text, not syntax.
        self.assertEqual((await listing("?q=%"))["total"], 0)
        self.assertEqual((await listing("?q=нет+такого"))["total"], 0)
        self.assertEqual((await self.request("GET", path + "?payer=3")).status, 400)

    async def test_group_payload_lists_who_owes_whom_inside_it(self):
        self.repo.create_expense(self.gid, 1, 1, "Dinner", 10000, {1: 5000, 2: 5000})
        payload = await (await self.request("GET", f"/api/groups/{self.gid}")).json()
        self.assertEqual(
            payload["transfers"], [{"from": 2, "to": 1, "amount_cents": 5000}]
        )
        self.repo.create_expense(self.gid, 2, 2, "Taxi", 10000, {1: 5000, 2: 5000})
        payload = await (await self.request("GET", f"/api/groups/{self.gid}")).json()
        self.assertEqual(payload["transfers"], [])

    async def test_group_payload_states_who_may_change_what(self):
        payload = await (await self.request("GET", f"/api/groups/{self.gid}")).json()
        self.assertTrue(payload["is_owner"])
        self.assertTrue(payload["can_change_currency"])
        self.assertEqual(
            [(m["id"], m["role"]) for m in payload["members"]],
            [(1, "owner"), (2, "member")],
        )
        self.repo.create_expense(self.gid, 1, 1, "Dinner", 10000, {1: 10000})
        payload = await (
            await self.request("GET", f"/api/groups/{self.gid}", uid=2)
        ).json()
        self.assertFalse(payload["is_owner"])
        self.assertFalse(payload["can_change_currency"])


if __name__ == "__main__":
    unittest.main()
