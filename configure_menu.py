"""Explicit deployment step: check HTTPS, then bind the Mini App menu to this bot."""

import asyncio
import logging
import os

import httpx
from telegram import Bot, MenuButtonWebApp, WebAppInfo

from miniapp import validate_url


async def configure():
    token = os.environ["BOT_TOKEN"]
    url = validate_url(os.environ["MINIAPP_URL"]) + "/"
    logging.getLogger("httpx").setLevel(logging.WARNING)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        response = await client.get(url + "healthz")
        response.raise_for_status()
        if response.json() != {"status": "ok", "app": "expensebot"}:
            raise RuntimeError("HTTPS endpoint did not return ExpenseBot health")
        page = await client.get(url)
        page.raise_for_status()
        if 'src="/app.js"' not in page.text:
            raise RuntimeError("Mini App is not available at the domain root")
        unauthenticated = await client.get(url + "api/me")
        if unauthenticated.status_code != 401:
            raise RuntimeError("API authorization check failed")
    async with Bot(token) as bot:
        me = await bot.get_me()
        expected = os.environ.get("EXPECTED_BOT_USERNAME", "").lstrip("@")
        if not expected or me.username.lower() != expected.lower():
            raise RuntimeError(
                f"Token belongs to @{me.username}; set EXPECTED_BOT_USERNAME after checking the new bot identity"
            )
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Расходы", web_app=WebAppInfo(url=url))
        )
        menu = await bot.get_chat_menu_button()
        if not isinstance(menu, MenuButtonWebApp) or menu.web_app.url != url:
            raise RuntimeError("Telegram menu verification failed")
        print(f"@{me.username}: {menu.text} -> {menu.web_app.url}")


if __name__ == "__main__":
    try:
        asyncio.run(configure())
    except Exception as error:
        # Do not print exception URLs: they may contain bot credentials.
        print(
            f"Menu setup failed ({type(error).__name__}). Check HTTPS and EXPECTED_BOT_USERNAME."
        )
        raise SystemExit(1) from None
