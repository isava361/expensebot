"""Telegram navigation, expense wizard and notifications."""

import asyncio
import html
import json
import logging
from io import BytesIO
from typing import Optional
from urllib.parse import quote
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from repository import Repo
from rates import Rates
from workbook import build_group_workbook, export_filename
from core import (
    GROUPS_PER_PAGE,
    EXPENSES_PER_PAGE,
    MEMBERS_PER_PAGE,
    SETTLEMENTS_PER_PAGE,
    now_unix,
    cents_from_str,
    split_amount_currency_desc,
    normalize_currency,
    format_cents,
    format_rate,
    parse_tz_offset,
    tz_label,
    format_time,
    split_message,
    rand_code,
    extract_start_code_from_text,
    extract_bare_code,
)

logger = logging.getLogger(__name__)

# ---------- Keyboards ----------


def main_keyboard() -> ReplyKeyboardMarkup:
    # The keyboard sits under every screen forever, so it holds only what is
    # used daily. Inviting people is a second route to the group card, which
    # already carries a share button; creating a group happens once per trip
    # and lives on the group list instead.
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🧾 Добавить трату")],
            [KeyboardButton("💰 Долги"), KeyboardButton("👥 Мои группы")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def new_group_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("➕ Создать группу", callback_data="gnew")


def inline_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отмена", callback_data="cancel_flow")]]
    )


# ---------- State helpers (stored in context.user_data) ----------


def _get_ae(ctx: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return ctx.user_data.get("add_expense")


def _set_ae(ctx: ContextTypes.DEFAULT_TYPE, state: dict) -> None:
    state.setdefault("operation_id", rand_code())
    ctx.user_data["add_expense"] = state


def _del_ae(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("add_expense", None)


def _get_pay(ctx: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return ctx.user_data.get("pay_part")


def _set_pay(ctx: ContextTypes.DEFAULT_TYPE, state: dict) -> None:
    ctx.user_data["pay_part"] = state


def _del_pay(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("pay_part", None)


def _get_receipt(ctx: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return ctx.user_data.get("receipt_for")


def _set_receipt(ctx: ContextTypes.DEFAULT_TYPE, state: dict) -> None:
    ctx.user_data["receipt_for"] = state


def _del_receipt(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("receipt_for", None)


def _get_group_input(ctx: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return ctx.user_data.get("group_input")


def _set_group_input(ctx: ContextTypes.DEFAULT_TYPE, state: dict) -> None:
    ctx.user_data["group_input"] = state


def _del_group_input(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("group_input", None)


def _is_new_group(ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    return ctx.user_data.get("new_group_ask", False)


def _set_new_group(ctx: ContextTypes.DEFAULT_TYPE, v: bool) -> None:
    ctx.user_data["new_group_ask"] = v


# ---------- App ----------

# Labels from keyboards the bot used to send. Clients keep showing them until
# they receive a new keyboard, so they have to keep working.
_LEGACY_DEBT_BUTTONS = {"📊 Балансы", "🔄 Взаимозачёт"}
_LEGACY_GROUP_BUTTONS = {"🔗 Приглашение", "➕ Создать группу"}

_TOP_BUTTONS = (
    {
        "👥 Мои группы",
        "🧾 Добавить трату",
        "💰 Долги",
    }
    | _LEGACY_DEBT_BUTTONS
    | _LEGACY_GROUP_BUTTONS
)


class App:
    def __init__(
        self,
        repo: Repo,
        bot_username: str = "",
        rates: Optional[Rates] = None,
    ):
        self.repo = repo
        self.base = bot_username
        self.rates = rates if rates is not None else Rates()

    def _best_name(self, user) -> str:
        if user.username:
            return "@" + user.username
        parts = [user.first_name or "", user.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        return name or str(user.id)

    def _groups_page_keyboard(
        self, uid: int, page: int, mode: str
    ) -> tuple[InlineKeyboardMarkup, int]:
        gs = self.repo.list_user_groups(uid)
        gs.sort(key=lambda g: g["id"], reverse=True)
        total = len(gs)
        if total == 0:
            return InlineKeyboardMarkup([]), 0

        start = page * GROUPS_PER_PAGE
        if start >= total:
            page = 0
            start = 0
        end = min(start + GROUPS_PER_PAGE, total)

        rows = []
        for g in gs[start:end]:
            cb = {"mg": f"mgsel|{g['id']}", "ae": f"aesel|{g['id']}"}[mode]
            rows.append(
                [InlineKeyboardButton(f"#{g['id']}: {g['title']}", callback_data=cb)]
            )

        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("« Назад", callback_data=f"{mode}|p:{page - 1}")
            )
        if end < total:
            nav.append(
                InlineKeyboardButton("Вперёд »", callback_data=f"{mode}|p:{page + 1}")
            )
        if nav:
            rows.append(nav)
        if mode == "mg":
            rows.append([new_group_button()])

        return InlineKeyboardMarkup(rows), total

    async def _send_group_picker(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        mode: str,
        page: int = 0,
    ) -> None:
        """The group list, or the way to make a first group when there is none."""
        prompts = {
            "mg": "Выберите группу:",
            "ae": "Выберите группу для добавления траты:",
        }
        markup, total = self._groups_page_keyboard(update.effective_user.id, page, mode)
        if total == 0:
            await self._edit_or_send(
                update,
                "У вас пока нет групп. Создайте первую — и позовите в неё людей.",
                InlineKeyboardMarkup([[new_group_button()]]),
            )
            return
        await self._edit_or_send(update, prompts[mode], markup)

    async def _refresh_keyboard(self, update: Update, uid: int) -> None:
        """Send the current keyboard to a user still holding an older one.

        A reply keyboard only changes when the bot attaches a new one to a
        message, and the screens below mostly carry inline keyboards, which
        cannot double as one. So this sends a short message of its own, once
        per user per layout change.
        """
        if not self.repo.has_stale_keyboard(uid):
            return
        self.repo.mark_keyboard_current(uid)
        await update.effective_chat.send_message(
            "Кнопки внизу обновились: приглашение и создание группы"
            " переехали в «👥 Мои группы».",
            reply_markup=main_keyboard(),
        )

    async def _edit_or_send(
        self,
        update: Update,
        text: str,
        markup: Optional[InlineKeyboardMarkup] = None,
    ) -> None:
        """Show a screen, splitting it up if it outgrew one message.

        The buttons ride on the last piece, where the reader ends up.
        """
        chunks = split_message(text)
        message = update.callback_query.message if update.callback_query else None
        for i, chunk in enumerate(chunks):
            tail = markup if i == len(chunks) - 1 else None
            if i == 0 and message:
                await message.edit_text(chunk, reply_markup=tail)
            else:
                await update.effective_chat.send_message(chunk, reply_markup=tail)

    # ---------- Command handlers ----------

    async def on_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        self.repo.upsert_user(user.id, self._best_name(user))
        self.repo.mark_keyboard_current(user.id)

        raw = (update.effective_message.text or "").strip()
        raw = (
            raw.replace(" ", " ").replace(" ", " ").replace(" ", " ").replace("+", " ")
        )

        code = (ctx.args or [""])[0] if ctx.args else ""
        if not code:
            code = extract_start_code_from_text(raw)

        if code:
            try:
                gid, title = self.repo.join_by_code(code, user.id)
                await update.effective_chat.send_message(
                    f"Вы присоединились к группе #{gid}: {title}",
                    reply_markup=main_keyboard(),
                )
                return
            except Exception:
                pass

        await update.effective_chat.send_message(
            "Привет! Я помогу делить траты в поездках.\nИспользуйте кнопки ниже.\n"
            f"Время показываю в {tz_label(self.repo.user_tz(user.id))}"
            " — сменить можно командой /tz +3.",
            reply_markup=main_keyboard(),
        )

    async def on_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._refresh_keyboard(update, update.effective_user.id)
        if (
            _get_ae(ctx)
            or _is_new_group(ctx)
            or _get_pay(ctx)
            or _get_receipt(ctx)
            or _get_group_input(ctx)
        ):
            _del_ae(ctx)
            _del_pay(ctx)
            _del_receipt(ctx)
            _del_group_input(ctx)
            _set_new_group(ctx, False)
            await update.effective_chat.send_message(
                "Ок, отменил. Можно начать заново.", reply_markup=main_keyboard()
            )
        else:
            await update.effective_chat.send_message("Нечего отменять.")

    async def on_tz(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Set the offset every timestamp this user sees is rendered in."""
        uid = update.effective_user.id
        self.repo.upsert_user(uid, self._best_name(update.effective_user))
        await self._refresh_keyboard(update, uid)

        raw = " ".join(ctx.args or []).strip()
        if not raw:
            await update.effective_chat.send_message(
                f"Ваш часовой пояс: {tz_label(self.repo.user_tz(uid))}.\n"
                "Сменить: /tz +3, /tz -05:30, /tz 0."
            )
            return
        try:
            offset = parse_tz_offset(raw)
        except ValueError:
            await update.effective_chat.send_message(
                "Не понял смещение. Примеры: /tz +3, /tz -05:30, /tz 0."
            )
            return

        self.repo.set_user_tz(uid, offset)
        await update.effective_chat.send_message(
            f"Часовой пояс: {tz_label(offset)}."
            f" Сейчас это {format_time(now_unix(), offset)}."
        )

    async def on_join(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id
        self.repo.upsert_user(uid, self._best_name(update.effective_user))
        await self._refresh_keyboard(update, uid)
        # PTB strips the /join@botname prefix automatically; ctx.args has the rest
        args = ctx.args or []
        code = ""
        if args:
            code = args[0].lstrip("_")  # handle /join _code or /join_code edge cases

        if not code:
            await update.effective_chat.send_message(
                "Использование: /join <код>\n"
                "Также работает команда: /join_<код> и ссылка /start <код>"
            )
            return

        try:
            gid, title = self.repo.join_by_code(code, uid)
            await update.effective_chat.send_message(
                f"Вы присоединились к группе #{gid}: {title}",
                reply_markup=main_keyboard(),
            )
        except Exception:
            # Bug fix: send user-friendly message instead of propagating exception
            await update.effective_chat.send_message(
                "Неверный или истёкший код приглашения."
            )

    # ---------- Photo handler ----------

    async def on_photo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id
        self.repo.upsert_user(uid, self._best_name(update.effective_user))
        await self._refresh_keyboard(update, uid)
        if _get_receipt(ctx) is None:
            await update.effective_chat.send_message(
                "Чтобы приложить чек, откройте трату и нажмите «📎 Приложить чек»."
            )
            return
        await self._flow_receipt(update, ctx)

    # ---------- Text handler ----------

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        txt = (update.effective_message.text or "").strip()
        uid = update.effective_user.id
        self.repo.upsert_user(uid, self._best_name(update.effective_user))
        await self._refresh_keyboard(update, uid)

        # Block top-level buttons while wizard is active
        wizard_running = (
            _is_new_group(ctx)
            or _get_ae(ctx) is not None
            or _get_pay(ctx) is not None
            or _get_receipt(ctx) is not None
            or _get_group_input(ctx) is not None
        )
        if wizard_running and txt in _TOP_BUTTONS:
            await update.effective_chat.send_message(
                "Сначала завершите текущее действие. Отправьте запрошенные данные"
                " или нажмите /cancel (или «❌ Отмена»)."
            )
            return

        in_wizard = wizard_running

        if not in_wizard:
            # /start@<code> or /start_<code>
            if txt.startswith("/start@") or txt.startswith("/start_"):
                code = txt.removeprefix("/start@").removeprefix("/start_").strip()
                if code:
                    try:
                        gid, title = self.repo.join_by_code(code, uid)
                        await update.effective_chat.send_message(
                            f"Вы присоединились к группе #{gid}: {title}",
                            reply_markup=main_keyboard(),
                        )
                        return
                    except Exception:
                        pass

            # /join_<code>
            if txt.startswith("/join_"):
                fields = txt.removeprefix("/join_").split()
                if fields:
                    try:
                        gid, title = self.repo.join_by_code(fields[0], uid)
                        await update.effective_chat.send_message(
                            f"Вы присоединились к группе #{gid}: {title}",
                            reply_markup=main_keyboard(),
                        )
                        return
                    except Exception:
                        pass

            # URL containing ?start=<code>
            code = extract_start_code_from_text(txt)
            if code:
                try:
                    gid, title = self.repo.join_by_code(code, uid)
                    await update.effective_chat.send_message(
                        f"Вы присоединились к группе #{gid}: {title}",
                        reply_markup=main_keyboard(),
                    )
                    return
                except Exception:
                    pass

            # Bare invite code
            code = extract_bare_code(txt)
            if code:
                try:
                    gid, title = self.repo.join_by_code(code, uid)
                    await update.effective_chat.send_message(
                        f"Вы присоединились к группе #{gid}: {title}",
                        reply_markup=main_keyboard(),
                    )
                    return
                except Exception:
                    pass

        # New group name input
        if _is_new_group(ctx):
            title = txt.strip()
            if not title or title.startswith("/"):
                await update.effective_chat.send_message(
                    "Название не может быть пустым."
                    " Введите название группы одним сообщением."
                )
                return
            _set_new_group(ctx, False)
            gid, code = self.repo.create_group(title, uid)

            enc = quote(f"/join {code}", safe="")
            share_href = f"https://t.me/share/url?url={enc}"
            html_join = f'<a href="{share_href}">/join {code}</a>'
            currency = self.repo.group_currency(gid)
            text = (
                f"Группа #{gid} создана: {html.escape(title)}\n"
                f"Валюта: {currency} — сменить можно в настройках группы,"
                f" пока в ней нет трат.\n"
                f"Команда: {html_join}"
            )
            await update.effective_chat.send_message(
                text,
                reply_markup=main_keyboard(),
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return

        # Group settings text input
        group_input = _get_group_input(ctx)
        if group_input is not None:
            await self._flow_group_input(update, ctx, group_input, txt)
            return

        # Waiting for a receipt photo
        if _get_receipt(ctx) is not None:
            await self._flow_receipt(update, ctx)
            return

        # Partial payment amount
        pay = _get_pay(ctx)
        if pay is not None:
            await self._flow_pay_part(update, ctx, pay, txt)
            return

        # Add-expense wizard
        st = _get_ae(ctx)
        if st is not None:
            await self._flow_add_expense(update, ctx, st, txt)
            return

        # Top-level button routing
        # "➕ Создать группу" is only on retired keyboards now, but a client
        # keeps showing them until it is handed a new one.
        if txt == "➕ Создать группу":
            await self._ask_group_name(update, ctx)
        elif txt == "👥 Мои группы" or txt in _LEGACY_GROUP_BUTTONS:
            # Inviting starts from the group card, which carries the link.
            await self._send_group_picker(update, ctx, "mg")
        elif txt == "🧾 Добавить трату":
            await self._send_group_picker(update, ctx, "ae")
        elif txt == "💰 Долги" or txt in _LEGACY_DEBT_BUTTONS:
            await self._show_debts(update, ctx)

    async def _flow_pay_part(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        pay: dict,
        txt: str,
    ) -> None:
        currency = pay["currency"]
        try:
            amount = cents_from_str(txt.strip())
            if amount <= 0:
                raise ValueError("non-positive")
        except Exception:
            await update.effective_chat.send_message(
                f"Сумма не распознана. Пришлите число, напр. 350.50"
                f" (не больше {format_cents(pay['max'], currency)}).",
                reply_markup=inline_cancel(),
            )
            return

        if amount > pay["max"]:
            await update.effective_chat.send_message(
                f"Это больше долга. Максимум — {format_cents(pay['max'], currency)}.",
                reply_markup=inline_cancel(),
            )
            return

        _del_pay(ctx)
        if await self._request_payment(update, ctx, pay["to"], currency, amount):
            await update.effective_chat.send_message(
                f"Отправил(а) {self.repo.user_name(pay['to'])} запрос на"
                f" подтверждение {format_cents(amount, currency)}."
                " Долг закроется, когда получатель подтвердит.",
                reply_markup=main_keyboard(),
            )
        else:
            await update.effective_chat.send_message(
                "Расчёт уже изменился — откройте «💰 Долги» заново.",
                reply_markup=main_keyboard(),
            )
        await self._show_debts(update, ctx)

    async def _ask_group_name(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        _set_new_group(ctx, True)
        await update.effective_chat.send_message(
            "Введи название новой группы одним сообщением.",
            reply_markup=inline_cancel(),
        )

    # ---------- Add-expense wizard (text steps) ----------

    async def _flow_add_expense(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        st: dict,
        txt: str,
    ) -> None:
        step = st.get("step")

        if step == "await_amount_desc":
            amount_text, currency, description = split_amount_currency_desc(txt)
            if not amount_text:
                await update.effective_chat.send_message(
                    "Нужно прислать сумму и описание. Пример: 1200 обед",
                    reply_markup=inline_cancel(),
                )
                return
            try:
                amt = cents_from_str(amount_text)
                if amt <= 0:
                    raise ValueError("non-positive")
            except Exception:
                await update.effective_chat.send_message(
                    "Сумма не распознана. Пример: 1200 обед",
                    reply_markup=inline_cancel(),
                )
                return

            base = st.get("currency") or self.repo.group_currency(st["group_id"])
            st["currency"] = base
            st["description"] = description or "Без описания"

            # Paid in another currency: the group's ledger stays in one
            # unit, so convert at the day's rate — and let the payer replace
            # it with what their bank actually took, which is the number the
            # others will be checking against.
            if currency and currency != base:
                st["orig_currency"] = currency
                st["orig_amount_cents"] = amt
                converted = await self.rates.convert(amt, currency, base)
                if converted is None:
                    st["step"] = "await_base_amount"
                    _set_ae(ctx, st)
                    await update.effective_chat.send_message(
                        f"Не знаю курс {currency} к {base}."
                        f" {format_cents(amt, currency)} — сколько это в {base}?"
                        f" Пришлите сумму, которую с вас списали.",
                        reply_markup=inline_cancel(),
                    )
                    return

                cents, rate, updated = converted
                st["amount_cents"] = cents
                st["step"] = "confirm_rate"
                _set_ae(ctx, st)
                tz = self.repo.user_tz(update.effective_user.id)
                await update.effective_chat.send_message(
                    f"{format_cents(amt, currency)} ≈ {format_cents(cents, base)}\n"
                    f"Курс: 1 {currency} = {rate:.4f} {base}"
                    f" (на {format_time(updated, tz)})\n"
                    f"Если банк списал другую сумму — пришлите её числом.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    f"✅ {format_cents(cents, base)} — дальше",
                                    callback_data="rateok",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    "❌ Отмена", callback_data="cancel_flow"
                                )
                            ],
                        ]
                    ),
                )
                return

            st["amount_cents"] = amt
            st["step"] = "choose_payer"
            _set_ae(ctx, st)
            await self._ask_payer(update, ctx, st)

        elif step in ("await_base_amount", "confirm_rate"):
            base = st.get("currency") or self.repo.group_currency(st["group_id"])
            try:
                amt = cents_from_str(txt.strip())
                if amt <= 0:
                    raise ValueError("non-positive")
            except Exception:
                await update.effective_chat.send_message(
                    f"Сумма не распознана. Пришлите, сколько это в {base}, напр. 9500",
                    reply_markup=inline_cancel(),
                )
                return

            st["amount_cents"] = amt
            st["step"] = "choose_payer"
            _set_ae(ctx, st)
            rate = format_rate(amt, st["orig_amount_cents"], base, st["orig_currency"])
            await update.effective_chat.send_message(
                f"Записал: {format_cents(st['orig_amount_cents'], st['orig_currency'])}"
                f" = {format_cents(amt, base)}" + (f" ({rate})" if rate else "")
            )
            await self._ask_payer(update, ctx, st)

        elif step == "await_custom_share":
            custom_left: list = st.get("custom_left", [])
            if not custom_left:
                st["step"] = "confirm"
                _set_ae(ctx, st)
                await self._finalize_expense(update, ctx, st)
                return

            try:
                amt = cents_from_str(txt.strip())
                if amt < 0:
                    raise ValueError("negative")
            except Exception:
                await self._edit_or_send(
                    update,
                    "Сумма не распознана. Пришлите число, напр. 350.50",
                    inline_cancel(),
                )
                return

            next_uid = custom_left[0]
            name = self.repo.user_name(next_uid)
            custom_shares: dict = st.setdefault("custom_shares", {})
            remaining = st["amount_cents"] - sum(custom_shares.values())

            if amt > remaining:
                await self._edit_or_send(
                    update,
                    f"Слишком много. Остаток — {format_cents(remaining)}."
                    f" Введите сумму для {name} не больше остатка.",
                    inline_cancel(),
                )
                return
            if len(custom_left) == 1 and amt != remaining:
                await self._edit_or_send(
                    update,
                    f"Для последнего участника осталось {format_cents(remaining)}. "
                    "Введите эту сумму или измените распределение перед сохранением.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    f"Назначить остаток {format_cents(remaining)}",
                                    callback_data=f"customremain|{next_uid}",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    "❌ Отмена", callback_data="cancel_flow"
                                )
                            ],
                        ]
                    ),
                )
                return

            custom_shares[next_uid] = amt
            st["custom_left"] = custom_left[1:]
            _set_ae(ctx, st)

            progress = self._shares_progress(st)

            if not st["custom_left"]:
                await self._edit_or_send(
                    update,
                    progress + "\nВсе суммы заданы. Проверьте трату.",
                    inline_cancel(),
                )
                await self._finalize_expense(update, ctx, st)
            else:
                await self._ask_next_custom(update, ctx, st)

    def _shares_progress(self, st: dict) -> str:
        custom_shares = st.get("custom_shares", {})
        if not custom_shares:
            return ""
        items = sorted(
            f"{self.repo.user_name(pid)}: {format_cents(v)}"
            for pid, v in custom_shares.items()
        )
        return "Назначено:\n• " + "\n• ".join(items)

    async def _ask_next_custom(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, st: dict
    ) -> None:
        custom_left: list = st.get("custom_left", [])
        if not custom_left:
            await self._finalize_expense(update, ctx, st)
            return
        uid = custom_left[0]
        name = self.repo.user_name(uid)
        remaining = st["amount_cents"] - sum(st.get("custom_shares", {}).values())
        progress = self._shares_progress(st)
        prefix = progress + "\n\n" if progress else ""
        rows = []
        if len(custom_left) == 1:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"Назначить остаток {format_cents(remaining)}",
                        callback_data=f"customremain|{uid}",
                    )
                ]
            )
        rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_flow")])
        await self._edit_or_send(
            update,
            f"{prefix}Введите сумму для участника {name}"
            f" (остаток — {format_cents(remaining)}, максимум — {format_cents(remaining)}):",
            InlineKeyboardMarkup(rows),
        )

    # ---------- Callback handler ----------

    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self.repo.upsert_user(
            update.effective_user.id, self._best_name(update.effective_user)
        )
        query = update.callback_query
        data = query.data
        uid = update.effective_user.id
        await self._refresh_keyboard(update, uid)

        if data == "cancel_flow":
            _del_ae(ctx)
            _del_pay(ctx)
            _del_receipt(ctx)
            _del_group_input(ctx)
            _set_new_group(ctx, False)
            await self._edit_or_send(update, "Отменено. Что дальше?")
            await query.answer("Отменено")
            return

        # Create a group from the group list
        if data == "gnew":
            await query.answer()
            await self._ask_group_name(update, ctx)
            return

        # Group list pagination
        for prefix, mode in [("mg|p:", "mg"), ("ae|p:", "ae")]:
            if data.startswith(prefix):
                await self._send_group_picker(
                    update, ctx, mode, int(data[len(prefix) :])
                )
                await query.answer()
                return

        # Group selection
        if data.startswith("mgsel|"):
            gid = int(data[len("mgsel|") :])
            if not self.repo.is_group_member(gid, uid):
                await query.answer("Нет доступа", show_alert=True)
                return
            await self._send_group_details(update, ctx, gid)
            await query.answer()
            return

        if data.startswith("aesel|"):
            gid = int(data[len("aesel|") :])
            if not self.repo.is_group_member(gid, uid):
                await query.answer("Нет доступа", show_alert=True)
                return
            base = self.repo.group_currency(gid)
            _set_ae(
                ctx,
                {
                    "group_id": gid,
                    "amount_cents": 0,
                    "description": "",
                    "payer": 0,
                    "participants": {},
                    "split_mode": "",
                    "custom_left": [],
                    "custom_shares": {},
                    "currency": base,
                    "orig_currency": "",
                    "orig_amount_cents": 0,
                    "step": "await_amount_desc",
                },
            )
            await update.effective_chat.send_message(
                f"Группа #{gid} выбрана, валюта — {base}. Пришлите сумму и"
                f" описание одним сообщением, напр.:\n1500 такси из аэропорта\n"
                f"Платили в другой валюте — укажите её: 100 EUR ужин"
            )
            await query.answer("Группа выбрана")
            return

        # Excel export of everything the group's debts are computed from
        if data.startswith("xlsx|"):
            gid = int(data[len("xlsx|") :])
            if not self.repo.is_group_member(gid, uid):
                await query.answer("Нет доступа", show_alert=True)
                return
            await query.answer("Готовлю файл…")
            await self._send_group_workbook(update, gid)
            return

        # Expense list pagination
        if data.startswith("explist|") or data.startswith("expdeleted|"):
            parts = data.split("|")
            if len(parts) >= 3 and parts[2].startswith("p:"):
                gid = int(parts[1])
                page = int(parts[2][2:])
                if self.repo.is_group_member(gid, uid):
                    await self._send_expenses_page(
                        update, ctx, gid, page, deleted=parts[0] == "expdeleted"
                    )
                else:
                    await query.answer("Нет доступа", show_alert=True)
                    return
            await query.answer()
            return

        # One expense in full
        if data.startswith("exphist|") or data.startswith("exprestore|"):
            parts = data.split("|")
            if len(parts) != 4:
                await query.answer("Некорректная кнопка", show_alert=True)
                return
            eid, gid, page = map(int, parts[1:])
            if not self.repo.is_group_member(gid, uid):
                await query.answer("Нет доступа", show_alert=True)
                return
            if parts[0] == "exphist":
                await query.answer()
                await self._send_expense_history(update, gid, eid, page)
            else:
                before = self.repo.get_expense(eid, gid, include_deleted=True)
                try:
                    restored = self.repo.restore_expense(eid, gid, uid)
                except ValueError as e:
                    await query.answer(str(e), show_alert=True)
                    return
                await query.answer(
                    "Трата восстановлена" if restored else "Трата уже восстановлена"
                )
                if restored:
                    await self._notify_expense_change(
                        ctx,
                        gid,
                        before,
                        self.repo.get_expense(eid, gid),
                        uid,
                        "восстановлена",
                    )
                await self._send_expense_card(update, ctx, gid, eid, 0)
            return

        if data.startswith("expcard|"):
            parts = data.split("|")
            if (
                len(parts) >= 4
                and parts[2].startswith("gid:")
                and parts[3].startswith("p:")
            ):
                eid = int(parts[1])
                gid = int(parts[2][4:])
                page = int(parts[3][2:])
                if not self.repo.is_group_member(gid, uid):
                    await query.answer("Нет доступа", show_alert=True)
                    return
                await self._send_expense_card(update, ctx, gid, eid, page)
                await query.answer()
                return
            await query.answer("Ошибка")
            return

        # Edit, receipt attach / show / remove — all creator-only
        if (
            data.startswith("expedit|")
            or data.startswith("exprcpt|")
            or data.startswith("expshow|")
            or data.startswith("exprdel|")
        ):
            parts = data.split("|")
            if not (
                len(parts) >= 4
                and parts[2].startswith("gid:")
                and parts[3].startswith("p:")
            ):
                await query.answer("Ошибка")
                return
            action = parts[0]
            eid = int(parts[1])
            gid = int(parts[2][4:])
            page = int(parts[3][2:])
            if not self.repo.is_group_member(gid, uid):
                await query.answer("Нет доступа", show_alert=True)
                return

            if action == "expshow":
                item = self.repo.get_expense(eid, gid)
                if not item or not item["receipt"]:
                    await query.answer("Чека нет", show_alert=True)
                    return
                await query.answer()
                await update.effective_chat.send_photo(
                    item["receipt"], caption=f"Чек к трате #{eid}"
                )
                return

            if not self.repo.can_edit_expense(eid, gid, uid):
                await query.answer(
                    "Менять трату может только тот, кто её добавил",
                    show_alert=True,
                )
                return

            if action == "expedit":
                await query.answer()
                await self._start_expense_edit(update, ctx, gid, eid, page)
                return
            if action == "exprcpt":
                _set_receipt(ctx, {"group_id": gid, "expense_id": eid, "page": page})
                await query.answer()
                await update.effective_chat.send_message(
                    f"Пришлите фото чека для траты #{eid} одним сообщением"
                    f" (или /cancel).",
                    reply_markup=inline_cancel(),
                )
                return

            self.repo.set_receipt(eid, gid, "", actor=uid)
            await query.answer("Чек убран")
            await self._send_expense_card(update, ctx, gid, eid, page)
            return

        # Delete expense
        # Bug fix: parts[0] is "expdel", parts[1] is the expense ID.
        # The Go original used parts[0] stripped of "expdel|", which left
        # the literal string "expdel" and parsed to ID=0, silently doing nothing.
        if data.startswith("expdel|"):
            parts = data.split("|")
            if (
                len(parts) >= 4
                and parts[2].startswith("gid:")
                and parts[3].startswith("p:")
            ):
                eid = int(parts[1])
                gid = int(parts[2][4:])
                page = int(parts[3][2:])
                try:
                    if not self.repo.can_delete_expense(eid, gid, uid):
                        await query.answer("Нет прав на удаление", show_alert=True)
                        return
                    before = self.repo.get_expense(eid, gid)
                    self.repo.delete_expense(eid, actor=uid)
                    await query.answer("Удалено")
                    _del_receipt(ctx)
                    await self._notify_expense_change(
                        ctx,
                        gid,
                        before,
                        self.repo.get_expense(eid, gid, True),
                        uid,
                        "удалена",
                    )
                    await self._edit_or_send(
                        update,
                        f"Трата #{eid} удалена. Её можно восстановить.",
                        InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "↩️ Отменить удаление",
                                        callback_data=f"exprestore|{eid}|{gid}|0",
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "🕓 История",
                                        callback_data=f"exphist|{eid}|{gid}|0",
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "К списку трат",
                                        callback_data=f"explist|{gid}|p:{page}",
                                    )
                                ],
                            ]
                        ),
                    )
                    return
                except ValueError as e:
                    await query.answer(str(e), show_alert=True)
                    return
                except Exception:
                    logger.exception("failed to delete expense")
            await query.answer("Ошибка удаления")
            return

        # Open the unified debts screen
        if data == "debts":
            await self._show_debts(update, ctx)
            await query.answer()
            return

        # Claim a payment to one counterparty in one currency. Nothing is
        # settled until the person who is owed the money confirms it.
        if data.startswith("paynet|"):
            parts = data.split("|")
            if (
                len(parts) == 4
                and parts[1].startswith("to:")
                and parts[2].startswith("cur:")
                and parts[3].startswith("amt:")
            ):
                to = int(parts[1][3:])
                currency = parts[2][4:]
                amt = int(parts[3][4:])
                try:
                    if await self._request_payment(update, ctx, to, currency, amt):
                        await query.answer("Отправлено на подтверждение")
                    else:
                        await query.answer("Расчёт уже не актуален", show_alert=True)
                    await self._show_debts(update, ctx)
                    return
                except Exception:
                    logger.exception("failed to request settlement")
            await query.answer("Ошибка подтверждения")
            return

        # Hand over part of a debt
        if data.startswith("paypart|"):
            parts = data.split("|")
            if (
                len(parts) == 3
                and parts[1].startswith("to:")
                and parts[2].startswith("cur:")
            ):
                to = int(parts[1][3:])
                currency = parts[2][4:]
                entry = self.repo.compute_user_debts(uid).get(to, {}).get(currency)
                owed = -entry["net"] if entry else 0
                if owed <= 0:
                    await query.answer("Этот долг уже закрыт", show_alert=True)
                    await self._show_debts(update, ctx)
                    return
                _set_pay(ctx, {"to": to, "currency": currency, "max": owed})
                await query.answer()
                await self._edit_or_send(
                    update,
                    f"Сколько вы отдали — {self.repo.user_name(to)}?"
                    f" Весь долг: {format_cents(owed, currency)}."
                    f" Пришлите сумму одним сообщением.",
                    inline_cancel(),
                )
                return
            await query.answer("Ошибка")
            return

        # Confirm or refuse a payment claim
        if data.startswith("paycfm|") or data.startswith("payrej|"):
            batch = data.split("|", 1)[1]
            info = self.repo.batch_info(batch)
            if info is None or info["confirmed"]:
                await query.answer("Запрос уже закрыт", show_alert=True)
                await self._show_debts(update, ctx)
                return

            amount = format_cents(info["amount_cents"], info["currency"])
            other = info["from"] if info["to"] == uid else info["to"]
            actor = self.repo.user_name(uid)

            if data.startswith("paycfm|"):
                if not self.repo.confirm_settlement(batch, uid):
                    await query.answer(
                        "Подтвердить может только тот, кому платили",
                        show_alert=True,
                    )
                    return
                note = (
                    f"{actor} подтвердил(а) получение {amount}. Долг закрыт."
                    if info["amount_cents"] > 0
                    else f"{actor} подтвердил(а) закрытие взаимных расчётов."
                )
                await query.answer("Подтверждено")
            else:
                if not self.repo.reject_settlement(batch, uid):
                    await query.answer("Нельзя отменить этот запрос", show_alert=True)
                    return
                if info["from"] == uid:
                    note = f"{actor} отменил(а) свой запрос на {amount}."
                else:
                    note = (
                        f"{actor} не подтвердил(а) получение {amount}."
                        " Долг остаётся — свяжитесь и разберитесь."
                    )
                await query.answer("Запрос отменён")

            try:
                await ctx.bot.send_message(other, note)
            except Exception:
                logger.info("failed to notify %s about a payment claim", other)
            await self._show_debts(update, ctx)
            return

        # Group settings
        if data.startswith("gset|"):
            gid = int(data[len("gset|") :])
            if not self.repo.is_group_member(gid, uid):
                await query.answer("Нет доступа", show_alert=True)
                return
            await self._send_group_settings(update, ctx, gid)
            await query.answer()
            return

        if data.startswith("grename|") or data.startswith("gcurother|"):
            gid = int(data.split("|", 1)[1])
            if not self.repo.is_group_owner(gid, uid):
                await query.answer("Только владелец группы", show_alert=True)
                return
            kind = "rename" if data.startswith("grename|") else "currency"
            _set_group_input(ctx, {"group_id": gid, "kind": kind})
            await query.answer()
            prompt = (
                "Пришлите новое название группы одним сообщением."
                if kind == "rename"
                else "Пришлите код валюты, напр. NOK."
            )
            await self._edit_or_send(update, prompt, inline_cancel())
            return

        if data.startswith("gcur|"):
            gid = int(data[len("gcur|") :])
            if not self.repo.is_group_owner(gid, uid):
                await query.answer("Только владелец группы", show_alert=True)
                return
            await self._send_currency_picker(update, gid)
            await query.answer()
            return

        if data.startswith("gcurset|"):
            parts = data.split("|")
            if len(parts) == 3:
                gid = int(parts[1])
                if not self.repo.is_group_owner(gid, uid):
                    await query.answer("Только владелец группы", show_alert=True)
                    return
                if self.repo.set_group_currency(gid, parts[2]):
                    await query.answer(f"Валюта: {parts[2]}")
                else:
                    await query.answer(
                        "Валюту уже не сменить: в группе есть траты",
                        show_alert=True,
                    )
                await self._send_group_settings(update, ctx, gid)
                return
            await query.answer("Ошибка")
            return

        if data.startswith("gleave|"):
            gid = int(data[len("gleave|") :])
            if not self.repo.is_group_member(gid, uid):
                await query.answer("Нет доступа", show_alert=True)
                return
            balance = self.repo.member_balance(gid, uid)
            if balance:
                await query.answer(
                    "Сначала закройте долги в этой группе", show_alert=True
                )
                return
            warning = ""
            if self.repo.is_group_owner(gid, uid):
                heir = self.repo.next_owner(gid, uid)
                warning = (
                    f"\nВы владелец: группа перейдёт к {self.repo.user_name(heir)}."
                    if heir
                    else "\nВы последний участник: группа будет удалена"
                    " вместе со всеми тратами."
                )
            await self._edit_or_send(
                update,
                f"Выйти из группы #{gid}?"
                f" Ваши прошлые траты останутся в истории.{warning}",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Да, выйти", callback_data=f"gleaveyes|{gid}"
                            )
                        ],
                        [InlineKeyboardButton("Отмена", callback_data=f"gset|{gid}")],
                    ]
                ),
            )
            await query.answer()
            return

        if data.startswith("gleaveyes|"):
            gid = int(data[len("gleaveyes|") :])
            result = await self._leave_group(update, ctx, gid)
            if result == "has_debt":
                await query.answer(
                    "Сначала закройте долги в этой группе", show_alert=True
                )
                return
            if result == "not_member":
                await query.answer("Вы уже не в группе", show_alert=True)
                return
            await query.answer("Готово")
            await self._send_group_picker(update, ctx, "mg")
            return

        # Owner removes a member
        if data.startswith("memdel|"):
            parts = data.split("|")
            if (
                len(parts) >= 4
                and parts[2].startswith("gid:")
                and parts[3].startswith("p:")
            ):
                target = int(parts[1])
                gid = int(parts[2][4:])
                page = int(parts[3][2:])
                if not self.repo.is_group_owner(gid, uid):
                    await query.answer("Только владелец группы", show_alert=True)
                    return
                reason = self.repo.remove_member(gid, target)
                if reason == "has_debt":
                    await query.answer(
                        "У участника есть незакрытый баланс", show_alert=True
                    )
                    return
                if reason:
                    await query.answer("Не получилось удалить", show_alert=True)
                    return
                try:
                    await ctx.bot.send_message(
                        target,
                        f"Вас удалили из группы #{gid}"
                        f" ({self.repo.get_group_title(gid)}).",
                    )
                except Exception:
                    logger.info("failed to notify %s about removal", target)
                await query.answer("Участник удалён")
                await self._send_members_page(update, gid, page)
                return
            await query.answer("Ошибка")
            return

        if data.startswith("memnote|"):
            await query.answer(
                "Сначала нужно закрыть его долги в этой группе", show_alert=True
            )
            return

        # Delete group (owner only)
        if data.startswith("grpdel|"):
            gid = int(data[len("grpdel|gid:") :])
            if not self.repo.can_delete_group(gid, uid):
                await query.answer("Только владелец может удалить группу")
                return
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Да, удалить", callback_data=f"grpdelyes|gid:{gid}"
                        )
                    ],
                    [InlineKeyboardButton("Отмена", callback_data=f"mgsel|{gid}")],
                ]
            )
            await self._edit_or_send(
                update,
                f"Точно удалить группу #{gid}? Это удалит все её данные.",
                markup,
            )
            await query.answer()
            return

        if data.startswith("grpdelyes|"):
            gid = int(data[len("grpdelyes|gid:") :])
            if not self.repo.can_delete_group(gid, uid):
                await query.answer(
                    "Только владелец может удалить группу", show_alert=True
                )
                return
            try:
                self.repo.delete_group(gid)
                await query.answer("Группа удалена")
                await self._send_group_picker(update, ctx, "mg")
                return
            except Exception:
                logger.exception("failed to delete group")
            await query.answer("Ошибка удаления группы")
            return

        # Members screen
        if data.startswith("members|"):
            parts = data.split("|")
            if len(parts) >= 3 and parts[2].startswith("p:"):
                gid = int(parts[1])
                page = int(parts[2][2:])
                if self.repo.is_group_member(gid, uid):
                    await self._send_members_page(update, gid, page)
                else:
                    await query.answer("Нет доступа", show_alert=True)
                    return
            await query.answer()
            return

        # Settlement history
        if data.startswith("setlist|"):
            parts = data.split("|")
            if len(parts) >= 3 and parts[2].startswith("p:"):
                gid = int(parts[1])
                page = int(parts[2][2:])
                if self.repo.can_view_settlements(gid, uid):
                    await self._send_settlements_page(update, gid, page)
                else:
                    await query.answer("Нет доступа", show_alert=True)
                    return
            await query.answer()
            return

        if data.startswith("setdel|"):
            parts = data.split("|")
            if (
                len(parts) >= 4
                and parts[2].startswith("gid:")
                and parts[3].startswith("p:")
            ):
                settlement_id = int(parts[1])
                gid = int(parts[2][4:])
                page = int(parts[3][2:])
                try:
                    if not self.repo.can_delete_settlement(settlement_id, gid, uid):
                        await query.answer("Нет прав на отмену", show_alert=True)
                        return
                    self.repo.delete_settlement(settlement_id, gid)
                    await query.answer("Платеж отменен")
                    await self._send_settlements_page(update, gid, page)
                    return
                except Exception:
                    logger.exception("failed to delete settlement")
            await query.answer("Ошибка отмены платежа")
            return

        # Add-expense flow callbacks
        st = _get_ae(ctx)
        if st is None:
            # The wizard is gone — cancelled, finished, or from an older
            # session. Say so: the buttons are still on screen, and a tap
            # that does nothing at all reads as a broken bot.
            await query.answer("Эта кнопка больше не активна", show_alert=True)
            return
        group_id = st.get("group_id")
        if not self.repo.is_group_member(group_id, uid):
            _del_ae(ctx)
            await query.answer("Нет доступа", show_alert=True)
            return

        # Keep the rate the bot proposed
        if data.startswith("expsave|") or data.startswith("review|"):
            parts = data.split("|")
            if st.get("step") != "confirm" or parts[-1] != st.get("confirm_token"):
                await query.answer("Эта проверка уже устарела", show_alert=True)
                return
            await query.answer()
            if parts[0] == "expsave":
                await self._finalize_expense(update, ctx, st, save=True)
            else:
                field = parts[1]
                st.pop("confirm_token", None)
                if field == "amount":
                    st["step"] = "await_amount_desc"
                    st["orig_currency"] = ""
                    st["orig_amount_cents"] = 0
                    await self._edit_or_send(
                        update,
                        "Пришлите сумму и описание, например: 1200 обед",
                        inline_cancel(),
                    )
                elif field == "payer":
                    st["step"] = "choose_payer"
                    await self._ask_payer(update, ctx, st)
                elif field == "participants":
                    st["step"] = "choose_participants"
                    await self._ask_participants(update, ctx, st)
                else:
                    st["step"] = "choose_split"
                    await self._ask_split_mode(update, ctx, st)
                _set_ae(ctx, st)
            return

        if data.startswith("customremain|"):
            if st.get("step") != "await_custom_share" or st.get("custom_left") != [
                int(data.split("|")[1])
            ]:
                await query.answer("Этот шаг уже завершён", show_alert=True)
                return
            remaining = st["amount_cents"] - sum(st["custom_shares"].values())
            await query.answer()
            await self._flow_add_expense(
                update, ctx, st, f"{remaining // 100}.{remaining % 100:02d}"
            )
            return

        expected_step = None
        if data.startswith("payer|"):
            expected_step = "choose_payer"
        elif data.startswith("toggle|") or data in {
            "part_all",
            "part_clear",
            "part_done",
            "part_me_payer",
        }:
            expected_step = "choose_participants"
        elif data.startswith("split|"):
            expected_step = "choose_split"
        if expected_step and st.get("step") != expected_step:
            await query.answer("Этот шаг уже завершён", show_alert=True)
            return

        if data == "rateok":
            st = _get_ae(ctx)
            if st is None or st.get("step") != "confirm_rate":
                await query.answer("Эта кнопка больше не активна", show_alert=True)
                return
            st["step"] = "choose_payer"
            _set_ae(ctx, st)
            await query.answer()
            await self._ask_payer(update, ctx, st)
            return

        if data.startswith("payer|"):
            payer = int(data[len("payer|") :])
            if not self.repo.is_group_member(group_id, payer):
                await query.answer("Нет доступа", show_alert=True)
                return
            st["payer"] = payer
            st["step"] = "choose_participants"
            _set_ae(ctx, st)
            await query.answer("Плательщик выбран")
            await self._ask_participants(update, ctx, st)
            return

        if data.startswith("toggle|"):
            pid = int(data[len("toggle|") :])
            if not self.repo.is_group_member(group_id, pid):
                await query.answer("Нет доступа", show_alert=True)
                return
            st["participants"][pid] = not st["participants"].get(pid, False)
            _set_ae(ctx, st)
            await query.answer()
            await self._ask_participants(update, ctx, st)
            return

        if data == "part_all":
            members = self.repo.list_members(group_id)
            st["participants"] = {m["id"]: True for m in members}
            _set_ae(ctx, st)
            await query.answer("Выбраны все")
            await self._ask_participants(update, ctx, st)
            return

        if data == "part_me_payer":
            participants = {}
            if self.repo.is_group_member(group_id, uid):
                participants[uid] = True
            if self.repo.is_group_member(group_id, st["payer"]):
                participants[st["payer"]] = True
            st["participants"] = participants
            _set_ae(ctx, st)
            await query.answer("Выбраны вы и плательщик")
            await self._ask_participants(update, ctx, st)
            return

        if data == "part_clear":
            st["participants"] = {}
            _set_ae(ctx, st)
            await query.answer("Выбор очищен")
            await self._ask_participants(update, ctx, st)
            return

        if data == "part_done":
            if not any(st["participants"].values()):
                await query.answer(
                    "Выберите хотя бы одного участника!", show_alert=True
                )
                return
            st["step"] = "choose_split"
            _set_ae(ctx, st)
            await query.answer()
            await self._ask_split_mode(update, ctx, st)
            return

        if data == "split|equal":
            st["split_mode"] = "equal"
            st["step"] = "confirm"
            _set_ae(ctx, st)
            await query.answer("Поровну")
            await self._finalize_expense(update, ctx, st)
            return

        if data == "split|custom":
            st["split_mode"] = "custom"
            st["custom_left"] = [pid for pid, on in st["participants"].items() if on]
            st["custom_shares"] = {}
            st["step"] = "await_custom_share"
            _set_ae(ctx, st)
            await query.answer("Свои доли")
            await self._ask_next_custom(update, ctx, st)
            return

        await query.answer()

    # ---------- Screens ----------

    async def _send_group_details(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, gid: int
    ) -> None:
        uid = update.effective_user.id
        try:
            code = self.repo.get_invite_code(gid)
        except Exception:
            logger.exception("failed to load group details for group %s", gid)
            return

        bal = self.repo.compute_group_balances(gid)
        currency = self.repo.group_currency(gid)

        cmd = f"/join {code}"
        enc = quote(cmd, safe="")
        share = f"https://t.me/share/url?url={enc}"
        cmd_html = f'<a href="{share}">{html.escape(cmd)}</a>'

        you_owe, owe_you = [], []
        for (frm, to), v in bal.items():
            if v <= 0:
                continue
            if frm == uid:
                you_owe.append(
                    f"вы → {html.escape(self.repo.user_name(to))}:"
                    f" {format_cents(v, currency)}"
                )
            elif to == uid:
                owe_you.append(
                    f"{html.escape(self.repo.user_name(frm))} → вам:"
                    f" {format_cents(v, currency)}"
                )
        you_owe.sort()
        owe_you.sort()

        lines = [
            f"<b>{html.escape(self.repo.get_group_title(gid))}</b>"
            f" · #{gid} · {currency}\n",
            f"Приглашение: {cmd_html}\n\n",
        ]
        if not you_owe and not owe_you:
            lines.append("В этой группе долгов нет 🎉")
        else:
            if you_owe:
                lines.append("Вы должны:\n• " + "\n• ".join(you_owe) + "\n")
            if owe_you:
                lines.append("Вам должны:\n• " + "\n• ".join(owe_you))
            lines.append(
                "\n\nЭто долги только по этой группе. Платить нужно по"
                " итогу всех групп — откройте «💰 Долги»."
            )
        text = "".join(lines)

        # Adding an expense is the reason people open a group, so it leads.
        # Debts are deliberately absent: that screen spans every group and
        # lives on the keyboard below, and offering it here reads as if it
        # showed this group alone. Members sit in the settings, next to the
        # buttons that change them.
        rows = [
            [InlineKeyboardButton("🧾 Добавить трату", callback_data=f"aesel|{gid}")],
            [
                InlineKeyboardButton("📋 Траты", callback_data=f"explist|{gid}|p:0"),
                InlineKeyboardButton("💸 Платежи", callback_data=f"setlist|{gid}|p:0"),
            ],
            [
                InlineKeyboardButton("🔗 Пригласить", url=share),
                InlineKeyboardButton("📊 Excel", callback_data=f"xlsx|{gid}"),
            ],
            [
                InlineKeyboardButton("⚙️ Настройки", callback_data=f"gset|{gid}"),
                InlineKeyboardButton("« Группы", callback_data="mg|p:0"),
            ],
        ]

        markup = InlineKeyboardMarkup(rows)
        opts = dict(
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=markup,
        )
        if update.callback_query and update.callback_query.message:
            await update.callback_query.message.edit_text(text, **opts)
        else:
            await update.effective_chat.send_message(text, **opts)

    async def _send_group_settings(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, gid: int
    ) -> None:
        uid = update.effective_user.id
        title = self.repo.get_group_title(gid)
        currency = self.repo.group_currency(gid)
        is_owner = self.repo.is_group_owner(gid, uid)
        balance = self.repo.member_balance(gid, uid)

        lines = [
            f"Настройки группы #{gid}\n",
            f"Название: {title}\n",
            f"Валюта: {currency}\n",
            f"Вы: {'владелец' if is_owner else 'участник'}\n",
        ]
        if balance:
            side = "вам должны" if balance > 0 else "вы должны"
            lines.append(
                f"\nВаш баланс здесь: {side} {format_cents(abs(balance), currency)}."
                " Выйти можно только с нулевым балансом.\n"
            )
        if not self.repo.can_change_currency(gid):
            lines.append(
                "\nВалюту уже не сменить: в группе есть траты или платежи,"
                " все суммы записаны в текущей валюте.\n"
            )

        rows = []
        if is_owner:
            rows.append(
                [
                    InlineKeyboardButton(
                        "✏️ Переименовать", callback_data=f"grename|{gid}"
                    )
                ]
            )
            if self.repo.can_change_currency(gid):
                rows.append(
                    [
                        InlineKeyboardButton(
                            f"💱 Валюта: {currency}", callback_data=f"gcur|{gid}"
                        )
                    ]
                )
        rows.append(
            [InlineKeyboardButton("👥 Участники", callback_data=f"members|{gid}|p:0")]
        )
        rows.append(
            [InlineKeyboardButton("🚪 Выйти из группы", callback_data=f"gleave|{gid}")]
        )
        if is_owner:
            rows.append(
                [
                    InlineKeyboardButton(
                        "🗑 Удалить группу", callback_data=f"grpdel|gid:{gid}"
                    )
                ]
            )
        rows.append(
            [InlineKeyboardButton("« Назад к группе", callback_data=f"mgsel|{gid}")]
        )

        await self._edit_or_send(update, "".join(lines), InlineKeyboardMarkup(rows))

    async def _send_currency_picker(self, update: Update, gid: int) -> None:
        current = self.repo.group_currency(gid)
        common = ["RUB", "USD", "EUR", "TRY", "GEL", "KZT", "AMD", "RSD", "THB", "AED"]
        rows, row = [], []
        for code in common:
            mark = "✅ " if code == current else ""
            row.append(
                InlineKeyboardButton(
                    f"{mark}{code}", callback_data=f"gcurset|{gid}|{code}"
                )
            )
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(
            [
                InlineKeyboardButton(
                    "Другая — ввести код", callback_data=f"gcurother|{gid}"
                )
            ]
        )
        rows.append([InlineKeyboardButton("« Назад", callback_data=f"gset|{gid}")])
        await self._edit_or_send(
            update,
            f"Валюта группы #{gid}. Сейчас: {current}.\n"
            "Все суммы в группе будут считаться в ней; сменить получится,"
            " только пока нет трат.",
            InlineKeyboardMarkup(rows),
        )

    async def _flow_group_input(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        state: dict,
        txt: str,
    ) -> None:
        gid, kind = state["group_id"], state["kind"]
        if not self.repo.is_group_owner(gid, update.effective_user.id):
            _del_group_input(ctx)
            await update.effective_chat.send_message(
                "Менять настройки может только владелец группы.",
                reply_markup=main_keyboard(),
            )
            return

        if kind == "rename":
            if not txt.strip() or txt.startswith("/"):
                await update.effective_chat.send_message(
                    "Название не может быть пустым. Пришлите новое название"
                    " одним сообщением.",
                    reply_markup=inline_cancel(),
                )
                return
            self.repo.rename_group(gid, txt)
            _del_group_input(ctx)
            await update.effective_chat.send_message(
                f"Группа #{gid} теперь называется «{self.repo.get_group_title(gid)}».",
                reply_markup=main_keyboard(),
            )
        else:
            code = normalize_currency(txt)
            if not code:
                await update.effective_chat.send_message(
                    "Не знаю такую валюту. Пришлите трёхбуквенный код, напр. NOK.",
                    reply_markup=inline_cancel(),
                )
                return
            if not self.repo.set_group_currency(gid, code):
                _del_group_input(ctx)
                await update.effective_chat.send_message(
                    "Валюту уже не сменить: в группе есть траты или платежи.",
                    reply_markup=main_keyboard(),
                )
                return
            _del_group_input(ctx)
            await update.effective_chat.send_message(
                f"Валюта группы #{gid}: {code}.", reply_markup=main_keyboard()
            )

        await self._send_group_settings(update, ctx, gid)

    async def _leave_group(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, gid: int
    ) -> str:
        uid = update.effective_user.id
        title = self.repo.get_group_title(gid)
        heir = (
            self.repo.next_owner(gid, uid) if self.repo.is_group_owner(gid, uid) else 0
        )
        result = self.repo.leave_group(gid, uid)
        if result in ("has_debt", "not_member"):
            return result

        name = self.repo.user_name(uid)
        if result == "last":
            await update.effective_chat.send_message(
                f"Вы вышли из группы «{title}». Участников не осталось,"
                " поэтому группа удалена."
            )
            return result

        for pid in (m["id"] for m in self.repo.list_members(gid)):
            note = f"{name} вышел(а) из группы #{gid} ({title})."
            if pid == heir:
                note += " Группа теперь ваша: вы её владелец."
            try:
                await ctx.bot.send_message(pid, note)
            except Exception:
                logger.info("failed to notify %s about a member leaving", pid)

        await update.effective_chat.send_message(
            f"Вы вышли из группы «{title}».", reply_markup=main_keyboard()
        )
        return result

    async def _send_group_workbook(self, update: Update, gid: int) -> None:
        """Hand the whole group ledger over as a spreadsheet.

        People check a split by laying the numbers out and adding them up,
        so the file carries the raw traces and shares, not just the debts
        the bot arrived at.
        """
        try:
            data = self.repo.export_group(gid)
        except Exception:
            logger.exception("failed to load export for group %s", gid)
            await update.effective_chat.send_message("Не удалось собрать выгрузку.")
            return

        if not data["expenses"] and not data["settlements"]:
            await update.effective_chat.send_message(
                "В группе пока нет трат — выгружать нечего."
            )
            return

        try:
            content = await asyncio.to_thread(
                build_group_workbook, data, self.repo.user_tz(update.effective_user.id)
            )
        except Exception:
            logger.exception("failed to build workbook for group %s", gid)
            await update.effective_chat.send_message("Не удалось собрать выгрузку.")
            return

        title = data["title"] or str(gid)
        await update.effective_chat.send_document(
            document=BytesIO(content),
            filename=export_filename(gid, title),
            caption=(
                f"Траты группы #{gid}: {title}\n"
                "«Траты» — все траты с долями каждого, «Итоги по людям» —"
                " сколько кто внёс и потратил, «Кто кому платит» — итоговые"
                " переводы. Как всё сходится, объяснено на листе"
                " «Как проверить»."
            ),
        )

    async def _send_expenses_page(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        gid: int,
        page: int,
        deleted: bool = False,
    ) -> None:
        page = max(0, page)
        total = self.repo.count_group_expenses(gid, deleted=deleted)
        prefix = "expdeleted" if deleted else "explist"
        switch = InlineKeyboardButton(
            "Активные траты" if deleted else "🗑 Удалённые траты",
            callback_data=f"{'explist' if deleted else 'expdeleted'}|{gid}|p:0",
        )
        offset = page * EXPENSES_PER_PAGE
        if total > 0 and offset >= total:
            page = 0
            offset = 0

        items = self.repo.list_group_expenses(
            gid, EXPENSES_PER_PAGE, offset, deleted=deleted
        )

        if total == 0:
            await self._edit_or_send(
                update,
                "Удалённых трат нет." if deleted else "В группе пока нет трат.",
                InlineKeyboardMarkup(
                    [
                        [switch],
                        [
                            InlineKeyboardButton(
                                "Назад к группе", callback_data=f"mgsel|{gid}"
                            )
                        ],
                    ]
                ),
            )
            return

        currency = self.repo.group_currency(gid)
        names = self.repo.names_for(
            {it["payer"] for it in items}
            | {it["created_by"] for it in items if it["created_by"]}
        )
        lines = [
            f"{'Удалённые траты' if deleted else 'Траты'} группы #{gid} (страница {page + 1}), валюта {currency}:\n"
        ]
        for it in items:
            paid_in = ""
            edited = " (изменена)" if it["updated_at"] else ""
            if it["orig_currency"]:
                paid_in = (
                    f" [оплачено"
                    f" {format_cents(it['orig_amount_cents'], it['orig_currency'])}]"
                )
            creator = names.get(it["created_by"], "—") if it["created_by"] else "—"
            lines.append(
                f"• #{it['id']} {it['desc']} —"
                f" {format_cents(it['amount_cents'], currency)}{paid_in}"
                f" (плательщик: {names[it['payer']]}, создал: {creator}){edited}\n"
            )

        # A row per expense opens its card, where the split, the receipt
        # and the edit and delete buttons live. The old screen could only
        # offer "delete", which put the riskiest action one tap away.
        rows = []
        for it in items:
            mark = " 🧾" if it["receipt"] else ""
            rows.append(
                [
                    InlineKeyboardButton(
                        f"#{it['id']} {it['desc']} —"
                        f" {format_cents(it['amount_cents'], currency)}{mark}",
                        callback_data=f"expcard|{it['id']}|gid:{gid}|p:{page}",
                    )
                ]
            )
        nav = [InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")]
        if page > 0:
            nav.insert(
                0,
                InlineKeyboardButton(
                    "« Назад", callback_data=f"{prefix}|{gid}|p:{page - 1}"
                ),
            )
        if offset + EXPENSES_PER_PAGE < total:
            nav.append(
                InlineKeyboardButton(
                    "Вперёд »", callback_data=f"{prefix}|{gid}|p:{page + 1}"
                )
            )
        rows.append([switch])
        rows.append(nav)

        await self._edit_or_send(update, "".join(lines), InlineKeyboardMarkup(rows))

    async def _send_expense_card(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        gid: int,
        eid: int,
        page: int,
    ) -> None:
        """One expense in full: the split, the receipt and what can be done.

        The list can only ever show a line per expense; this is where a
        person checks who was actually counted in.
        """
        uid = update.effective_user.id
        item = self.repo.get_expense(eid, gid)
        if item is None:
            deleted = self.repo.get_expense(eid, gid, include_deleted=True)
            rows = [
                [
                    InlineKeyboardButton(
                        "К списку трат", callback_data=f"explist|{gid}|p:{page}"
                    )
                ]
            ]
            if deleted:
                rows.insert(
                    0,
                    [
                        InlineKeyboardButton(
                            "🕓 История", callback_data=f"exphist|{eid}|{gid}|0"
                        )
                    ],
                )
                if deleted["created_by"] == uid:
                    rows.insert(
                        0,
                        [
                            InlineKeyboardButton(
                                "↩️ Восстановить",
                                callback_data=f"exprestore|{eid}|{gid}|0",
                            )
                        ],
                    )
            await self._edit_or_send(
                update,
                "Трата не найдена — возможно, её удалили.",
                InlineKeyboardMarkup(rows),
            )
            return

        currency = self.repo.group_currency(gid)
        tz = self.repo.user_tz(uid)
        names = self.repo.names_for(
            {item["payer"], *item["shares"]}
            | ({item["created_by"]} if item["created_by"] else set())
        )

        lines = [
            f"Трата #{item['id']}: {item['desc']}\n",
            f"Сумма: {format_cents(item['amount_cents'], currency)}\n",
        ]
        if item["orig_currency"]:
            rate = format_rate(
                item["amount_cents"],
                item["orig_amount_cents"],
                currency,
                item["orig_currency"],
            )
            lines.append(
                f"Оплачено:"
                f" {format_cents(item['orig_amount_cents'], item['orig_currency'])}"
                + (f" ({rate})\n" if rate else "\n")
            )
        lines.append(f"Плательщик: {names[item['payer']]}\n")
        if item["created_by"]:
            lines.append(f"Добавил(а): {names[item['created_by']]}\n")
        lines.append(f"Создана: {format_time(item['created_at'], tz)}\n")
        if item["updated_at"]:
            lines.append(f"Изменена: {format_time(item['updated_at'], tz)}\n")
        lines.append(f"Чек: {'приложен' if item['receipt'] else 'нет'}\n")
        lines.append("\nДоли:\n")
        for pid, share in sorted(
            item["shares"].items(), key=lambda kv: names[kv[0]].lower()
        ):
            lines.append(f"• {names[pid]}: {format_cents(share, currency)}\n")

        rows = [
            [
                InlineKeyboardButton(
                    "🕓 История изменений", callback_data=f"exphist|{eid}|{gid}|0"
                )
            ]
        ]
        if item["receipt"]:
            rows.append(
                [
                    InlineKeyboardButton(
                        "🧾 Показать чек",
                        callback_data=f"expshow|{eid}|gid:{gid}|p:{page}",
                    )
                ]
            )
        if self.repo.can_edit_expense(eid, gid, uid):
            rows.append(
                [
                    InlineKeyboardButton(
                        "✏️ Изменить", callback_data=f"expedit|{eid}|gid:{gid}|p:{page}"
                    )
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        "📎 Заменить чек" if item["receipt"] else "📎 Приложить чек",
                        callback_data=f"exprcpt|{eid}|gid:{gid}|p:{page}",
                    )
                ]
            )
            if item["receipt"]:
                rows.append(
                    [
                        InlineKeyboardButton(
                            "🗑 Убрать чек",
                            callback_data=f"exprdel|{eid}|gid:{gid}|p:{page}",
                        )
                    ]
                )
            rows.append(
                [
                    InlineKeyboardButton(
                        "🗑 Удалить трату",
                        callback_data=f"expdel|{eid}|gid:{gid}|p:{page}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    "« К списку трат", callback_data=f"explist|{gid}|p:{page}"
                )
            ]
        )

        await self._edit_or_send(update, "".join(lines), InlineKeyboardMarkup(rows))

    async def _start_expense_edit(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        gid: int,
        eid: int,
        page: int,
    ) -> None:
        """Re-run the add-expense wizard over an existing expense.

        Editing an amount without redoing the split would leave the shares
        adding up to the old total, so the whole thing is entered again —
        prefilled, and saved back onto the same expense.
        """
        item = self.repo.get_expense(eid, gid)
        if item is None:
            await self._edit_or_send(update, "Трата не найдена.")
            return

        currency = self.repo.group_currency(gid)
        _set_ae(
            ctx,
            {
                "group_id": gid,
                "expense_id": eid,
                "expected_revision": item["revision"],
                "page": page,
                "amount_cents": 0,
                "description": "",
                "payer": 0,
                "participants": {},
                "split_mode": "",
                "custom_left": [],
                "custom_shares": {},
                "currency": currency,
                "orig_currency": "",
                "orig_amount_cents": 0,
                "step": "await_amount_desc",
            },
        )
        current = format_cents(item["amount_cents"], currency)
        await update.effective_chat.send_message(
            f"Меняем трату #{eid}. Сейчас: {item['desc']} — {current}.\n"
            f"Пришлите новую сумму и описание одним сообщением"
            f" (валюта группы — {currency}).",
            reply_markup=inline_cancel(),
        )

    async def _send_expense_history(self, update, gid, eid, page=0):
        page = max(0, page)
        uid = update.effective_user.id
        events = self.repo.expense_history(eid, gid, uid, limit=6, offset=page * 5)
        currency = self.repo.group_currency(gid)
        tz = self.repo.user_tz(uid)
        lines = [f"История траты #{eid}\n"]
        actions = {
            "create": "Добавлена",
            "edit": "Изменена",
            "delete": "Удалена",
            "restore": "Восстановлена",
            "receipt": "Изменён чек",
        }
        for event in events[:5]:
            before = json.loads(event["before_json"]) if event["before_json"] else {}
            after = json.loads(event["after_json"])
            lines.append(
                f"\n{format_time(event['created_at'], tz)} · {self.repo.user_name(event['actor_tg_id'])}: {actions.get(event['action'], event['action'])}\n"
            )
            for key, label in [
                ("desc", "Описание"),
                ("amount_cents", "Сумма"),
                ("payer", "Плательщик"),
                ("orig_currency", "Исходная валюта"),
                ("orig_amount_cents", "Оплачено"),
                ("receipt", "Чек"),
            ]:
                if before.get(key) == after.get(key):
                    continue

                def show(snapshot):
                    value = snapshot.get(key)
                    if value is None:
                        return "—"
                    if key == "amount_cents":
                        return format_cents(value, currency)
                    if key == "orig_amount_cents":
                        return format_cents(value, snapshot.get("orig_currency", ""))
                    if key == "payer":
                        return self.repo.user_name(value)
                    if key == "receipt":
                        return "приложен" if value else "нет"
                    return str(value) or "—"

                # Two different receipt IDs should be described as a replacement.
                if key == "receipt" and before.get(key) and after.get(key):
                    lines.append("Чек: заменён\n")
                else:
                    lines.append(f"{label}: {show(before)} → {show(after)}\n")
            old_shares, new_shares = before.get("shares", {}), after.get("shares", {})
            for pid in sorted(set(old_shares) | set(new_shares)):
                if old_shares.get(pid) != new_shares.get(pid):
                    lines.append(
                        f"Доля {self.repo.user_name(int(pid))}: {format_cents(old_shares.get(pid, 0), currency)} → {format_cents(new_shares.get(pid, 0), currency)}\n"
                    )
        if not events:
            lines.append(
                "Изменений пока нет. История записывается с обновления бота.\n"
            )
        rows, navigation = [], []
        if page:
            navigation.append(
                InlineKeyboardButton(
                    "←", callback_data=f"exphist|{eid}|{gid}|{page - 1}"
                )
            )
        if len(events) > 5:
            navigation.append(
                InlineKeyboardButton(
                    "→", callback_data=f"exphist|{eid}|{gid}|{page + 1}"
                )
            )
        if navigation:
            rows.append(navigation)
        rows.append(
            [
                InlineKeyboardButton(
                    "К трате", callback_data=f"expcard|{eid}|gid:{gid}|p:0"
                )
            ]
        )
        await self._edit_or_send(update, "".join(lines), InlineKeyboardMarkup(rows))

    async def _notify_expense_change(self, ctx, gid, before, after, actor, verb):
        before = before or {}
        old_shares = before.get("shares", {}) if not before.get("deleted") else {}
        new_shares = after["shares"] if not after.get("deleted") else {}
        touched = (
            set(before.get("shares", {}))
            | set(after["shares"])
            | {before.get("payer"), after["payer"]}
        )
        currency = self.repo.group_currency(gid)
        for pid in touched - {None, actor}:
            text = (
                f"В группе #{gid} ({self.repo.get_group_title(gid)}) {verb} трата #{after['id']}: {after['desc']}.\n"
                f"Автор действия: {self.repo.user_name(actor)}.\n"
                f"Ваша доля: {format_cents(old_shares.get(pid, 0), currency)} → {format_cents(new_shares.get(pid, 0), currency)}.\n"
                f"Плательщик: {self.repo.user_name(after['payer'])}."
            )
            try:
                for chunk in split_message(text):
                    await ctx.bot.send_message(pid, chunk)
            except Exception:
                logger.warning(
                    "failed to notify expense participant %s", pid, exc_info=True
                )

    async def _flow_receipt(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Attach a photo the user sent to the expense they picked."""
        target = _get_receipt(ctx)
        message = update.effective_message
        photos = getattr(message, "photo", None)
        if not photos:
            await update.effective_chat.send_message(
                "Пришлите фото чека одной картинкой или нажмите /cancel."
            )
            return

        gid, eid = target["group_id"], target["expense_id"]
        if not self.repo.can_edit_expense(eid, gid, update.effective_user.id):
            _del_receipt(ctx)
            await update.effective_chat.send_message(
                "Чек может приложить только тот, кто добавил трату.",
                reply_markup=main_keyboard(),
            )
            return

        # The last entry is the largest rendition Telegram kept.
        self.repo.set_receipt(
            eid, gid, photos[-1].file_id, actor=update.effective_user.id
        )
        _del_receipt(ctx)
        await update.effective_chat.send_message(
            f"Чек приложен к трате #{eid}.", reply_markup=main_keyboard()
        )
        await self._send_expense_card(update, ctx, gid, eid, target.get("page", 0))

    async def _send_members_page(self, update: Update, gid: int, page: int) -> None:
        members = self.repo.list_members_detailed(gid)
        total = len(members)

        if total == 0:
            await self._edit_or_send(
                update,
                "В группе пока нет участников.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Назад к группе", callback_data=f"mgsel|{gid}"
                            )
                        ]
                    ]
                ),
            )
            return

        start = page * MEMBERS_PER_PAGE
        if start >= total:
            page = 0
            start = 0
        end = min(start + MEMBERS_PER_PAGE, total)

        lines = [f"Участники группы #{gid} ({total} всего), страница {page + 1}:\n"]
        for m in members[start:end]:
            role_mark = " 👑 владелец" if m["role"] == "owner" else ""
            lines.append(f"• {m['name']}{role_mark}\n")

        rows = []
        if self.repo.is_group_owner(gid, update.effective_user.id):
            currency = self.repo.group_currency(gid)
            balances = self.repo.member_balances(gid)
            for m in members[start:end]:
                if m["id"] == update.effective_user.id:
                    continue
                balance = balances.get(m["id"], 0)
                if balance:
                    rows.append(
                        [
                            InlineKeyboardButton(
                                f"{m['name']}: {format_cents(balance, currency)} — не удалить",
                                callback_data=f"memnote|{gid}|p:{page}",
                            )
                        ]
                    )
                else:
                    rows.append(
                        [
                            InlineKeyboardButton(
                                f"🚫 Удалить {m['name']}",
                                callback_data=f"memdel|{m['id']}|gid:{gid}|p:{page}",
                            )
                        ]
                    )

        nav = [InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")]
        if page > 0:
            nav.insert(
                0,
                InlineKeyboardButton(
                    "« Назад", callback_data=f"members|{gid}|p:{page - 1}"
                ),
            )
        if end < total:
            nav.append(
                InlineKeyboardButton(
                    "Вперёд »", callback_data=f"members|{gid}|p:{page + 1}"
                )
            )
        rows.append(nav)

        await self._edit_or_send(update, "".join(lines), InlineKeyboardMarkup(rows))

    async def _send_settlements_page(self, update: Update, gid: int, page: int) -> None:
        uid = update.effective_user.id
        total = self.repo.count_group_settlements(gid)
        offset = page * SETTLEMENTS_PER_PAGE
        if total > 0 and offset >= total:
            page = 0
            offset = 0

        items = self.repo.list_group_settlements(gid, SETTLEMENTS_PER_PAGE, offset)
        if total == 0:
            await self._edit_or_send(
                update,
                "В группе пока нет платежей.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Назад к группе", callback_data=f"mgsel|{gid}"
                            )
                        ]
                    ]
                ),
            )
            return

        viewer_tz = self.repo.user_tz(update.effective_user.id)
        currency = self.repo.group_currency(gid)
        names = self.repo.names_for(
            {i["from"] for i in items} | {i["to"] for i in items}
        )
        lines = [f"Платежи группы #{gid} (страница {page + 1}):\n"]
        for item in items:
            status = "" if item["confirmed"] else " — ждёт подтверждения"
            lines.append(
                f"• #{item['id']} {names[item['from']]} → "
                f"{names[item['to']]}: {format_cents(item['amount_cents'], currency)}"
                f" ({format_time(item['created_at'], viewer_tz)}){status}\n"
            )

        rows = []
        for item in items:
            if self.repo.can_delete_settlement(item["id"], gid, uid):
                rows.append(
                    [
                        InlineKeyboardButton(
                            f"Отменить #{item['id']}",
                            callback_data=f"setdel|{item['id']}|gid:{gid}|p:{page}",
                        )
                    ]
                )

        nav = [InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")]
        if page > 0:
            nav.insert(
                0,
                InlineKeyboardButton(
                    "« Назад", callback_data=f"setlist|{gid}|p:{page - 1}"
                ),
            )
        if offset + SETTLEMENTS_PER_PAGE < total:
            nav.append(
                InlineKeyboardButton(
                    "Вперёд »", callback_data=f"setlist|{gid}|p:{page + 1}"
                )
            )
        rows.append(nav)

        await self._edit_or_send(update, "".join(lines), InlineKeyboardMarkup(rows))

    # ---------- Add-expense sub-steps ----------

    async def _ask_payer(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, st: dict
    ) -> None:
        members = self.repo.list_members(st["group_id"])
        if not members:
            await self._edit_or_send(update, "В группе пока нет участников.")
            return
        btns = [
            [
                InlineKeyboardButton(
                    f"Плательщик: {m['name']}", callback_data=f"payer|{m['id']}"
                )
            ]
            for m in members
        ]
        btns.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_flow")])
        base = st.get("currency") or self.repo.group_currency(st["group_id"])
        paid_in = ""
        if st.get("orig_currency"):
            paid_in = (
                f" (оплачено"
                f" {format_cents(st['orig_amount_cents'], st['orig_currency'])})"
            )
        text = (
            f"Сумма: {format_cents(st['amount_cents'], base)}{paid_in}\n"
            f"Описание: {st['description']}\n"
            f"Выберите плательщика:"
        )
        await self._edit_or_send(update, text, InlineKeyboardMarkup(btns))

    async def _ask_participants(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, st: dict
    ) -> None:
        members = self.repo.list_members(st["group_id"])
        rows = []
        for m in members:
            on = st["participants"].get(m["id"], False)
            label = ("✅ " if on else "❌ ") + m["name"]
            rows.append(
                [InlineKeyboardButton(label, callback_data=f"toggle|{m['id']}")]
            )
        rows.append(
            [
                InlineKeyboardButton("Все", callback_data="part_all"),
                InlineKeyboardButton("Я и плательщик", callback_data="part_me_payer"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton("Очистить", callback_data="part_clear"),
                InlineKeyboardButton("Готово →", callback_data="part_done"),
            ]
        )
        rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_flow")])
        await self._edit_or_send(
            update,
            "Выберите участников (нажимайте, чтобы включить/исключить), затем «Готово».",
            InlineKeyboardMarkup(rows),
        )

    async def _ask_split_mode(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, st: dict
    ) -> None:
        btns = [
            [InlineKeyboardButton("Поровну", callback_data="split|equal")],
            [InlineKeyboardButton("Свои доли", callback_data="split|custom")],
        ]
        await self._edit_or_send(update, "Как разделить?", InlineKeyboardMarkup(btns))

    async def _finalize_expense(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, st: dict, save=False
    ) -> None:
        uid = update.effective_user.id
        group_id = st["group_id"]
        if not self.repo.is_group_member(group_id, uid):
            _del_ae(ctx)
            await self._edit_or_send(update, "Нет доступа к группе.")
            return

        if st["split_mode"] == "equal":
            participants = [pid for pid, on in st["participants"].items() if on]
            if not participants:
                _del_ae(ctx)
                await self._edit_or_send(
                    update, "Нет участников. Нажмите /cancel и начните заново."
                )
                return
            count = len(participants)
            base_share = st["amount_cents"] // count
            remainder = st["amount_cents"] - base_share * count
            shares = {}
            for pid in participants:
                sh = base_share
                if remainder > 0:
                    sh += 1
                    remainder -= 1
                shares[pid] = sh
        else:
            shares = st.get("custom_shares", {})
            if sum(shares.values()) != st["amount_cents"]:
                _del_ae(ctx)
                await self._edit_or_send(
                    update,
                    "Сумма долей не равна общей сумме. Нажмите /cancel и начните заново.",
                )
                return

        if not self.repo.is_group_member(group_id, st["payer"]) or any(
            not self.repo.is_group_member(group_id, pid) for pid in shares
        ):
            _del_ae(ctx)
            await self._edit_or_send(
                update, "Участник не найден в группе. Начните заново."
            )
            return

        editing = st.get("expense_id") or 0
        if editing and not self.repo.can_edit_expense(editing, group_id, uid):
            _del_ae(ctx)
            await self._edit_or_send(
                update, "Менять трату может только тот, кто её добавил."
            )
            return

        if not save:
            st["step"] = "confirm"
            st["confirm_token"] = rand_code()
            _set_ae(ctx, st)
            token = st["confirm_token"]
            currency = st.get("currency") or self.repo.group_currency(group_id)
            names = self.repo.names_for({st["payer"], *shares})
            text = (
                f"Проверьте {'изменения траты' if editing else 'трату'}\n"
                f"Группа: {self.repo.get_group_title(group_id)}\n"
                f"{st['description']} — {format_cents(st['amount_cents'], currency)}\n"
                f"Плательщик: {names[st['payer']]}\n\nДоли:\n"
                + "\n".join(
                    f"• {names[pid]}: {format_cents(amount, currency)}"
                    for pid, amount in shares.items()
                )
            )
            if st.get("orig_currency"):
                text += f"\nОплачено: {format_cents(st['orig_amount_cents'], st['orig_currency'])}"
            await self._edit_or_send(
                update,
                text,
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ Сохранить", callback_data=f"expsave|{token}"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "✏️ Сумма и описание",
                                callback_data=f"review|amount|{token}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "Плательщик", callback_data=f"review|payer|{token}"
                            ),
                            InlineKeyboardButton(
                                "Участники",
                                callback_data=f"review|participants|{token}",
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "Доли", callback_data=f"review|split|{token}"
                            ),
                            InlineKeyboardButton(
                                "❌ Отмена", callback_data="cancel_flow"
                            ),
                        ],
                    ]
                ),
            )
            return

        before = self.repo.get_expense(editing, group_id) if editing else None
        try:
            if editing:
                self.repo.update_expense(
                    editing,
                    group_id,
                    st["payer"],
                    st["description"],
                    st["amount_cents"],
                    shares,
                    orig_currency=st.get("orig_currency", ""),
                    orig_amount_cents=st.get("orig_amount_cents", 0),
                    actor=uid,
                    expected_revision=st.get("expected_revision"),
                )
                expense_id = editing
            else:
                expense_id = self.repo.create_expense(
                    group_id,
                    uid,
                    st["payer"],
                    st["description"],
                    st["amount_cents"],
                    shares,
                    orig_currency=st.get("orig_currency", ""),
                    orig_amount_cents=st.get("orig_amount_cents", 0),
                    operation_id=st.get("operation_id", ""),
                )
        except ValueError as e:
            await self._edit_or_send(update, str(e), inline_cancel())
            return
        except Exception:
            logger.exception("failed to save expense")
            await update.effective_chat.send_message(
                "Не удалось сохранить трату. Данные остались в черновике — попробуйте ещё раз."
            )
            await self._finalize_expense(update, ctx, st)
            return

        page = st.get("page", 0)
        _del_ae(ctx)

        base = st.get("currency") or self.repo.group_currency(group_id)
        paid_in = ""
        if st.get("orig_currency"):
            paid_in = (
                f" (оплачено"
                f" {format_cents(st['orig_amount_cents'], st['orig_currency'])})"
            )
        await self._notify_expense_change(
            ctx,
            group_id,
            before,
            self.repo.get_expense(expense_id, group_id),
            uid,
            "изменена" if editing else "добавлена",
        )
        await self._edit_or_send(
            update,
            f"Трата #{expense_id} {'изменена' if editing else 'добавлена'}."
            f" Сумма {format_cents(st['amount_cents'], base)}{paid_in},"
            f" плательщик {self.repo.user_name(st['payer'])}.",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Открыть трату",
                            callback_data=f"expcard|{expense_id}|gid:{group_id}|p:{page}",
                        )
                    ]
                ]
            ),
        )

    # ---------- Balance screens ----------

    def _debt_block(self, entry: dict, titles: dict) -> str:
        """Per-group detail under one counterparty, so the net is not a
        black box: it shows which group each part came from."""
        parts = []
        for gid, delta in sorted(entry["by_group"].items()):
            if delta == 0:
                continue
            title = titles.get(gid, "")
            side = "вам" if delta > 0 else "вы"
            parts.append(f"    #{gid} {title}: {side} {format_cents(abs(delta))}\n")
        return "".join(parts)

    def _pending_lines(self, uid: int, pending: list[dict], names: dict) -> list[str]:
        lines = []
        for info in pending:
            amount = format_cents(info["amount_cents"], info["currency"])
            if info["from"] == uid:
                lines.append(
                    f"• вы → {names[info['to']]}: {amount} — ждём подтверждения\n"
                )
            else:
                lines.append(
                    f"• {names[info['from']]} → вам: {amount} — подтвердите получение\n"
                )
        return lines

    async def _show_debts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id
        if not self.repo.list_user_groups(uid):
            await update.effective_chat.send_message(
                "У вас нет групп. Нажмите «Создать группу»."
            )
            return

        # Walking every group of every counterparty is the slowest thing the
        # bot does, and it blocks the event loop for everyone else.
        debts = await asyncio.to_thread(self.repo.compute_user_debts, uid)
        pending = self.repo.list_pending_settlements(uid)
        names = self.repo.names_for(
            set(debts) | {i["from"] for i in pending} | {i["to"] for i in pending}
        )
        titles = self.repo.group_titles(
            gid
            for per_currency in debts.values()
            for entry in per_currency.values()
            for gid in entry["by_group"]
        )

        you_owe, owe_you, even = [], [], []
        for other, per_currency in debts.items():
            name = names[other]
            for currency, entry in sorted(per_currency.items()):
                detail = self._debt_block(entry, titles)
                if not detail:
                    continue
                net = entry["net"]
                if net < 0:
                    you_owe.append((other, currency, -net, name, detail))
                elif net > 0:
                    owe_you.append((other, currency, net, name, detail))
                else:
                    even.append((other, currency, name, detail))
        you_owe.sort(key=lambda row: (row[3].lower(), row[1]))
        owe_you.sort(key=lambda row: (row[3].lower(), row[1]))
        even.sort(key=lambda row: (row[2].lower(), row[1]))

        # A claim already sent must not be offered again, or the same money
        # gets "paid" twice while the first request is still open.
        awaiting = {
            (info["to"] if info["from"] == uid else info["from"], info["currency"])
            for info in pending
        }

        lines = ["💰 Долги по всем группам\n"]
        if not you_owe and not owe_you and not even and not pending:
            lines.append("\nДолгов нет 🎉")
        if you_owe:
            lines.append("\nВы должны:\n")
            for _, currency, amount, name, detail in you_owe:
                lines.append(f"• {name} — {format_cents(amount, currency)}\n{detail}")
        if owe_you:
            lines.append("\nВам должны:\n")
            for _, currency, amount, name, detail in owe_you:
                lines.append(f"• {name} — {format_cents(amount, currency)}\n{detail}")
        if even:
            lines.append("\nВы в расчёте (долги погасили друг друга):\n")
            for _, currency, name, detail in even:
                lines.append(f"• {name} ({currency})\n{detail}")
        if pending:
            lines.append("\nОжидают подтверждения:\n")
            lines.extend(self._pending_lines(uid, pending, names))

        rows = []
        for info in pending:
            if info["to"] == uid:
                amount = format_cents(info["amount_cents"], info["currency"])
                rows.append(
                    [
                        InlineKeyboardButton(
                            f"✅ Получил(а) от {names[info['from']]}: {amount}",
                            callback_data=f"paycfm|{info['batch']}",
                        ),
                    ]
                )
                rows.append(
                    [
                        InlineKeyboardButton(
                            f"❌ Не получал(а) от {names[info['from']]}",
                            callback_data=f"payrej|{info['batch']}",
                        ),
                    ]
                )
            else:
                rows.append(
                    [
                        InlineKeyboardButton(
                            f"↩️ Отменить запрос к {names[info['to']]}",
                            callback_data=f"payrej|{info['batch']}",
                        )
                    ]
                )

        for other, currency, amount, name, _ in you_owe:
            if (other, currency) in awaiting:
                continue
            rows.append(
                [
                    InlineKeyboardButton(
                        f"Оплатил(а) {name}: {format_cents(amount, currency)}",
                        callback_data=f"paynet|to:{other}|cur:{currency}|amt:{amount}",
                    )
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        f"Отдал(а) часть {name} ({currency})",
                        callback_data=f"paypart|to:{other}|cur:{currency}",
                    )
                ]
            )
        for other, currency, name, _ in even:
            if (other, currency) in awaiting:
                continue
            rows.append(
                [
                    InlineKeyboardButton(
                        f"✅ Закрыть расчёты с {name} ({currency})",
                        callback_data=f"paynet|to:{other}|cur:{currency}|amt:0",
                    )
                ]
            )

        markup = InlineKeyboardMarkup(rows) if rows else None
        await self._edit_or_send(update, "".join(lines), markup)

    async def _request_payment(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        other: int,
        currency: str,
        amount: int,
    ) -> bool:
        """Send a payment claim to the person who is owed the money."""
        uid = update.effective_user.id
        batch = self.repo.request_settlement(uid, other, currency, amount)
        if not batch:
            return False

        from_name = self.repo.user_name(uid)
        shown = format_cents(amount, currency)
        if amount > 0:
            text = (
                f"{from_name} отметил(а), что отдал(а) вам {shown}.\n"
                "Подтвердите, что деньги получены — до этого долг остаётся."
            )
        else:
            text = (
                f"{from_name} предлагает закрыть взаимные расчёты ({currency}):"
                " долги погасили друг друга, переводить нечего.\n"
                "Подтвердите, если согласны."
            )
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Подтверждаю", callback_data=f"paycfm|{batch}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Не получал(а)", callback_data=f"payrej|{batch}"
                    )
                ],
            ]
        )
        try:
            await ctx.bot.send_message(other, text, reply_markup=markup)
        except Exception:
            logger.info("failed to notify %s about a payment claim", other)
        return True
