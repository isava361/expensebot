"""Loopback-only Mini App HTTP server; all ledger access uses signed Telegram users."""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, quote

from aiohttp import web
from telegram.error import TelegramError

from core import MAX_AMOUNT_CENTS, KNOWN_CURRENCIES, normalize_currency
from workbook import build_group_workbook, export_filename

STATIC = Path(__file__).with_name("web")
# One or more lowercase path segments ending in a web extension: enough for
# module folders, and no way to name a dotfile or climb out of web/.
STATIC_NAME = re.compile(r"[a-z0-9_-]+(?:/[a-z0-9_-]+)*\.(?:html|js|css|map)")
logger = logging.getLogger(__name__)
RECEIPT_LIMIT = 10 * 1024 * 1024


class InputError(ValueError):
    """A safe, actionable message intended for the Mini App user."""


def public_expense(expense):
    result = dict(expense)
    result["has_receipt"] = bool(result.pop("receipt", ""))
    return result


def expense_access(repo, uid, gid, eid, edit=False, revision=None):
    with repo._lock:
        if not repo.is_group_member(gid, uid):
            raise web.HTTPForbidden()
        expense = repo.get_expense(eid, gid, include_deleted=True)
        if not expense:
            raise web.HTTPNotFound()
        if edit and (expense["created_by"] != uid or expense["deleted"]):
            raise web.HTTPForbidden()
        if revision is not None and expense["revision"] != revision:
            raise InputError("Трата уже изменена. Откройте её заново.")
        return expense


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


LEAVE_ERRORS = {
    "not_member": "Вы уже не состоите в этой группе.",
    "has_debt": "Сначала рассчитайтесь: в группе остаётся ваш непогашенный долг.",
}
REMOVE_ERRORS = {
    "not_member": "Этот человек уже не в группе.",
    "has_debt": "У участника остаётся непогашенный долг в группе.",
    "owner": "Владельца группы убрать нельзя — он может только выйти сам.",
}


def expense_fields(body):
    """One shape of expense payload, validated the same way for create and edit."""
    amount = integer(body.get("amount_cents"))
    payer = integer(body.get("payer"), maximum=2**52 - 1)
    desc = string(body.get("description"), 500)
    participants = body.get("participants")
    if not isinstance(participants, list) or not 1 <= len(participants) <= 500:
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
    orig_currency, orig_amount = "", 0
    if body.get("orig_currency"):
        orig_currency = normalize_currency(string(body["orig_currency"], 3))
        if not orig_currency:
            raise ValueError("Неизвестная валюта.")
        orig_amount = integer(body.get("orig_amount_cents"))
    return payer, desc, amount, shares, orig_currency, orig_amount


def dispatch(repo, user, method, path, body, query, bot=""):
    """Run under one repository lock, including authorization and subsequent reads/writes."""
    uid = user["id"]
    with repo._lock:
        repo.upsert_user(uid, user["name"])
        if path == "/api/me" and method == "GET":
            return {
                "user": user,
                "currencies": sorted(KNOWN_CURRENCIES),
                "bot": bot,
            }
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
        operation_match = re.fullmatch(
            r"/api/groups/([0-9]+)/operations/([A-Za-z0-9_-]{16,64})", path
        )
        if operation_match and method == "GET":
            gid = int(operation_match[1])
            if not repo.is_group_member(gid, uid):
                raise web.HTTPForbidden()
            expense = repo.expense_by_operation(gid, uid, operation_match[2])
            return {"expense": public_expense(expense) if expense else None}
        match = re.fullmatch(
            r"/api/groups/([0-9]+)"
            r"(?:/(expenses|settings|leave|members)(?:/([0-9]+))?)?"
            r"(?:/(history|restore))?",
            path,
        )
        if not match:
            raise web.HTTPNotFound()
        gid = int(match[1])
        if not repo.is_group_member(gid, uid):
            raise web.HTTPForbidden()
        section, item, action = match[2], match[3], match[4]
        if action and (section != "expenses" or not item):
            raise web.HTTPNotFound()
        if not section and method == "GET":
            return {
                "id": gid,
                "title": repo.get_group_title(gid),
                "currency": repo.group_currency(gid),
                "members": repo.list_members_detailed(gid),
                "invite_code": repo.get_invite_code(gid),
                "balances": repo.member_balances(gid),
                "is_owner": repo.is_group_owner(gid, uid),
                "can_change_currency": repo.can_change_currency(gid),
                # Who owes whom inside this group alone, already simplified
                # through chains. Settling still happens on the netted
                # cross-group figure, so this is shown, not acted on.
                "transfers": [
                    {"from": pair[0], "to": pair[1], "amount_cents": cents}
                    for pair, cents in repo.compute_group_balances(gid).items()
                ],
            }
        if section == "settings" and method == "POST":
            if not repo.is_group_owner(gid, uid):
                raise web.HTTPForbidden()
            title = string(body["title"], 100) if "title" in body else None
            currency = None
            if "currency" in body:
                currency = normalize_currency(string(body["currency"], 3))
                if not currency:
                    raise ValueError("Неизвестная валюта.")
            try:
                repo.update_group_settings(gid, uid, title, currency)
            except ValueError as error:
                raise InputError(str(error)) from None
            return {"ok": True}
        if section == "leave" and method == "POST":
            reason = repo.leave_group(gid, uid)
            if reason not in ("", "last"):
                raise ValueError(
                    LEAVE_ERRORS.get(reason, "Не удалось выйти из группы.")
                )
            return {"ok": True, "deleted": reason == "last"}
        if section == "members" and item and method == "DELETE":
            if not repo.is_group_owner(gid, uid):
                raise web.HTTPForbidden()
            reason = repo.remove_member(gid, int(item))
            if reason:
                raise ValueError(
                    REMOVE_ERRORS.get(reason, "Не удалось убрать участника.")
                )
            return {"ok": True}
        if section == "expenses" and not item:
            if method == "GET":
                deleted = bool(integer(int(query.get("deleted", "0")), 0, 1))
                offset = integer(int(query.get("offset", "0")), 0, 1000000)
                search = query.get("q", "")[:100].strip()
                payer = integer(int(query.get("payer", "0")), 0, 2**52 - 1)
                if payer and not repo.is_group_member(gid, payer):
                    raise ValueError("Этот человек не в группе.")
                expenses = repo.list_group_expenses(
                    gid, 30, offset, deleted=deleted, query=search, payer=payer
                )
                return {
                    "expenses": [public_expense(e) for e in expenses],
                    "total": repo.count_group_expenses(
                        gid, deleted=deleted, query=search, payer=payer
                    ),
                    "sum_cents": repo.sum_group_expenses(
                        gid, query=search, payer=payer, deleted=deleted
                    ),
                }
            if method == "POST":
                if body.get(
                    "group_currency", repo.group_currency(gid)
                ) != repo.group_currency(gid):
                    raise InputError(
                        "Валюта группы изменилась. Откройте «Изменить» в очереди и проверьте сумму."
                    )
                payer, desc, amount, shares, orig, orig_amount = expense_fields(body)
                operation = string(body.get("operation_id"), 64)
                if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", operation):
                    raise ValueError("Некорректный идентификатор операции.")
                previous = repo.expense_by_operation(gid, uid, operation)
                if previous and (
                    previous["deleted"]
                    or (
                        previous["payer"],
                        previous["desc"],
                        previous["amount_cents"],
                        previous["shares"],
                        previous["orig_currency"],
                        previous["orig_amount_cents"],
                    )
                    != (payer, desc, amount, shares, orig, orig_amount)
                ):
                    raise web.HTTPConflict(
                        reason="Трата уже отправлена и отличается от записи в очереди. Откройте «Изменить»."
                    )
                eid = repo.create_expense(
                    gid,
                    uid,
                    payer,
                    desc,
                    amount,
                    shares,
                    orig_currency=orig,
                    orig_amount_cents=orig_amount,
                    operation_id=f"web:{uid}:{operation}",
                )
                return {"id": eid}
        if section == "expenses" and item:
            eid = int(item)
            expense = repo.get_expense(eid, gid, include_deleted=True)
            if not expense:
                raise web.HTTPNotFound()
            if action == "history" and method == "GET":
                offset = integer(int(query.get("offset", "0")), 0, 1000000)
                rows = repo.expense_history(eid, gid, uid, limit=21, offset=offset)
                history, ids = [], set()
                for record in rows[:20]:
                    ids.add(record["actor_tg_id"])
                    snapshots = {}
                    for key in ("before", "after"):
                        snapshot = (
                            json.loads(record[f"{key}_json"])
                            if record[f"{key}_json"]
                            else None
                        )
                        snapshots[key] = public_expense(snapshot) if snapshot else None
                        if snapshot:
                            ids.update(
                                (snapshot["payer"], *map(int, snapshot["shares"]))
                            )
                    history.append(
                        dict(
                            id=record["id"],
                            actor=record["actor_tg_id"],
                            action=record["action"],
                            created_at=record["created_at"],
                            **snapshots,
                        )
                    )
                return {
                    "history": history,
                    "names": repo.names_for(ids),
                    "more": len(rows) > 20,
                }
            if action == "restore" and method == "POST":
                if expense["created_by"] != uid:
                    raise web.HTTPForbidden()
                try:
                    repo.restore_expense(eid, gid, uid)
                except ValueError as error:
                    raise InputError(str(error)) from None
                return {"ok": True}
            if action:
                raise web.HTTPMethodNotAllowed(
                    method, ["GET" if action == "history" else "POST"]
                )
            if method == "GET":
                return dict(
                    public_expense(expense),
                    shares=repo.get_expense_shares(eid),
                    can_edit=repo.can_edit_expense(eid, gid, uid),
                    can_restore=expense["deleted"] and expense["created_by"] == uid,
                )
            if expense["deleted"]:
                raise web.HTTPNotFound()
            if method == "POST":
                if not repo.can_edit_expense(eid, gid, uid):
                    raise web.HTTPForbidden()
                payer, desc, amount, shares, orig, orig_amount = expense_fields(body)
                repo.update_expense(
                    eid,
                    gid,
                    payer,
                    desc,
                    amount,
                    shares,
                    orig_currency=orig,
                    orig_amount_cents=orig_amount,
                    actor=uid,
                    expected_revision=integer(body.get("revision"), 0, 2**31),
                )
                return {"ok": True}
            if method == "DELETE":
                if not repo.can_delete_expense(eid, gid, uid):
                    raise web.HTTPForbidden()
                repo.delete_expense(eid, actor=uid)
                return {"ok": True}
        raise web.HTTPMethodNotAllowed(method, ["GET", "POST"])


def create_web_app(
    repo, token, url, max_age=3600, rates=None, bot="", telegram_bot=None
):
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
                409: "Трата уже отправлена и отличается от записи в очереди. Откройте «Изменить».",
                413: "Фото слишком большое. Выберите файл до 10 МБ."
                if request.path.endswith("/receipt")
                else "Слишком большой запрос.",
                415: "Выберите фото в формате JPEG или PNG.",
            }
            response = web.json_response(
                {"error": messages.get(error.status, "Некорректный запрос.")},
                status=error.status,
            )
        except InputError as error:
            response = web.json_response({"error": str(error)}, status=400)
        except TelegramError:
            # Telegram exceptions may contain download URLs with the bot token.
            response = web.json_response(
                {
                    "error": "Telegram не принял запрос. Проверьте, что бот запущен в личном чате, и повторите. Для чека выберите обычное фото JPEG или PNG."
                },
                status=502,
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
                "Content-Security-Policy": "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org",
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
            bot,
        )
        return web.json_response(result)

    file_slots = asyncio.Semaphore(3)

    async def receipt(request):
        uid = request["user"]["id"]
        gid, eid = int(request.match_info["gid"]), int(request.match_info["eid"])
        revision = (
            integer(int(request.query.get("revision", "-1")), 0, 2**31)
            if request.method != "GET"
            else None
        )
        expense = await asyncio.to_thread(
            expense_access, repo, uid, gid, eid, request.method != "GET", revision
        )
        if request.method == "DELETE":
            await asyncio.to_thread(repo.set_receipt, eid, gid, "", uid, revision)
            return web.json_response({"ok": True})
        if telegram_bot is None:
            raise InputError("Хранение чеков в Telegram пока недоступно.")
        async with file_slots:
            if request.method == "GET":
                if not expense["receipt"]:
                    raise web.HTTPNotFound()
                remote = await telegram_bot.get_file(expense["receipt"])
                if remote.file_size and remote.file_size > RECEIPT_LIMIT:
                    raise InputError("Этот чек слишком большой для просмотра.")
                data = await remote.download_as_bytearray()
                # Recheck membership after the network await.
                await asyncio.to_thread(expense_access, repo, uid, gid, eid)
                return web.Response(body=bytes(data), content_type="image/jpeg")
            if request.content_type not in ("image/jpeg", "image/png"):
                raise web.HTTPUnsupportedMediaType()
            # read() enforces this limit even for a chunked upload. No temp files.
            data = await request.clone(client_max_size=RECEIPT_LIMIT + 1).read()
            if len(data) > RECEIPT_LIMIT:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=RECEIPT_LIMIT, actual_size=len(data)
                )
            if not (
                data.startswith(b"\xff\xd8\xff")
                or data.startswith(b"\x89PNG\r\n\x1a\n")
            ):
                raise web.HTTPUnsupportedMediaType()
            await asyncio.to_thread(expense_access, repo, uid, gid, eid, True, revision)
            message = await telegram_bot.send_photo(
                chat_id=uid,
                photo=data,
                caption=f"Чек к трате «{expense['desc']}» (№{eid})",
                disable_notification=True,
            )
            try:
                await asyncio.to_thread(
                    repo.set_receipt, eid, gid, message.photo[-1].file_id, uid, revision
                )
            except ValueError:
                raise InputError(
                    "Фото сохранено в вашем чате Telegram, но трата уже изменилась. Откройте её заново и прикрепите чек ещё раз."
                ) from None
            return web.json_response({"ok": True})

    def make_export(uid, gid):
        with repo._lock:
            if not repo.is_group_member(gid, uid):
                raise web.HTTPForbidden()
            data, tz = repo.export_group(gid), repo.user_tz(uid)
        return build_group_workbook(data, tz), export_filename(gid, data["title"])

    async def export(request):
        uid, gid = request["user"]["id"], int(request.match_info["gid"])
        data, filename = await asyncio.to_thread(make_export, uid, gid)
        if request.method == "POST":
            if telegram_bot is None:
                raise InputError(
                    "Отправка в Telegram пока недоступна. Используйте «Скачать»."
                )
            await telegram_bot.send_document(
                chat_id=uid, document=data, filename=filename, disable_notification=True
            )
            return web.json_response({"ok": True})
        return web.Response(
            body=data,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
            },
        )

    async def rate(request):
        """Outside `dispatch`: the provider call is async and must not hold the lock."""
        if rates is None:
            raise web.HTTPNotFound()
        orig = normalize_currency(request.query.get("from", ""))
        base = normalize_currency(request.query.get("to", ""))
        if not orig or not base:
            raise ValueError("Неизвестная валюта.")
        amount = integer(int(request.query.get("amount", "0")))
        converted = await rates.convert(amount, orig, base)
        if converted is None:
            return web.json_response({"converted": None})
        return web.json_response(
            {
                "converted": converted[0],
                "rate": converted[1],
                "updated": converted[2],
            }
        )

    async def health(request):
        return web.json_response({"status": "ok", "app": "expensebot"})

    async def static(request):
        """Serve web/ as ES modules: a lowercase name, one safe extension."""
        name = request.match_info.get("name") or "index.html"
        if not STATIC_NAME.fullmatch(name):
            raise web.HTTPNotFound()
        path = (STATIC / name).resolve()
        if not path.is_file() or STATIC.resolve() not in path.parents:
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    app = web.Application(middlewares=[security], client_max_size=32768)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/rate", rate)
    for method in ("GET", "POST", "DELETE"):
        app.router.add_route(
            method, r"/api/groups/{gid:\d+}/expenses/{eid:\d+}/receipt", receipt
        )
    for method in ("GET", "POST"):
        app.router.add_route(method, r"/api/groups/{gid:\d+}/export", export)
    app.router.add_route("*", "/api/{path:.*}", api)
    app.router.add_get("/", static)
    app.router.add_get("/{name:.*}", static)
    return app


async def start_web(
    repo, token, url, port, max_age=3600, rates=None, bot="", telegram_bot=None
):
    integer(port, 1024, 65535)
    runner = web.AppRunner(
        create_web_app(repo, token, url, max_age, rates, bot, telegram_bot),
        access_log=None,
    )
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", port).start()
    except BaseException:
        await runner.cleanup()
        raise
    logger.info("Mini App listening on 127.0.0.1:%s", port)
    return runner
