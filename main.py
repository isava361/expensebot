#!/usr/bin/env python3
"""Run the Telegram expense bot or manage verified database backups."""

import argparse
import asyncio
import logging
import os
from telegram import (
    Update,
    InlineKeyboardButton as InlineKeyboardButton,
    InlineKeyboardMarkup as InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)
from storage import restore_backup
from core import (
    GROUPS_PER_PAGE as GROUPS_PER_PAGE,
    EXPENSES_PER_PAGE as EXPENSES_PER_PAGE,
    MEMBERS_PER_PAGE as MEMBERS_PER_PAGE,
    SETTLEMENTS_PER_PAGE as SETTLEMENTS_PER_PAGE,
    MAX_AMOUNT_CENTS as MAX_AMOUNT_CENTS,
    MESSAGE_LIMIT as MESSAGE_LIMIT,
    KEYBOARD_VERSION as KEYBOARD_VERSION,
    MIGRATIONS_DIR as MIGRATIONS_DIR,
    BASE_CURRENCY as BASE_CURRENCY,
    DEFAULT_CURRENCY as DEFAULT_CURRENCY,
    DEFAULT_TZ_OFFSET as DEFAULT_TZ_OFFSET,
    now_unix as now_unix,
    cents_from_str as cents_from_str,
    split_amount_currency_desc as split_amount_currency_desc,
    split_amount_and_description as split_amount_and_description,
    KNOWN_CURRENCIES as KNOWN_CURRENCIES,
    normalize_currency as normalize_currency,
    format_cents as format_cents,
    format_rate as format_rate,
    MAX_TZ_OFFSET_MIN as MAX_TZ_OFFSET_MIN,
    parse_tz_offset as parse_tz_offset,
    tz_label as tz_label,
    default_currency as default_currency,
    default_tz_offset_min as default_tz_offset_min,
    format_time as format_time,
    split_message as split_message,
    rand_code as rand_code,
    extract_start_code_from_text as extract_start_code_from_text,
    extract_bare_code as extract_bare_code,
    settle_net as settle_net,
)
from repository import Repo as Repo
from rates import Rates as Rates
from workbook import (
    build_group_workbook as build_group_workbook,
    build_xlsx as build_xlsx,
    _col_letter as _col_letter,
)
from handlers import (
    App as App,
    main_keyboard as main_keyboard,
    _TOP_BUTTONS as _TOP_BUTTONS,
    _LEGACY_DEBT_BUTTONS as _LEGACY_DEBT_BUTTONS,
    _LEGACY_GROUP_BUTTONS as _LEGACY_GROUP_BUTTONS,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Telegram expense bot and database backups"
    )
    parser.add_argument(
        "command", nargs="?", choices=["run", "backup", "restore"], default="run"
    )
    parser.add_argument("--output", help="New destination database (must not exist)")
    parser.add_argument("--backup", help="Backup to restore")
    args = parser.parse_args()
    db_path = os.environ.get("DB_PATH", "./data.db")
    if args.command != "run":
        if not args.output:
            parser.error("--output is required")
        source = args.backup if args.command == "restore" else db_path
        if not source:
            parser.error("--backup is required for restore")
        print(restore_backup(source, args.output))
        return

    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        raise RuntimeError("BOT_TOKEN env required")

    backup_interval = int(os.environ.get("BACKUP_INTERVAL", "86400"))
    if backup_interval <= 0:
        parser.error("BACKUP_INTERVAL must be positive")
    repo = Repo(db_path)
    bot_app = App(repo)
    backup_stop = asyncio.Event()
    backup_task = None

    async def periodic_backup():
        while not backup_stop.is_set():
            try:
                await asyncio.to_thread(repo.backup)
                logger.info("Database backup verified")
            except Exception:
                logger.exception("Database backup failed")
            try:
                await asyncio.wait_for(backup_stop.wait(), timeout=backup_interval)
            except asyncio.TimeoutError:
                pass

    async def post_init(application: Application) -> None:
        nonlocal backup_task
        me = await application.bot.get_me()
        bot_app.base = me.username
        logger.info("Starting bot @%s …", me.username)
        backup_task = asyncio.create_task(periodic_backup())

    async def post_stop(application: Application) -> None:
        backup_stop.set()
        if backup_task is not None:
            await backup_task

    async def on_error(update, ctx):
        logger.error(
            "Unhandled bot error",
            exc_info=(type(ctx.error), ctx.error, ctx.error.__traceback__),
        )
        if isinstance(update, Update) and update.effective_chat:
            try:
                await update.effective_chat.send_message(
                    "Не удалось завершить действие. Попробуйте ещё раз или откройте нужный раздел заново."
                )
            except Exception:
                logger.warning("Failed to send error notice")

    # Wizard state lives in user_data, so without persistence a restart in
    # the middle of adding an expense leaves the user answering questions
    # nobody is listening to any more.
    state_path = os.environ.get("STATE_PATH", "./bot_state.pickle")
    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_stop(post_stop)
        .persistence(PicklePersistence(filepath=state_path))
        .build()
    )

    application.add_handler(CommandHandler("start", bot_app.on_start))
    application.add_error_handler(on_error)
    application.add_handler(CommandHandler("join", bot_app.on_join))
    application.add_handler(CommandHandler("cancel", bot_app.on_cancel))
    application.add_handler(CommandHandler("tz", bot_app.on_tz))
    application.add_handler(CallbackQueryHandler(bot_app.on_callback))
    application.add_handler(MessageHandler(filters.PHOTO, bot_app.on_photo))
    # Use filters.TEXT (not ~filters.COMMAND) so that /join_<code> and
    # /start_<code> text patterns reach on_text; specific commands above
    # are consumed first within the same handler group.
    application.add_handler(MessageHandler(filters.TEXT, bot_app.on_text))

    try:
        # Money is involved: an expense someone sent while the bot was down
        # should still land, not vanish.
        application.run_polling(drop_pending_updates=False)
    finally:
        repo.close()


if __name__ == "__main__":
    main()
