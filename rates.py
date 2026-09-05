"""Cached currency conversion with a manual-entry fallback."""

import asyncio
import logging
import os
from typing import Optional
import httpx
from core import MAX_AMOUNT_CENTS, now_unix

logger = logging.getLogger(__name__)

# ---------- Exchange rates ----------
#
# open.er-api.com is free, needs no key and covers every currency the bot
# knows. Rates there move once a day, so one fetch per base currency serves
# everybody for hours.
#
# Every lookup is best-effort. A rate is a starting point, not the truth: the
# number that actually matters is what the payer's bank took, so a failed
# fetch simply falls back to asking them, and an automatic rate can always be
# overridden with the real amount.
RATES_URL = os.environ.get("RATES_URL", "https://open.er-api.com/v6/latest/{base}")
RATES_TTL = int(os.environ.get("RATES_TTL", "21600"))  # 6 hours
RATES_TIMEOUT = 5.0


class Rates:
    """Daily exchange rates, cached per base currency."""

    def __init__(self, url: Optional[str] = None, ttl: int = 0):
        self._url = RATES_URL if url is None else url
        self._ttl = ttl or RATES_TTL
        # base -> (fetched_at, {currency: units per one base}, provider's date)
        self._cache: dict[str, tuple[int, dict, int]] = {}
        self._lock = asyncio.Lock()

    async def _fetch(self, base: str) -> tuple[dict, int]:
        async with httpx.AsyncClient(timeout=RATES_TIMEOUT) as client:
            response = await client.get(self._url.format(base=base))
            response.raise_for_status()
            payload = response.json()
        if payload.get("result") not in (None, "success"):
            raise ValueError(f"rate provider returned {payload.get('result')!r}")
        rates = payload.get("rates") or {}
        if not rates:
            raise ValueError("rate provider returned no rates")
        return rates, int(payload.get("time_last_update_unix") or now_unix())

    async def _rates_for(self, base: str) -> tuple[dict, int]:
        cached = self._cache.get(base)
        if cached and now_unix() - cached[0] < self._ttl:
            return cached[1], cached[2]
        async with self._lock:
            # Somebody may have fetched while this call waited for the lock;
            # without the second look a busy moment fires one request per user.
            cached = self._cache.get(base)
            if cached and now_unix() - cached[0] < self._ttl:
                return cached[1], cached[2]
            rates, updated = await self._fetch(base)
            self._cache[base] = (now_unix(), rates, updated)
            return rates, updated

    async def convert(
        self, amount_cents: int, orig: str, base: str
    ) -> Optional[tuple[int, float, int]]:
        """Convert ``orig`` into ``base``.

        Returns ``(amount in base, base per one orig, rate date)``, or None
        when there is no usable rate and the amount has to be asked for.
        """
        if not self._url or not orig or orig == base or amount_cents <= 0:
            return None
        try:
            rates, updated = await self._rates_for(base)
        except Exception as e:
            logger.info("no exchange rate for %s->%s: %s", orig, base, e)
            return None

        per_base = rates.get(orig)  # how much `orig` one unit of `base` buys
        if not isinstance(per_base, (int, float)) or per_base <= 0:
            return None
        rate = 1 / per_base
        converted = int(amount_cents * rate + 0.5)
        if converted <= 0 or converted > MAX_AMOUNT_CENTS:
            return None
        return converted, rate, updated
