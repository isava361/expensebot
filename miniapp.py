"""Loopback-only Mini App HTTP server; all ledger access uses signed Telegram users."""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web

from core import MAX_AMOUNT_CENTS, KNOWN_CURRENCIES, normalize_currency

STATIC = Path(__file__).with_name("web")
logger = logging.getLogger(__name__)


def validate_init_data(raw, token, max_age=3600, now=None):
    """Telegram HMAC flow: retain every decoded field except hash, including signature."""
    try:
        if not isinstance(raw, str) or not raw or len(raw) > 16384:
            raise ValueError()
        pairs = parse_qsl(
            raw,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=32,
        )
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError()
        received = data.pop("hash")
        if not re.fullmatch(r"[0-9a-f]{64}", received):
            raise ValueError()
        secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
        check = "\n".join(f"{key}={data[key]}" for key in sorted(data))
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            raise ValueError()
        age = (time.time() if now is None else now) - int(data["auth_date"])
        if not -30 <= age <= max_age:
            raise ValueError()
        user = json.loads(data["user"])
        if (
            not isinstance(user, dict)
            or type(user.get("id")) is not int
            or not 0 < user["id"] < 2**52
        ):
            raise ValueError()
        name = " ".join(user.get(k, "") for k in ("first_name", "last_name")).strip()
        return {"id": user["id"], "name": name[:200] or str(user["id"])}
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise ValueError(
            "Откройте приложение заново из меню бота в Telegram."
        ) from None


def validate_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("MINIAPP_URL must be an HTTPS URL at the subdomain root")
    return f"https://{parsed.netloc}"


def integer(value, minimum=1, maximum=MAX_AMOUNT_CENTS):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Некорректное число.")
    return value


def string(value, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("Заполните текстовое поле корректно.")
    return value.strip()


def dispatch(repo, user, method, path, body, query):
    """Run under one repository lock, including authorization and subsequent reads/writes."""
    uid = user["id"]
    with repo._lock:
        repo.upsert_user(uid, user["name"])
        if path == "/api/me" and method == "GET":
            return {"user": user, "currencies": sorted(KNOWN_CURRENCIES)}
        if path == "/api/groups":
            if method == "GET":
                return {
                    "groups": [
                        dict(
                            g,
                            currency=repo.group_currency(g["id"]),
                            balance=repo.member_balance(g["id"], uid),
                        )
                        for g in repo.list_user_groups(uid)
                    ]
                }
            if method == "POST":
                title = string(body.get("title"), 100)
                currency = normalize_currency(string(body.get("currency"), 3))
                if not currency:
                    raise ValueError("Неизвестная валюта.")
                gid, _ = repo.create_group(title, uid, currency)
                return {"id": gid}
        if path == "/api/join" and method == "POST":
            gid, _ = repo.join_by_code(string(body.get("code"), 64), uid)
            return {"id": gid}
        if path == "/api/debts" and method == "GET":
            debts = repo.compute_user_debts(uid)
            pending = repo.list_pending_settlements(uid)
            ids = set(debts)
            for p in pending:
                ids.update((p["from"], p["to"]))
            return {
                "debts": debts,
                "pending": pending,
                "names": repo.names_for(ids),
                "groups": repo.group_titles(
                    g["id"] for g in repo.list_user_groups(uid)
                ),
            }
        if path == "/api/payments" and method == "POST":
            batch = repo.request_settlement(
                uid,
                integer(body.get("other"), maximum=2**52 - 1),
                string(body.get("currency"), 3),
                integer(body.get("amount_cents"), minimum=0),
            )
            if not batch:
                raise ValueError("Долг изменился или платёж уже ожидает подтверждения.")
            return {"batch": batch}
        match = re.fullmatch(r"/api/payments/([A-Za-z0-9_-]+)/(confirm|reject)", path)
        if match and method == "POST":
            action = (
                repo.confirm_settlement
                if match[2] == "confirm"
                else repo.reject_settlement
            )
            if not action(match[1], uid):
                raise web.HTTPForbidden()
            return {"ok": True}
        match = re.fullmatch(
            r"/api/groups/([0-9]+)(?:/(expenses)(?:/([0-9]+))?)?", path
        )
        if not match:
            raise web.HTTPNotFound()
        gid = int(match[1])
        if not repo.is_group_member(gid, uid):
            raise web.HTTPForbidden()
        if not match[2] and method == "GET":
            return {
                "id": gid,
                "title": repo.get_group_title(gid),
                "currency": repo.group_currency(gid),
                "members": repo.list_members(gid),
                "invite_code": repo.get_invite_code(gid),
                "balances": repo.member_balances(gid),
            }
        if match[2] and not match[3]:
            if method == "GET":
                offset = integer(int(query.get("offset", "0")), 0, 1000000)
                expenses = repo.list_group_expenses(gid, 30, offset)
                for expense in expenses:
                    expense.pop("receipt", None)
                return {"expenses": expenses, "total": repo.count_group_expenses(gid)}
            if method == "POST":
                amount = integer(body.get("amount_cents"))
                payer = integer(body.get("payer"), maximum=2**52 - 1)
                desc = string(body.get("description"), 500)
                participants = body.get("participants")
                if (
                    not isinstance(participants, list)
                    or not 1 <= len(participants) <= 500
                ):
                    raise ValueError("Выберите участников.")
                participants = [integer(p, maximum=2**52 - 1) for p in participants]
                if len(set(participants)) != len(participants):
                    raise ValueError("Повторяющиеся участники.")
                custom = body.get("shares")
                if custom is None:
                    part, rest = divmod(amount, len(participants))
                    shares = {p: part + (i < rest) for i, p in enumerate(participants)}
                else:
                    if not isinstance(custom, list) or len(custom) != len(participants):
                        raise ValueError("Укажите долю каждого участника.")
                    shares = dict(zip(participants, [integer(v, 0) for v in custom]))
                operation = string(body.get("operation_id"), 64)
                if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", operation):
                    raise ValueError("Некорректный идентификатор операции.")
                eid = repo.create_expense(
                    gid,
                    uid,
                    payer,
                    desc,
                    amount,
                    shares,
                    operation_id=f"web:{uid}:{operation}",
                )
                return {"id": eid}
        if match[3]:
            eid = int(match[3])
            expense = repo.get_expense(eid, gid)
            if not expense:
                raise web.HTTPNotFound()
            if method == "GET":
                expense.pop("receipt", None)
                return dict(expense, shares=repo.get_expense_shares(eid))
            if method == "DELETE":
                if not repo.can_delete_expense(eid, gid, uid):
                    raise web.HTTPForbidden()
                repo.delete_expense(eid, actor=uid)
                return {"ok": True}
        raise web.HTTPMethodNotAllowed(method, ["GET", "POST"])


def create_web_app(repo, token, url, max_age=3600):
    origin = validate_url(url)
    if not token or not 60 <= max_age <= 86400:
        raise ValueError("BOT_TOKEN and INIT_DATA_MAX_AGE (60..86400) required")

    @web.middleware
    async def security(request, handler):
        try:
            if request.path.startswith("/api/"):
                if request.headers.get("Origin", origin) != origin:
                    raise web.HTTPForbidden()
                auth = request.headers.get("Authorization", "")
                try:
                    if not auth.startswith("tma "):
                        raise ValueError()
                    request["user"] = validate_init_data(auth[4:], token, max_age)
                except ValueError:
                    raise web.HTTPUnauthorized() from None
            response = await handler(request)
        except web.HTTPException as error:
            messages = {
                401: "Откройте приложение заново из меню бота в Telegram.",
                403: "Нет доступа к этому действию.",
                404: "Запись не найдена.",
            }
            response = web.json_response(
                {"error": messages.get(error.status, "Некорректный запрос.")},
                status=error.status,
            )
        except (ValueError, TypeError, KeyError):
            response = web.json_response(
                {
                    "error": "Проверьте введённые данные. Возможно, запись уже изменилась."
                },
                status=400,
            )
        except Exception:
            logger.exception("Mini App request failed")
            response = web.json_response(
                {"error": "Не удалось выполнить действие. Попробуйте ещё раз."},
                status=500,
            )
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org",
            }
        )
        return response

    async def api(request):
        body = {}
        if request.method == "POST":
            if request.content_type != "application/json":
                raise web.HTTPUnsupportedMediaType()
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError()
        result = await asyncio.to_thread(
            dispatch,
            repo,
            request["user"],
            request.method,
            request.path,
            body,
            dict(request.query),
        )
        return web.json_response(result)

    async def health(request):
        return web.json_response({"status": "ok", "app": "expensebot"})

    async def static(request):
        name = request.match_info.get("name", "index.html")
        if name not in {"index.html", "app.js", "app.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    app = web.Application(middlewares=[security], client_max_size=32768)
    app.router.add_get("/healthz", health)
    app.router.add_route("*", "/api/{path:.*}", api)
    app.router.add_get("/", static)
    app.router.add_get("/{name}", static)
    return app


async def start_web(repo, token, url, port, max_age=3600):
    integer(port, 1024, 65535)
    runner = web.AppRunner(create_web_app(repo, token, url, max_age), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", port).start()
    except BaseException:
        await runner.cleanup()
        raise
    logger.info("Mini App listening on 127.0.0.1:%s", port)
    return runner
