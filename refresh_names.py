"""Explicit one-off step: reread every known user's name from Telegram.

Names otherwise change only when their owner next talks to the bot or opens
the Mini App. Pass --dry-run to see the changes without storing them."""

import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from handlers import App


async def refresh(bot, db_path, dry_run=False):
    # mode=rw: a mistyped DB_PATH must fail, not create an empty database.
    conn = sqlite3.connect(
        Path(db_path).resolve().as_uri() + "?mode=rw", uri=True, timeout=10
    )
    changed = skipped = 0
    try:
        users = conn.execute("SELECT tg_id, name FROM users ORDER BY tg_id").fetchall()
        for uid, old in users:
            try:
                chat = await bot.get_chat(uid)
            except TelegramError as error:
                # Someone who never opened the bot or has blocked it. Only the
                # error type: Telegram messages may carry request URLs.
                skipped += 1
                print(f"{uid}: {old} — skipped ({type(error).__name__})")
                continue
            new = App._best_name(chat)
            if new != old:
                changed += 1
                print(f"{uid}: {old} -> {new}")
                if not dry_run:
                    # One short transaction each: the bot is using this file.
                    with conn:
                        conn.execute(
                            "UPDATE users SET name=? WHERE tg_id=?", (new, uid)
                        )
            await asyncio.sleep(0.05)
    finally:
        conn.close()
    print(
        f"{len(users)} users: {changed} {'to rename' if dry_run else 'renamed'},"
        f" {skipped} skipped"
    )
    return changed, skipped


async def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    async with Bot(os.environ["BOT_TOKEN"]) as bot:
        await refresh(
            bot, os.environ.get("DB_PATH", "./data.db"), "--dry-run" in sys.argv[1:]
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        # Do not print exception URLs: they may contain bot credentials.
        print(
            f"Name refresh failed ({type(error).__name__}). Check BOT_TOKEN and DB_PATH."
        )
        raise SystemExit(1) from None
