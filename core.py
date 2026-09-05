"""Money arithmetic, parsing, formatting and shared configuration."""

import base64
import logging
import os
import re
import secrets
import time
from pathlib import Path

logger = logging.getLogger(__name__)

GROUPS_PER_PAGE = 5
EXPENSES_PER_PAGE = 10
MEMBERS_PER_PAGE = 15
SETTLEMENTS_PER_PAGE = 10
MAX_AMOUNT_CENTS = 1_000_000_000
# Telegram rejects anything longer; leave room for the ellipsis marker.
MESSAGE_LIMIT = 3900
# Bump whenever main_keyboard() changes: a reply keyboard lives on the client
# until the bot sends a new one, so users have to be pushed the new layout.
KEYBOARD_VERSION = 2
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
# Base currency a new group starts with; changeable while the group is empty.
BASE_CURRENCY = "RUB"
DEFAULT_CURRENCY = os.environ.get("DEFAULT_CURRENCY", BASE_CURRENCY).strip().upper()
# Offset new users start with, e.g. "+03:00". Everyone can change it with /tz.
DEFAULT_TZ_OFFSET = os.environ.get("DEFAULT_TZ_OFFSET", "0")

# ---------- Utils ----------

_AMOUNT_WITH_DESC_RE = re.compile(
    r"^\s*(?P<amount>[+-]?(?:(?:\d{1,3}(?:[ _]\d{3})+|\d+)(?:[.,]\d*)?|[.,]\d+))"
    r"(?=$|\s)(?:\s+(?P<desc>.*))?$"
)


def now_unix() -> int:
    return int(time.time())


def cents_from_str(s: str) -> int:
    """Parse monetary string to integer cents.

    Bug fix vs Go original: '12.' now correctly returns 1200 (not 12).
    The Go code failed to pad an empty frac string, so ParseInt('12') = 12 cents.
    """
    s = s.strip().replace(",", ".")
    if not s:
        raise ValueError("empty amount")

    negative = s.startswith("-")
    if s[0] in "+-":
        s = s[1:]
    if not s:
        raise ValueError("empty amount after sign")

    if " " in s or "_" in s:
        if not re.fullmatch(r"\d{1,3}(?:[ _]\d{3})+(?:\.\d*)?", s):
            raise ValueError("invalid thousands separator")
        s = s.replace(" ", "").replace("_", "")

    if s.count(".") > 1:
        raise ValueError("invalid amount")

    if "." in s:
        int_part, frac = s.split(".", 1)
        if not int_part and not frac:
            raise ValueError("empty amount")
        int_part = int_part or "0"
        if not int_part.isdigit() or (frac and not frac.isdigit()):
            raise ValueError("invalid amount")
        if len(frac) > 2:
            raise ValueError("too many decimal places")
        frac = (frac + "00")[:2]
        value = int(int_part) * 100 + int(frac)
    else:
        if not s.isdigit():
            raise ValueError("invalid amount")
        value = int(s) * 100

    value = -value if negative else value
    if abs(value) > MAX_AMOUNT_CENTS:
        raise ValueError("amount too large")
    return value


def split_amount_currency_desc(text: str) -> tuple[str, str, str]:
    """Split "1500 EUR ужин" into amount, currency and description.

    The currency is optional and may be glued to the number ("1500€") or
    stand as a code after it. An unrecognised word stays in the description,
    so nothing a person types is silently read as money in another currency.
    """
    prepared = text.strip()
    for symbol in _CURRENCY_SYMBOLS:
        if symbol in prepared:
            prepared = prepared.replace(symbol, f" {symbol} ")
    amount, rest = split_amount_and_description(" ".join(prepared.split()))
    if not amount:
        return "", "", ""

    head, _, tail = rest.partition(" ")
    currency = normalize_currency(head) if head else ""
    if currency:
        return amount, currency, tail.strip()
    return amount, "", rest


def split_amount_and_description(text: str) -> tuple[str, str]:
    match = _AMOUNT_WITH_DESC_RE.match(text)
    if not match:
        return "", ""
    amount = match.group("amount")
    desc = (match.group("desc") or "").strip()
    first_desc_token = desc.split(maxsplit=1)[0] if desc else ""
    plain_amount = amount.lstrip("+-")
    if (
        first_desc_token
        and " " not in amount
        and "_" not in amount
        and re.fullmatch(r"\d{1,3}", plain_amount)
        and re.fullmatch(r"\d[\d.,_]*", first_desc_token)
    ):
        return "", ""
    return amount, desc


# Codes the bot recognises after an amount. A closed list on purpose: a
# three-letter word after a number is far more often part of the description
# ("1500 gas") than a currency, and guessing wrong changes what people owe.
KNOWN_CURRENCIES = {
    "AED",
    "AMD",
    "AUD",
    "AZN",
    "BGN",
    "BRL",
    "BYN",
    "CAD",
    "CHF",
    "CNY",
    "CZK",
    "DKK",
    "EGP",
    "EUR",
    "GBP",
    "GEL",
    "HKD",
    "HUF",
    "IDR",
    "ILS",
    "INR",
    "JPY",
    "KGS",
    "KRW",
    "KZT",
    "MAD",
    "MDL",
    "MXN",
    "MYR",
    "NOK",
    "NZD",
    "PHP",
    "PLN",
    "RON",
    "RSD",
    "RUB",
    "SEK",
    "SGD",
    "THB",
    "TRY",
    "UAH",
    "USD",
    "UZS",
    "VND",
    "ZAR",
}

_CURRENCY_SYMBOLS = {
    "€": "EUR",
    "$": "USD",
    "₽": "RUB",
    "£": "GBP",
    "₺": "TRY",
    "¥": "JPY",
    "₾": "GEL",
    "₸": "KZT",
    "֏": "AMD",
    "₴": "UAH",
    "₪": "ILS",
    "₹": "INR",
    "₩": "KRW",
    "﷼": "AED",
    "฿": "THB",
    "₫": "VND",
    "zł": "PLN",
}


def normalize_currency(token: str) -> str:
    """Return the currency a token names, or "" if it names none."""
    cleaned = token.strip()
    if cleaned in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[cleaned]
    upper = cleaned.upper()
    return upper if upper in KNOWN_CURRENCIES else ""


def format_cents(c: int, currency: str = "") -> str:
    sign = ""
    if c < 0:
        sign = "-"
        c = -c
    amount = f"{sign}{c // 100}.{c % 100:02d}"
    return f"{amount} {currency}" if currency else amount


def format_rate(base_cents: int, orig_cents: int, base: str, orig: str) -> str:
    """How much base currency one unit of the paid currency cost."""
    if orig_cents <= 0:
        return ""
    return f"1 {orig} = {base_cents / orig_cents:.4f} {base}"


# A trip crosses time zones, so a timestamp only means something next to
# the offset it was rendered in. Telegram never tells us a user's zone, so
# people set their own and everyone else falls back to this default.
MAX_TZ_OFFSET_MIN = 14 * 60
_TZ_RE = re.compile(r"^(?P<sign>[+-])?(?P<hours>\d{1,2})(?::?(?P<minutes>\d{2}))?$")


def parse_tz_offset(text: str) -> int:
    """Read "+3", "-05:30", "0300" or "UTC+3" as minutes from UTC."""
    cleaned = text.strip().upper().replace("UTC", "").replace("GMT", "").strip()
    cleaned = cleaned.replace(" ", "")
    if not cleaned:
        raise ValueError("empty offset")
    m = _TZ_RE.match(cleaned)
    if not m:
        raise ValueError(f"bad offset: {text!r}")
    minutes = int(m.group("hours")) * 60 + int(m.group("minutes") or 0)
    if m.group("sign") == "-":
        minutes = -minutes
    if abs(minutes) > MAX_TZ_OFFSET_MIN:
        raise ValueError(f"offset out of range: {text!r}")
    return minutes


def tz_label(offset_min: int) -> str:
    sign = "-" if offset_min < 0 else "+"
    hours, minutes = divmod(abs(offset_min), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def default_currency() -> str:
    """The currency a new group starts in.

    Falls back to roubles rather than storing whatever the environment
    happens to hold: an unknown code would end up on the group as its base
    currency, labelling every amount in it with something meaningless.
    """
    code = normalize_currency(DEFAULT_CURRENCY)
    if not code:
        logger.warning(
            "bad DEFAULT_CURRENCY %r, using %s", DEFAULT_CURRENCY, BASE_CURRENCY
        )
        return BASE_CURRENCY
    return code


def default_tz_offset_min() -> int:
    try:
        return parse_tz_offset(DEFAULT_TZ_OFFSET)
    except ValueError:
        logger.warning("bad DEFAULT_TZ_OFFSET %r, using UTC", DEFAULT_TZ_OFFSET)
        return 0


def format_time(ts: int, offset_min: int = 0) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts + offset_min * 60))


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Cut a screen into Telegram-sized pieces on line boundaries.

    The debts screen grows with every group a person is in, and a message
    over the limit is not truncated by Telegram — it is refused, so the
    screen would simply never arrive.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # one absurdly long line, split it hard
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


def rand_code() -> str:
    b = secrets.token_bytes(8)
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


# ---------- Invite-code parsing ----------

_CODE_RE = re.compile(r"[a-zA-Z0-9\-_]+")


def extract_start_code_from_text(raw: str) -> str:
    s = raw.replace(" ", " ").replace(" ", " ").replace(" ", " ").strip()
    m = re.search(r"(?i)start=([a-zA-Z0-9\-_]+)", s)
    return m.group(1) if m else ""


def extract_bare_code(raw: str) -> str:
    s = raw.strip().replace("+", " ")
    if re.search(r"\s", s):
        return ""
    if not 6 <= len(s) <= 64:
        return ""
    return s if _CODE_RE.fullmatch(s) else ""


# ---------- Debt netting ----------


def settle_net(net: dict[int, int]) -> dict[tuple[int, int], int]:
    """Turn per-user net positions into a minimal set of directed debts.

    ``net`` maps a user id to their balance in cents: positive means the
    group owes them, negative means they owe the group. The sum over all
    users is zero.

    Debts are chained through intermediaries, not just netted pairwise: if
    A owes B and B owes C, B drops out and A pays C directly. The greedy
    largest-debtor/largest-creditor matching keeps the number of transfers
    small and is deterministic for a given ``net``, which matters because
    the payment buttons carry the computed amount and are re-validated
    against a freshly computed balance.
    """
    debtors = sorted(
        ((uid, -v) for uid, v in net.items() if v < 0),
        key=lambda t: (-t[1], t[0]),
    )
    creditors = sorted(
        ((uid, v) for uid, v in net.items() if v > 0),
        key=lambda t: (-t[1], t[0]),
    )

    result: dict[tuple[int, int], int] = {}
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        debtor, owed = debtors[i]
        creditor, due = creditors[j]
        amount = min(owed, due)
        key = (debtor, creditor)
        result[key] = result.get(key, 0) + amount
        owed -= amount
        due -= amount
        debtors[i] = (debtor, owed)
        creditors[j] = (creditor, due)
        if owed == 0:
            i += 1
        if due == 0:
            j += 1

    return result
