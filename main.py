#!/usr/bin/env python3
"""
Expense-splitting Telegram bot (python-telegram-bot v21+ + SQLite).

Requirements: pip install "python-telegram-bot>=21.0"

ENV:
  BOT_TOKEN=<telegram bot token>
  DB_PATH=./data.db
"""

import asyncio
import base64
import html
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
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
    "AED", "AMD", "AUD", "AZN", "BGN", "BRL", "BYN", "CAD", "CHF", "CNY",
    "CZK", "DKK", "EGP", "EUR", "GBP", "GEL", "HKD", "HUF", "IDR", "ILS",
    "INR", "JPY", "KGS", "KRW", "KZT", "MAD", "MDL", "MXN", "MYR", "NOK",
    "NZD", "PHP", "PLN", "RON", "RSD", "RUB", "SEK", "SGD", "THB", "TRY",
    "UAH", "USD", "UZS", "VND", "ZAR",
}

_CURRENCY_SYMBOLS = {
    "€": "EUR", "$": "USD", "₽": "RUB", "£": "GBP", "₺": "TRY", "¥": "JPY",
    "₾": "GEL", "₸": "KZT", "֏": "AMD", "₴": "UAH", "₪": "ILS", "₹": "INR",
    "₩": "KRW", "﷼": "AED", "฿": "THB", "₫": "VND", "zł": "PLN",
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
        logger.warning("bad DEFAULT_CURRENCY %r, using %s", DEFAULT_CURRENCY, BASE_CURRENCY)
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
    s = (
        raw.replace(" ", " ")
        .replace(" ", " ")
        .replace(" ", " ")
        .strip()
    )
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


# ---------- Repo ----------

# ---------- XLSX writing ----------
#
# The export has to open in Excel, so it is a real .xlsx: a zip holding the
# handful of XML parts a spreadsheet needs. Writing them here keeps the bot
# on its single dependency, and the file only ever carries text and numbers,
# which is the easy end of the format.

_XLSX_DEFAULT_STYLE = 0
_XLSX_HEADER_STYLE = 1
_XLSX_MONEY_STYLE = 2

# XML 1.0 cannot carry these at all, and descriptions are user input.
_XLSX_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SHEET_NAME_BAD_CHARS = re.compile(r"[\[\]:*?/\\]")


class Money:
    """A cents amount that lands in the sheet as a number Excel can sum.

    Written as text it would be a string in the column, and the whole point
    of the export is that people can add the numbers up themselves.
    """

    __slots__ = ("cents",)

    def __init__(self, cents: int) -> None:
        self.cents = cents


def _xml_text(value: str) -> str:
    return html.escape(_XLSX_BAD_CHARS.sub("", value), quote=False)


def _col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(ord("A") + rem) + name
    return name


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Money):
        return format_cents(value.cents)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _cell_xml(ref: str, value, style: int) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, Money):
        return f'<c r="{ref}" s="{_XLSX_MONEY_STYLE}"><v>{format_cents(value.cents)}</v></c>'
    if isinstance(value, int) and not isinstance(value, bool):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    if isinstance(value, float):
        return f'<c r="{ref}" s="{style}"><v>{value:.6f}</v></c>'
    return (
        f'<c r="{ref}" t="inlineStr" s="{style}">'
        f'<is><t xml:space="preserve">{_xml_text(str(value))}</t></is></c>'
    )


def _sheet_xml(header: list, rows: list[list]) -> str:
    all_rows = ([header] if header else []) + rows
    ncols = max((len(r) for r in all_rows), default=1) or 1

    widths = []
    for c in range(ncols):
        longest = max((len(_cell_text(r[c])) for r in all_rows if c < len(r)), default=0)
        widths.append(min(max(longest + 2, 9), 46))
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
        for i, w in enumerate(widths)
    )

    body = []
    for r_index, row in enumerate(all_rows, start=1):
        style = _XLSX_HEADER_STYLE if header and r_index == 1 else _XLSX_DEFAULT_STYLE
        cells = "".join(
            _cell_xml(f"{_col_letter(c)}{r_index}", value, style)
            for c, value in enumerate(row)
        )
        body.append(f'<row r="{r_index}">{cells}</row>')

    # Keep the header on screen while scrolling through a long trip.
    pane = (
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
    ) if header else ""

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{pane}<cols>{cols}</cols>"
        f"<sheetData>{''.join(body)}</sheetData></worksheet>"
    )


_XLSX_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="0.00"/></numFmts>'
    '<fonts count="2">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    "</fonts>"
    # Excel insists these two fills exist before any of its own.
    '<fills count="2">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    "</fills>"
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="3">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def _sheet_name(name: str, index: int) -> str:
    cleaned = _SHEET_NAME_BAD_CHARS.sub(" ", name).strip()[:31]
    return cleaned or f"Лист{index}"


def build_xlsx(sheets: list[tuple[str, list, list[list]]]) -> bytes:
    """Pack ``(name, header, rows)`` triples into an .xlsx file.

    Cells are ``str``, ``int``, ``Money`` or ``None``.
    """
    if not sheets:
        raise ValueError("a workbook needs at least one sheet")

    parts: dict[str, str] = {}
    sheet_files = []
    for i, (name, header, rows) in enumerate(sheets, start=1):
        path = f"xl/worksheets/sheet{i}.xml"
        parts[path] = _sheet_xml(header, rows)
        sheet_files.append((_sheet_name(name, i), i, path))

    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels"'
        ' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml"'
        ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml"'
        ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    ]
    for _, _, path in sheet_files:
        content_types.append(
            f'<Override PartName="/{path}"'
            ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    content_types.append("</Types>")
    parts["[Content_Types].xml"] = "".join(content_types)

    parts["_rels/.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1"'
        ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
        ' Target="xl/workbook.xml"/>'
        "</Relationships>"
    )

    parts["xl/workbook.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        + "".join(
            f'<sheet name="{_xml_text(name)}" sheetId="{i}" r:id="rId{i}"/>'
            for name, i, _ in sheet_files
        )
        + "</sheets></workbook>"
    )

    styles_rid = len(sheet_files) + 1
    parts["xl/_rels/workbook.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{i}"'
            ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"'
            f' Target="worksheets/sheet{i}.xml"/>'
            for _, i, _ in sheet_files
        )
        + f'<Relationship Id="rId{styles_rid}"'
        ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"'
        ' Target="styles.xml"/>'
        "</Relationships>"
    )

    parts["xl/styles.xml"] = _XLSX_STYLES

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, text in parts.items():
            # A fixed timestamp keeps identical data producing identical bytes.
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text.encode("utf-8"))
    return buf.getvalue()


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


class Repo:
    def __init__(self, db_path: str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        if not MIGRATIONS_DIR.exists():
            raise RuntimeError(f"migrations directory not found: {MIGRATIONS_DIR}")

        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations("
                "version TEXT PRIMARY KEY,"
                "applied_at INTEGER NOT NULL"
                ")"
            )
            applied = {
                row["version"]
                for row in self._conn.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = path.stem
                if version in applied:
                    continue
                if version == "002_expense_created_by":
                    self._migrate_expense_created_by()
                    self._conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (version, now_unix()),
                    )
                    continue
                sql = path.read_text(encoding="utf-8")
                self._conn.executescript(sql)
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                    (version, now_unix()),
                )
            self._conn.commit()

    def _column_exists(self, table: str, column: str) -> bool:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row["name"] == column for row in rows)

    def _migrate_expense_created_by(self) -> None:
        if not self._column_exists("expenses", "created_by_tg_id"):
            self._conn.execute("ALTER TABLE expenses ADD COLUMN created_by_tg_id INTEGER")
        self._conn.execute(
            "UPDATE expenses SET created_by_tg_id=payer_tg_id "
            "WHERE created_by_tg_id IS NULL"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- users --

    def upsert_user(self, tg_id: int, name: str) -> None:
        with self._lock:
            # New users are stamped with the current keyboard: they are shown
            # it by /start, so they must not be told it changed.
            self._conn.execute(
                "INSERT INTO users(tg_id,name,keyboard_version,tz_offset_min)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name",
                (tg_id, name, KEYBOARD_VERSION, default_tz_offset_min()),
            )
            self._conn.commit()

    def has_stale_keyboard(self, uid: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT keyboard_version FROM users WHERE tg_id=?", (uid,)
            ).fetchone()
        return row is not None and row["keyboard_version"] < KEYBOARD_VERSION

    def mark_keyboard_current(self, uid: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET keyboard_version=? WHERE tg_id=?",
                (KEYBOARD_VERSION, uid),
            )
            self._conn.commit()

    def names_for(self, ids) -> dict[int, str]:
        """Names for a whole screen in one query instead of one per row."""
        wanted = list({int(i) for i in ids})
        if not wanted:
            return {}
        placeholders = ",".join("?" * len(wanted))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT tg_id, name FROM users WHERE tg_id IN ({placeholders})",
                wanted,
            ).fetchall()
        found = {r["tg_id"]: r["name"] for r in rows}
        return {uid: found.get(uid, str(uid)) for uid in wanted}

    def user_tz(self, uid: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT tz_offset_min FROM users WHERE tg_id=?", (uid,)
            ).fetchone()
        return row["tz_offset_min"] if row else default_tz_offset_min()

    def set_user_tz(self, uid: int, offset_min: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET tz_offset_min=? WHERE tg_id=?", (offset_min, uid)
            )
            self._conn.commit()

    def user_name(self, uid: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM users WHERE tg_id=?", (uid,)
            ).fetchone()
        return row["name"] if row else str(uid)

    # -- groups --

    def create_group(self, title: str, owner: int, currency: str = "") -> tuple[int, str]:
        code = rand_code()
        currency = normalize_currency(currency) or default_currency()
        with self._lock:
            cur = self._conn.execute(
                'INSERT INTO "groups"(title,owner_tg_id,invite_code,created_at,currency)'
                " VALUES(?,?,?,?,?)",
                (title, owner, code, now_unix(), currency),
            )
            gid = cur.lastrowid
            self._conn.execute(
                "INSERT INTO group_members(group_id,tg_id,role,joined_at)"
                " VALUES(?,?,?,?)",
                (gid, owner, "owner", now_unix()),
            )
            self._conn.commit()
        return gid, code

    def join_by_code(self, code: str, uid: int) -> tuple[int, str]:
        with self._lock:
            row = self._conn.execute(
                'SELECT id,title FROM "groups" WHERE invite_code=?', (code,)
            ).fetchone()
            if row is None:
                raise ValueError("invalid invite code")
            gid, title = row["id"], row["title"]
            self._conn.execute(
                "INSERT INTO group_members(group_id,tg_id,role,joined_at)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(group_id,tg_id) DO NOTHING",
                (gid, uid, "member", now_unix()),
            )
            self._conn.commit()
        return gid, title

    def list_user_groups(self, uid: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT g.id,g.title FROM "groups" g'
                " JOIN group_members m ON g.id=m.group_id"
                " WHERE m.tg_id=? ORDER BY g.created_at DESC",
                (uid,),
            ).fetchall()
        return [{"id": r["id"], "title": r["title"]} for r in rows]

    def is_group_member(self, group_id: int, uid: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM group_members WHERE group_id=? AND tg_id=?",
                (group_id, uid),
            ).fetchone()
        return row is not None

    def can_view_group(self, group_id: int, uid: int) -> bool:
        return self.is_group_member(group_id, uid)

    def can_add_expense(self, group_id: int, uid: int) -> bool:
        return self.is_group_member(group_id, uid)

    def can_view_settlements(self, group_id: int, uid: int) -> bool:
        return self.is_group_member(group_id, uid)

    def get_invite_code(self, group_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                'SELECT invite_code FROM "groups" WHERE id=?', (group_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"group {group_id} not found")
        return row["invite_code"]

    def get_group_title(self, group_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                'SELECT title FROM "groups" WHERE id=?', (group_id,)
            ).fetchone()
        return row["title"] if row else ""

    def group_currency(self, group_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                'SELECT currency FROM "groups" WHERE id=?', (group_id,)
            ).fetchone()
        return row["currency"] if row else default_currency()

    def group_titles(self, group_ids) -> dict[int, str]:
        """Titles for a screen that spans groups, in one query."""
        ids = list({int(i) for i in group_ids})
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f'SELECT id, title FROM "groups" WHERE id IN ({placeholders})', ids
            ).fetchall()
        found = {r["id"]: r["title"] for r in rows}
        return {gid: found.get(gid, "") for gid in ids}

    def group_currencies(self, group_ids) -> dict[int, str]:
        """One lookup for a screen that spans groups."""
        ids = list(group_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f'SELECT id, currency FROM "groups" WHERE id IN ({placeholders})',
                ids,
            ).fetchall()
        return {r["id"]: r["currency"] for r in rows}

    def can_change_currency(self, group_id: int) -> bool:
        """Only while the group is empty.

        Every stored amount is already in the base currency; swapping it
        later would silently reinterpret every past trace.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT"
                " (SELECT COUNT(*) FROM expenses WHERE group_id=? AND deleted=0)"
                " + (SELECT COUNT(*) FROM settlements WHERE group_id=?)",
                (group_id, group_id),
            ).fetchone()
        return bool(row) and row[0] == 0

    def set_group_currency(self, group_id: int, currency: str) -> bool:
        code = normalize_currency(currency)
        if not code or not self.can_change_currency(group_id):
            return False
        with self._lock:
            self._conn.execute(
                'UPDATE "groups" SET currency=? WHERE id=?', (code, group_id)
            )
            self._conn.commit()
        return True

    def is_group_owner(self, group_id: int, uid: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                'SELECT owner_tg_id FROM "groups" WHERE id=?', (group_id,)
            ).fetchone()
        return row is not None and row["owner_tg_id"] == uid

    def can_delete_group(self, group_id: int, uid: int) -> bool:
        return self.is_group_owner(group_id, uid)

    def rename_group(self, group_id: int, title: str) -> bool:
        clean = title.strip()
        if not clean:
            return False
        with self._lock:
            self._conn.execute(
                'UPDATE "groups" SET title=? WHERE id=?', (clean[:100], group_id)
            )
            self._conn.commit()
        return True

    def member_balance(self, group_id: int, uid: int) -> int:
        """What this person is up or down in one group, in its currency."""
        return self._net_positions(group_id).get(uid, 0)

    def member_balances(self, group_id: int) -> dict[int, int]:
        """Everyone's balance at once: the members screen needs them all,
        and each call walks every expense in the group."""
        return self._net_positions(group_id)

    def remove_member(self, group_id: int, uid: int) -> str:
        """Take someone out of a group. Returns "" or why it cannot happen.

        Somebody who is still owed money — or still owes it — cannot be
        dropped: their share of every past expense stays in the ledger, but
        the debt screen only walks the groups a person belongs to, so the
        debt would keep existing for the other side alone.
        """
        if not self.is_group_member(group_id, uid):
            return "not_member"
        if self.member_balance(group_id, uid) != 0:
            return "has_debt"
        if self.is_group_owner(group_id, uid):
            return "owner"
        with self._lock:
            self._conn.execute(
                "DELETE FROM group_members WHERE group_id=? AND tg_id=?",
                (group_id, uid),
            )
            self._conn.commit()
        return ""

    def leave_group(self, group_id: int, uid: int) -> str:
        """Leave a group, handing ownership over if the owner walks out.

        Returns "" on success, "last" if the group was removed with its
        last member, or the reason it could not happen.
        """
        if not self.is_group_member(group_id, uid):
            return "not_member"
        if self.member_balance(group_id, uid) != 0:
            return "has_debt"

        if not self.is_group_owner(group_id, uid):
            with self._lock:
                self._conn.execute(
                    "DELETE FROM group_members WHERE group_id=? AND tg_id=?",
                    (group_id, uid),
                )
                self._conn.commit()
            return ""

        with self._lock:
            heir = self._conn.execute(
                "SELECT tg_id FROM group_members"
                " WHERE group_id=? AND tg_id<>?"
                " ORDER BY joined_at, tg_id LIMIT 1",
                (group_id, uid),
            ).fetchone()
        if heir is None:
            # Nobody is left to own it, so the group goes with them.
            self.delete_group(group_id)
            return "last"

        with self._lock:
            self._conn.execute(
                'UPDATE "groups" SET owner_tg_id=? WHERE id=?',
                (heir["tg_id"], group_id),
            )
            self._conn.execute(
                "UPDATE group_members SET role='owner' WHERE group_id=? AND tg_id=?",
                (group_id, heir["tg_id"]),
            )
            self._conn.execute(
                "DELETE FROM group_members WHERE group_id=? AND tg_id=?",
                (group_id, uid),
            )
            self._conn.commit()
        return ""

    def next_owner(self, group_id: int, leaving: int) -> int:
        """Who would inherit the group — for warning the leaver in advance."""
        with self._lock:
            row = self._conn.execute(
                "SELECT tg_id FROM group_members"
                " WHERE group_id=? AND tg_id<>?"
                " ORDER BY joined_at, tg_id LIMIT 1",
                (group_id, leaving),
            ).fetchone()
        return row["tg_id"] if row else 0

    def delete_group(self, group_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM settlements WHERE group_id=?", (group_id,))
            self._conn.execute('DELETE FROM "groups" WHERE id=?', (group_id,))
            self._conn.commit()

    # -- members --

    def list_members(self, group_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.tg_id,u.name FROM group_members m"
                " JOIN users u ON u.tg_id=m.tg_id"
                " WHERE m.group_id=? ORDER BY u.name",
                (group_id,),
            ).fetchall()
        return [{"id": r["tg_id"], "name": r["name"]} for r in rows]

    def list_members_detailed(self, group_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.tg_id, u.name, m.role"
                " FROM group_members m"
                " JOIN users u ON u.tg_id = m.tg_id"
                " WHERE m.group_id = ?"
                " ORDER BY (m.role <> 'owner'), u.name",
                (group_id,),
            ).fetchall()
        return [{"id": r["tg_id"], "name": r["name"], "role": r["role"]} for r in rows]

    # -- expenses --

    def create_expense(
        self,
        group_id: int,
        created_by: int,
        payer: int,
        description: str,
        amount_cents: int,
        shares: dict,
        orig_currency: str = "",
        orig_amount_cents: int = 0,
    ) -> int:
        if not shares:
            raise ValueError("no participants")
        with self._lock:
            rows = self._conn.execute(
                "SELECT tg_id FROM group_members WHERE group_id=?",
                (group_id,),
            ).fetchall()
            members = {r["tg_id"] for r in rows}
            if created_by not in members:
                raise ValueError("creator is not a group member")
            if payer not in members:
                raise ValueError("payer is not a group member")
            if not set(shares).issubset(members):
                raise ValueError("expense participant is not a group member")

            cur = self._conn.execute(
                "INSERT INTO expenses("
                "group_id,created_by_tg_id,payer_tg_id,description,amount_cents,"
                "created_at,orig_currency,orig_amount_cents"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    group_id, created_by, payer, description, amount_cents,
                    now_unix(), orig_currency or None, orig_amount_cents or None,
                ),
            )
            expense_id = cur.lastrowid
            self._conn.executemany(
                "INSERT INTO expense_participants(expense_id,participant_tg_id,share_cents)"
                " VALUES(?,?,?)",
                [(expense_id, pid, cents) for pid, cents in shares.items()],
            )
            self._conn.commit()
        return expense_id

    def get_expense(self, expense_id: int, group_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, group_id, payer_tg_id, created_by_tg_id, description,"
                " amount_cents, created_at, updated_at, orig_currency,"
                " orig_amount_cents, receipt_file_id"
                " FROM expenses WHERE id=? AND group_id=? AND deleted=0",
                (expense_id, group_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "group_id": row["group_id"],
            "payer": row["payer_tg_id"],
            "created_by": row["created_by_tg_id"],
            "desc": row["description"],
            "amount_cents": row["amount_cents"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"] or 0,
            "orig_currency": row["orig_currency"] or "",
            "orig_amount_cents": row["orig_amount_cents"] or 0,
            "receipt": row["receipt_file_id"] or "",
            "shares": self.get_expense_shares(expense_id),
        }

    def update_expense(
        self,
        expense_id: int,
        group_id: int,
        payer: int,
        description: str,
        amount_cents: int,
        shares: dict,
        orig_currency: str = "",
        orig_amount_cents: int = 0,
    ) -> None:
        """Rewrite an expense in place, keeping its number and its receipt."""
        if not shares:
            raise ValueError("no participants")
        with self._lock:
            rows = self._conn.execute(
                "SELECT tg_id FROM group_members WHERE group_id=?", (group_id,)
            ).fetchall()
            members = {r["tg_id"] for r in rows}
            if payer not in members:
                raise ValueError("payer is not a group member")
            if not set(shares).issubset(members):
                raise ValueError("expense participant is not a group member")

            self._conn.execute(
                "UPDATE expenses SET payer_tg_id=?, description=?, amount_cents=?,"
                " orig_currency=?, orig_amount_cents=?, updated_at=?"
                " WHERE id=? AND group_id=? AND deleted=0",
                (
                    payer, description, amount_cents,
                    orig_currency or None, orig_amount_cents or None,
                    now_unix(), expense_id, group_id,
                ),
            )
            self._conn.execute(
                "DELETE FROM expense_participants WHERE expense_id=?", (expense_id,)
            )
            self._conn.executemany(
                "INSERT INTO expense_participants(expense_id,participant_tg_id,share_cents)"
                " VALUES(?,?,?)",
                [(expense_id, pid, cents) for pid, cents in shares.items()],
            )
            self._conn.commit()

    def set_receipt(self, expense_id: int, group_id: int, file_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE expenses SET receipt_file_id=? WHERE id=? AND group_id=?",
                (file_id or None, expense_id, group_id),
            )
            self._conn.commit()

    def delete_expense(self, expense_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE expenses SET deleted=1 WHERE id=?", (expense_id,)
            )
            self._conn.commit()

    def can_edit_expense(self, expense_id: int, group_id: int, uid: int) -> bool:
        """Same rule as deleting: the person who entered it owns it."""
        return self.can_delete_expense(expense_id, group_id, uid)

    def can_delete_expense(self, expense_id: int, group_id: int, uid: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT e.created_by_tg_id"
                " FROM expenses e"
                " WHERE e.id=? AND e.group_id=? AND e.deleted=0",
                (expense_id, group_id),
            ).fetchone()
        if row is None:
            return False
        if not self.is_group_member(group_id, uid):
            return False
        return row["created_by_tg_id"] == uid

    def get_expense_shares(self, expense_id: int) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT participant_tg_id, share_cents"
                " FROM expense_participants WHERE expense_id=?",
                (expense_id,),
            ).fetchall()
        return {r["participant_tg_id"]: r["share_cents"] for r in rows}

    def list_group_expenses(self, group_id: int, limit: int, offset: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,payer_tg_id,created_by_tg_id,amount_cents,description,"
                "created_at,updated_at,orig_currency,orig_amount_cents,"
                "receipt_file_id"
                " FROM expenses WHERE group_id=? AND deleted=0"
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                (group_id, limit, offset),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "payer": r["payer_tg_id"],
                "created_by": r["created_by_tg_id"],
                "amount_cents": r["amount_cents"],
                "desc": r["description"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"] or 0,
                "orig_currency": r["orig_currency"] or "",
                "orig_amount_cents": r["orig_amount_cents"] or 0,
                "receipt": r["receipt_file_id"] or "",
            }
            for r in rows
        ]

    def count_group_expenses(self, group_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM expenses WHERE group_id=? AND deleted=0",
                (group_id,),
            ).fetchone()
        return row[0] if row else 0

    # -- balances --

    def _net_positions(self, group_id: int) -> dict[int, int]:
        """Per-user balance in one group: positive = the group owes them."""
        with self._lock:
            share_rows = self._conn.execute(
                "SELECT e.payer_tg_id AS payer,"
                " p.participant_tg_id AS participant,"
                " p.share_cents AS share"
                " FROM expenses e"
                " JOIN expense_participants p ON p.expense_id = e.id"
                " WHERE e.group_id=? AND e.deleted=0",
                (group_id,),
            ).fetchall()
            settlement_rows = self._conn.execute(
                "SELECT from_tg_id, to_tg_id, amount_cents"
                " FROM settlements WHERE group_id=? AND confirmed_by_to=1",
                (group_id,),
            ).fetchall()

        net: dict[int, int] = {}
        for r in share_rows:
            payer, participant, share = r["payer"], r["participant"], r["share"]
            if participant == payer:
                continue
            net[participant] = net.get(participant, 0) - share
            net[payer] = net.get(payer, 0) + share

        for r in settlement_rows:
            frm, to, amt = r["from_tg_id"], r["to_tg_id"], r["amount_cents"]
            net[frm] = net.get(frm, 0) + amt
            net[to] = net.get(to, 0) - amt

        return {uid: v for uid, v in net.items() if v != 0}

    def compute_group_balances(self, group_id: int) -> dict:
        """Returns {(from_uid, to_uid): amount_cents} for net unpaid debts.

        Debts are simplified through chains, so A owing B while B owes C
        collapses into A paying C directly.
        """
        return settle_net(self._net_positions(group_id))

    def compute_user_debts(self, uid: int) -> dict[int, dict[str, dict]]:
        """What ``uid`` owes and is owed, per counterparty, across groups.

        Returns ``{other_uid: {currency: {"net": cents, "by_group": {gid: cents}}}}``
        where a positive amount means the counterparty owes ``uid`` and a
        negative one means ``uid`` owes them. Debts in opposite directions
        cancel: owing someone 50 in one group while they owe 50 in another
        nets to zero, and there is genuinely nothing to transfer.

        Currencies are kept apart, because a debt in lira is not repaid by
        a credit in roubles — netting them would invent an exchange rate
        nobody agreed on.

        Only pairs that share a group can appear here, because every entry
        comes from a single group's simplified balances.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.group_id AS gid, g.currency AS currency"
                " FROM group_members m"
                ' JOIN "groups" g ON g.id = m.group_id'
                " WHERE m.tg_id=?",
                (uid,),
            ).fetchall()

        result: dict[int, dict[str, dict]] = {}
        for r in rows:
            gid, currency = r["gid"], r["currency"]
            for (frm, to), amount in self.compute_group_balances(gid).items():
                if frm == uid:
                    other, delta = to, -amount
                elif to == uid:
                    other, delta = frm, amount
                else:
                    continue
                entry = result.setdefault(other, {}).setdefault(
                    currency, {"net": 0, "by_group": {}}
                )
                entry["net"] += delta
                entry["by_group"][gid] = entry["by_group"].get(gid, 0) + delta

        return result

    # -- settlements --

    def request_settlement(
        self, uid: int, other: int, currency: str, amount_cents: int
    ) -> str:
        """Record a payment ``uid`` says they made, awaiting confirmation.

        Returns the batch id, or "" if the request does not match the live
        debt. Nothing here changes any balance: the rows land unconfirmed,
        and only the person who was supposed to receive the money can turn
        them into a settled debt.

        Paying the whole net closes the debt in every shared group and in
        both directions, so the two halves of a cross-group offset
        disappear together instead of one being paid twice. A smaller
        amount is spread over the groups where ``uid`` owes, largest debt
        first, and leaves the rest standing — the export shows exactly
        which group each part went to.
        """
        currency = normalize_currency(currency)
        if amount_cents < 0 or not currency:
            return ""

        with self._lock:
            if self.pending_batch_between(uid, other, currency):
                return ""  # one open claim at a time, or people double-pay
            entry = self.compute_user_debts(uid).get(other, {}).get(currency)
            if entry is None:
                return ""
            owed = -entry["net"]
            if amount_cents > owed:
                return ""

            rows = []
            if amount_cents == owed:
                for gid, delta in entry["by_group"].items():
                    if delta < 0:
                        rows.append((gid, uid, other, -delta))
                    elif delta > 0:
                        rows.append((gid, other, uid, delta))
            else:
                left = amount_cents
                debts = sorted(
                    ((gid, -delta) for gid, delta in entry["by_group"].items() if delta < 0),
                    key=lambda pair: (-pair[1], pair[0]),
                )
                for gid, debt in debts:
                    if left <= 0:
                        break
                    part = min(left, debt)
                    rows.append((gid, uid, other, part))
                    left -= part
                if left > 0:
                    return ""
            if not rows:
                return ""

            batch = rand_code()
            ts = now_unix()
            self._conn.executemany(
                "INSERT INTO settlements"
                "(group_id,from_tg_id,to_tg_id,amount_cents,confirmed_by_to,"
                "created_at,batch)"
                " VALUES(?,?,?,?,0,?,?)",
                [(gid, frm, to, amt, ts, batch) for gid, frm, to, amt in rows],
            )
            self._conn.commit()
            return batch

    def pending_batch_between(self, uid: int, other: int, currency: str) -> str:
        """The open claim between two people in one currency, if any."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.batch, g.currency"
                " FROM settlements s"
                ' JOIN "groups" g ON g.id = s.group_id'
                " WHERE s.confirmed_by_to=0 AND s.batch IS NOT NULL"
                " AND ((s.from_tg_id=? AND s.to_tg_id=?)"
                "      OR (s.from_tg_id=? AND s.to_tg_id=?))",
                (uid, other, other, uid),
            ).fetchall()
        for r in rows:
            if r["currency"] == currency:
                return r["batch"]
        return ""

    def batch_info(self, batch: str) -> Optional[dict]:
        """Who owes whom, how much, and in which groups — for one claim."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.group_id, s.from_tg_id, s.to_tg_id, s.amount_cents,"
                " s.confirmed_by_to, s.created_at, g.currency"
                " FROM settlements s"
                ' JOIN "groups" g ON g.id = s.group_id'
                " WHERE s.batch=?",
                (batch,),
            ).fetchall()
        if not rows:
            return None
        # The payer is the side that hands money over; a full settlement can
        # also carry the opposite half of a cross-group offset, which is
        # bookkeeping rather than a transfer.
        paid = {}
        for r in rows:
            key = (r["from_tg_id"], r["to_tg_id"])
            paid[key] = paid.get(key, 0) + r["amount_cents"]
        (frm, to), amount = max(paid.items(), key=lambda kv: kv[1])
        return {
            "batch": batch,
            "from": frm,
            "to": to,
            "amount_cents": amount,
            "currency": rows[0]["currency"],
            "confirmed": bool(rows[0]["confirmed_by_to"]),
            "created_at": rows[0]["created_at"],
            "groups": sorted({r["group_id"] for r in rows}),
        }

    def confirm_settlement(self, batch: str, uid: int) -> bool:
        """Only the person the money was owed to can confirm it arrived."""
        info = self.batch_info(batch)
        if info is None or info["confirmed"] or info["to"] != uid:
            return False
        with self._lock:
            self._conn.execute(
                "UPDATE settlements SET confirmed_by_to=1"
                " WHERE batch=? AND confirmed_by_to=0",
                (batch,),
            )
            self._conn.commit()
        return True

    def reject_settlement(self, batch: str, uid: int) -> bool:
        """Drop an unconfirmed claim.

        The recipient rejects a payment they never got; the payer withdraws
        a claim they sent by mistake. Either way the debt simply stays.
        """
        info = self.batch_info(batch)
        if info is None or info["confirmed"] or uid not in (info["from"], info["to"]):
            return False
        with self._lock:
            self._conn.execute(
                "DELETE FROM settlements WHERE batch=? AND confirmed_by_to=0",
                (batch,),
            )
            self._conn.commit()
        return True

    def list_pending_settlements(self, uid: int) -> list[dict]:
        """Open claims this person sent or has to answer, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT batch FROM settlements"
                " WHERE confirmed_by_to=0 AND batch IS NOT NULL"
                " AND (from_tg_id=? OR to_tg_id=?)"
                " ORDER BY created_at DESC",
                (uid, uid),
            ).fetchall()
        infos = [self.batch_info(r["batch"]) for r in rows]
        return [i for i in infos if i is not None]

    def count_group_settlements(self, group_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM settlements WHERE group_id=?",
                (group_id,),
            ).fetchone()
        return row[0] if row else 0

    def list_group_settlements(self, group_id: int, limit: int, offset: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, from_tg_id, to_tg_id, amount_cents, created_at,"
                " confirmed_by_to, batch"
                " FROM settlements WHERE group_id=?"
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                (group_id, limit, offset),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "from": r["from_tg_id"],
                "to": r["to_tg_id"],
                "amount_cents": r["amount_cents"],
                "created_at": r["created_at"],
                "confirmed": bool(r["confirmed_by_to"]),
                "batch": r["batch"] or "",
            }
            for r in rows
        ]

    def can_delete_settlement(self, settlement_id: int, group_id: int, uid: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT from_tg_id FROM settlements WHERE id=? AND group_id=?",
                (settlement_id, group_id),
            ).fetchone()
        if row is None:
            return False
        return row["from_tg_id"] == uid or self.is_group_owner(group_id, uid)

    def delete_settlement(self, settlement_id: int, group_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM settlements WHERE id=? AND group_id=?",
                (settlement_id, group_id),
            )
            self._conn.commit()

    # -- export --

    def export_group(self, group_id: int) -> dict:
        """Every row a member would need to re-check the group's maths.

        Deleted expenses are left out: they do not affect any debt, so
        showing them would only invite people to add them back in by hand.
        """
        with self._lock:
            expense_rows = self._conn.execute(
                "SELECT id, payer_tg_id, created_by_tg_id, description,"
                " amount_cents, created_at, updated_at, orig_currency,"
                " orig_amount_cents, receipt_file_id"
                " FROM expenses WHERE group_id=? AND deleted=0"
                " ORDER BY created_at, id",
                (group_id,),
            ).fetchall()
            share_rows = self._conn.execute(
                "SELECT p.expense_id, p.participant_tg_id, p.share_cents"
                " FROM expense_participants p"
                " JOIN expenses e ON e.id = p.expense_id"
                " WHERE e.group_id=? AND e.deleted=0",
                (group_id,),
            ).fetchall()
            settlement_rows = self._conn.execute(
                "SELECT id, from_tg_id, to_tg_id, amount_cents,"
                " confirmed_by_to, created_at"
                " FROM settlements WHERE group_id=? ORDER BY created_at, id",
                (group_id,),
            ).fetchall()

        shares: dict[int, dict[int, int]] = {}
        for r in share_rows:
            shares.setdefault(r["expense_id"], {})[r["participant_tg_id"]] = r["share_cents"]

        expenses = [
            {
                "id": r["id"],
                "payer": r["payer_tg_id"],
                "created_by": r["created_by_tg_id"],
                "desc": r["description"],
                "amount_cents": r["amount_cents"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"] or 0,
                "orig_currency": r["orig_currency"] or "",
                "orig_amount_cents": r["orig_amount_cents"] or 0,
                "receipt": r["receipt_file_id"] or "",
                "shares": shares.get(r["id"], {}),
            }
            for r in expense_rows
        ]
        settlements = [
            {
                "id": r["id"],
                "from": r["from_tg_id"],
                "to": r["to_tg_id"],
                "amount_cents": r["amount_cents"],
                "counted": bool(r["confirmed_by_to"]),
                "created_at": r["created_at"],
            }
            for r in settlement_rows
        ]

        members = self.list_members_detailed(group_id)
        names = {m["id"]: m["name"] for m in members}
        for e in expenses:
            for uid in [e["payer"], e["created_by"], *e["shares"]]:
                if uid and uid not in names:
                    names[uid] = self.user_name(uid)
        for s in settlements:
            for uid in (s["from"], s["to"]):
                if uid not in names:
                    names[uid] = self.user_name(uid)

        return {
            "group_id": group_id,
            "title": self.get_group_title(group_id),
            "currency": self.group_currency(group_id),
            "members": members,
            "names": names,
            "expenses": expenses,
            "settlements": settlements,
            "balances": self.compute_group_balances(group_id),
        }


# ---------- Group report ----------
#
# The point of the export is that nobody has to trust the bot: every number
# it derives is shown next to the raw rows it came from, so a member can
# redo the arithmetic by hand and land on the same debts.

_EXPORT_SLUG_RE = re.compile(r"[^\w-]+", re.UNICODE)


def export_filename(group_id: int, title: str) -> str:
    slug = _EXPORT_SLUG_RE.sub("-", title).strip("-")[:40].strip("-")
    parts = [f"group-{group_id}", slug, time.strftime("%Y-%m-%d")]
    return "-".join(p for p in parts if p) + ".xlsx"


def _expenses_sheet(data: dict, order: list[int], tz: int) -> tuple[str, list, list[list]]:
    """One row per expense, one share column per person.

    Reading across a row shows who paid and how the amount was cut up;
    reading down a person's column gives everything they consumed. The two
    totals at the bottom are what the balances sheet starts from.
    """
    names = data["names"]
    base = data["currency"]
    header = [
        "№", "Дата", "Описание", f"Сумма, {base}", f"Сумма долей, {base}",
        "Кто платил", "Кто добавил", "Оплачено в валюте", "Сумма в валюте",
        f"Курс к {base}", "Чек", "Изменена",
    ] + [f"Доля: {names[uid]}" for uid in order]

    rows: list[list] = []
    total = 0
    share_totals = {uid: 0 for uid in order}
    for e in data["expenses"]:
        shares = e["shares"]
        total += e["amount_cents"]
        for uid, cents in shares.items():
            share_totals[uid] = share_totals.get(uid, 0) + cents
        orig_currency = e["orig_currency"]
        rate = (
            e["amount_cents"] / e["orig_amount_cents"]
            if orig_currency and e["orig_amount_cents"]
            else None
        )
        rows.append(
            [
                e["id"],
                format_time(e["created_at"], tz),
                e["desc"],
                Money(e["amount_cents"]),
                Money(sum(shares.values())),
                names[e["payer"]],
                names[e["created_by"]] if e["created_by"] else "",
                orig_currency,
                Money(e["orig_amount_cents"]) if orig_currency else None,
                rate,
                "да" if e["receipt"] else "",
                format_time(e["updated_at"], tz) if e["updated_at"] else "",
            ]
            + [Money(shares[uid]) if uid in shares else None for uid in order]
        )

    rows.append(
        ["", "", "ИТОГО", Money(total), Money(sum(share_totals.values())),
         "", "", "", None, None, "", ""]
        + [Money(share_totals.get(uid, 0)) for uid in order]
    )
    return ("Траты", header, rows)


def _totals_sheet(data: dict, order: list[int]) -> tuple[str, list, list[list]]:
    """Each person's four raw numbers and the balance they add up to.

    The balance column has to sum to zero: every rouble one person is short
    is a rouble another is up.
    """
    names = data["names"]
    paid = {uid: 0 for uid in order}
    consumed = {uid: 0 for uid in order}
    sent = {uid: 0 for uid in order}
    received = {uid: 0 for uid in order}

    for e in data["expenses"]:
        paid[e["payer"]] = paid.get(e["payer"], 0) + e["amount_cents"]
        for uid, cents in e["shares"].items():
            consumed[uid] = consumed.get(uid, 0) + cents
    for s in data["settlements"]:
        if not s["counted"]:
            continue
        sent[s["from"]] = sent.get(s["from"], 0) + s["amount_cents"]
        received[s["to"]] = received.get(s["to"], 0) + s["amount_cents"]

    base = data["currency"]
    header = [
        "Участник", f"Оплатил, {base}", f"Его доля, {base}",
        f"Отдал по расчётам, {base}", f"Получил по расчётам, {base}",
        f"Баланс, {base}", "Итог",
    ]
    rows: list[list] = []
    totals = [0, 0, 0, 0, 0]
    for uid in order:
        balance = paid[uid] - consumed[uid] + sent[uid] - received[uid]
        if balance > 0:
            verdict = f"должны вернуть {format_cents(balance, base)}"
        elif balance < 0:
            verdict = f"должен(на) {format_cents(-balance, base)}"
        else:
            verdict = "в расчёте"
        rows.append([
            names[uid],
            Money(paid[uid]),
            Money(consumed[uid]),
            Money(sent[uid]),
            Money(received[uid]),
            Money(balance),
            verdict,
        ])
        for i, value in enumerate(
            (paid[uid], consumed[uid], sent[uid], received[uid], balance)
        ):
            totals[i] += value

    rows.append(["ИТОГО"] + [Money(v) for v in totals] + ["сумма балансов = 0"])
    return ("Итоги по людям", header, rows)


def _transfers_sheet(data: dict) -> tuple[str, list, list[list]]:
    names = data["names"]
    header = ["Должник", "Получатель", f"Сумма, {data['currency']}"]
    rows = [
        [names[frm], names[to], Money(amount)]
        for (frm, to), amount in sorted(
            data["balances"].items(),
            key=lambda kv: (names[kv[0][0]].lower(), names[kv[0][1]].lower()),
        )
    ]
    if not rows:
        rows.append(["Все в расчёте", "", ""])
    return ("Кто кому платит", header, rows)


def _settlements_sheet(data: dict, tz: int) -> tuple[str, list, list[list]]:
    names = data["names"]
    header = [
        "№", "Дата", "Кто отдал", "Кому", f"Сумма, {data['currency']}", "Статус",
    ]
    rows = [
        [
            s["id"],
            format_time(s["created_at"], tz),
            names[s["from"]],
            names[s["to"]],
            Money(s["amount_cents"]),
            "подтверждён" if s["counted"] else "ждёт подтверждения",
        ]
        for s in data["settlements"]
    ]
    if not rows:
        rows.append(["", "", "Платежей ещё не было", "", "", ""])
    return ("Платежи", header, rows)


def _howto_sheet(data: dict, tz: int) -> tuple[str, list, list[list]]:
    return (
        "Как проверить",
        ["Как проверить расчёт вручную"],
        [[line] for line in [
            f"Группа #{data['group_id']}: {data['title']}",
            f"Валюта группы: {data['currency']} — все суммы в ней.",
            f"Выгружено: {format_time(now_unix(), tz)},"
            f" время в {tz_label(tz)} (сменить: /tz +3)",
            "",
            "Лист «Траты» — исходные данные, по одной строке на трату.",
            "  Плательщик отдал всю сумму, а колонки «Доля: …» показывают,",
            "  за кого эта сумма была потрачена.",
            "  «Сумма долей» в каждой строке обязана совпадать с «Суммой».",
            "",
            "Лист «Итоги по людям» — по каждому человеку:",
            "  Баланс = Оплатил − Его доля + Отдал по расчётам − Получил по расчётам.",
            "  Плюс — человеку должны, минус — должен он.",
            "  Сумма всех балансов всегда равна нулю.",
            "",
            "Лист «Кто кому платит» — те же балансы, сведённые в минимум переводов:",
            "  если А должен Б, а Б должен В, бот убирает Б и просит А платить В.",
            "  Итоговые суммы у каждого человека при этом не меняются.",
            "",
            "Лист «Платежи» — расчёты между людьми.",
            "  Долг уменьшают только подтверждённые получателем платежи;",
            "  строки «ждёт подтверждения» ни на что пока не влияют.",
            "",
            "Трата, оплаченная в другой валюте, хранит и то, что реально отдали:",
            "  колонки «Оплачено в валюте», «Сумма в валюте» и «Курс».",
            "  В долгах участвует только сумма в валюте группы.",
            "",
            "Колонка «Чек» помечает траты, к которым приложено фото чека —",
            "  его видно в боте на карточке траты.",
            "",
            "Удалённые траты в выгрузку не попадают: они не участвуют и в долгах.",
        ]],
    )


def build_group_workbook(data: dict, tz: int = 0) -> bytes:
    """Render one group's ledger as an .xlsx workbook.

    ``tz`` is the requesting user's offset from UTC in minutes: the file
    is read by a person, so it should carry their clock, not the server's.
    """
    # Members first, in the order the bot shows them, then anyone who appears
    # only in old rows — a name must never silently drop a share.
    order = [m["id"] for m in data["members"]]
    seen = set(order)
    for e in data["expenses"]:
        for uid in [e["payer"], e["created_by"], *e["shares"]]:
            if uid and uid not in seen:
                seen.add(uid)
                order.append(uid)
    for s in data["settlements"]:
        for uid in (s["from"], s["to"]):
            if uid not in seen:
                seen.add(uid)
                order.append(uid)

    return build_xlsx([
        _expenses_sheet(data, order, tz),
        _totals_sheet(data, order),
        _transfers_sheet(data),
        _settlements_sheet(data, tz),
        _howto_sheet(data, tz),
    ])


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

_TOP_BUTTONS = {
    "👥 Мои группы", "🧾 Добавить трату", "💰 Долги",
} | _LEGACY_DEBT_BUTTONS | _LEGACY_GROUP_BUTTONS


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
            rows.append([InlineKeyboardButton(f"#{g['id']}: {g['title']}", callback_data=cb)])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("« Назад", callback_data=f"{mode}|p:{page - 1}"))
        if end < total:
            nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"{mode}|p:{page + 1}"))
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
        markup, total = self._groups_page_keyboard(
            update.effective_user.id, page, mode
        )
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
            raw.replace(" ", " ")
            .replace(" ", " ")
            .replace(" ", " ")
            .replace("+", " ")
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
                "Сейчас идёт мастер. Отправьте запрошенные данные"
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
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            f"✅ {format_cents(cents, base)} — дальше",
                            callback_data="rateok",
                        )],
                        [InlineKeyboardButton(
                            "❌ Отмена", callback_data="cancel_flow"
                        )],
                    ]),
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
                    f"Сумма не распознана. Пришлите, сколько это в {base},"
                    f" напр. 9500",
                    reply_markup=inline_cancel(),
                )
                return

            st["amount_cents"] = amt
            st["step"] = "choose_payer"
            _set_ae(ctx, st)
            rate = format_rate(
                amt, st["orig_amount_cents"], base, st["orig_currency"]
            )
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
                amt = remaining

            custom_shares[next_uid] = amt
            st["custom_left"] = custom_left[1:]
            _set_ae(ctx, st)

            progress = self._shares_progress(st)

            if not st["custom_left"]:
                await self._edit_or_send(
                    update, progress + "\nВсе суммы заданы. Сохраняю…", inline_cancel()
                )
                await self._finalize_expense(update, ctx, st)
            else:
                nxt = st["custom_left"][0]
                rem = st["amount_cents"] - sum(custom_shares.values())
                await self._edit_or_send(
                    update,
                    f"{progress}\n\nВведите сумму для участника"
                    f" {self.repo.user_name(nxt)} (остаток — {format_cents(rem)}):",
                    inline_cancel(),
                )

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
        await self._edit_or_send(
            update,
            f"{prefix}Введите сумму для участника {name}"
            f" (остаток — {format_cents(remaining)}, максимум — {format_cents(remaining)}):",
            inline_cancel(),
        )

    # ---------- Callback handler ----------

    async def on_callback(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self.repo.upsert_user(update.effective_user.id, self._best_name(update.effective_user))
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
                    update, ctx, mode, int(data[len(prefix):])
                )
                await query.answer()
                return

        # Group selection
        if data.startswith("mgsel|"):
            gid = int(data[len("mgsel|"):])
            if not self.repo.is_group_member(gid, uid):
                await query.answer("Нет доступа", show_alert=True)
                return
            await self._send_group_details(update, ctx, gid)
            await query.answer()
            return

        if data.startswith("aesel|"):
            gid = int(data[len("aesel|"):])
            if not self.repo.is_group_member(gid, uid):
                await query.answer("Нет доступа", show_alert=True)
                return
            base = self.repo.group_currency(gid)
            _set_ae(ctx, {
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
            })
            await update.effective_chat.send_message(
                f"Группа #{gid} выбрана, валюта — {base}. Пришлите сумму и"
                f" описание одним сообщением, напр.:\n1500 такси из аэропорта\n"
                f"Платили в другой валюте — укажите её: 100 EUR ужин"
            )
            await query.answer("Группа выбрана")
            return

        # Excel export of everything the group's debts are computed from
        if data.startswith("xlsx|"):
            gid = int(data[len("xlsx|"):])
            if not self.repo.is_group_member(gid, uid):
                await query.answer("Нет доступа", show_alert=True)
                return
            await query.answer("Готовлю файл…")
            await self._send_group_workbook(update, gid)
            return

        # Expense list pagination
        if data.startswith("explist|"):
            parts = data.split("|")
            if len(parts) >= 3 and parts[2].startswith("p:"):
                gid = int(parts[1])
                page = int(parts[2][2:])
                if self.repo.is_group_member(gid, uid):
                    await self._send_expenses_page(update, ctx, gid, page)
                else:
                    await query.answer("Нет доступа", show_alert=True)
                    return
            await query.answer()
            return

        # One expense in full
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

            self.repo.set_receipt(eid, gid, "")
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
                    self.repo.delete_expense(eid)
                    await query.answer("Удалено")
                    _del_receipt(ctx)
                    await self._send_expenses_page(update, ctx, gid, page)
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
                    f"{actor} подтвердил(а) получение {amount}."
                    " Долг закрыт."
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
            gid = int(data[len("gset|"):])
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
            gid = int(data[len("gcur|"):])
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
            gid = int(data[len("gleave|"):])
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
                    f"\nВы владелец: группа перейдёт к"
                    f" {self.repo.user_name(heir)}."
                    if heir
                    else "\nВы последний участник: группа будет удалена"
                         " вместе со всеми тратами."
                )
            await self._edit_or_send(
                update,
                f"Выйти из группы #{gid}?"
                f" Ваши прошлые траты останутся в истории.{warning}",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "Да, выйти", callback_data=f"gleaveyes|{gid}"
                    )],
                    [InlineKeyboardButton("Отмена", callback_data=f"gset|{gid}")],
                ]),
            )
            await query.answer()
            return

        if data.startswith("gleaveyes|"):
            gid = int(data[len("gleaveyes|"):])
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
            gid = int(data[len("grpdel|gid:"):])
            if not self.repo.can_delete_group(gid, uid):
                await query.answer("Только владелец может удалить группу")
                return
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("Да, удалить", callback_data=f"grpdelyes|gid:{gid}")],
                [InlineKeyboardButton("Отмена", callback_data=f"mgsel|{gid}")],
            ])
            await self._edit_or_send(
                update,
                f"Точно удалить группу #{gid}? Это удалит все её данные.",
                markup,
            )
            await query.answer()
            return

        if data.startswith("grpdelyes|"):
            gid = int(data[len("grpdelyes|gid:"):])
            if not self.repo.can_delete_group(gid, uid):
                await query.answer("Только владелец может удалить группу", show_alert=True)
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
            await query.answer("Мастер уже закрыт", show_alert=True)
            return
        group_id = st.get("group_id")
        if not self.repo.is_group_member(group_id, uid):
            _del_ae(ctx)
            await query.answer("Нет доступа", show_alert=True)
            return

        # Keep the rate the bot proposed
        if data == "rateok":
            st = _get_ae(ctx)
            if st is None or st.get("step") != "confirm_rate":
                await query.answer("Мастер уже закрыт", show_alert=True)
                return
            st["step"] = "choose_payer"
            _set_ae(ctx, st)
            await query.answer()
            await self._ask_payer(update, ctx, st)
            return

        if data.startswith("payer|"):
            payer = int(data[len("payer|"):])
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
            pid = int(data[len("toggle|"):])
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
                await query.answer("Выберите хотя бы одного участника!", show_alert=True)
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
            rows.append([InlineKeyboardButton(
                "✏️ Переименовать", callback_data=f"grename|{gid}"
            )])
            if self.repo.can_change_currency(gid):
                rows.append([InlineKeyboardButton(
                    f"💱 Валюта: {currency}", callback_data=f"gcur|{gid}"
                )])
        rows.append([InlineKeyboardButton(
            "👥 Участники", callback_data=f"members|{gid}|p:0"
        )])
        rows.append([InlineKeyboardButton(
            "🚪 Выйти из группы", callback_data=f"gleave|{gid}"
        )])
        if is_owner:
            rows.append([InlineKeyboardButton(
                "🗑 Удалить группу", callback_data=f"grpdel|gid:{gid}"
            )])
        rows.append([InlineKeyboardButton(
            "« Назад к группе", callback_data=f"mgsel|{gid}"
        )])

        await self._edit_or_send(update, "".join(lines), InlineKeyboardMarkup(rows))

    async def _send_currency_picker(self, update: Update, gid: int) -> None:
        current = self.repo.group_currency(gid)
        common = ["RUB", "USD", "EUR", "TRY", "GEL", "KZT", "AMD", "RSD", "THB", "AED"]
        rows, row = [], []
        for code in common:
            mark = "✅ " if code == current else ""
            row.append(InlineKeyboardButton(
                f"{mark}{code}", callback_data=f"gcurset|{gid}|{code}"
            ))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton(
            "Другая — ввести код", callback_data=f"gcurother|{gid}"
        )])
        rows.append([InlineKeyboardButton(
            "« Назад", callback_data=f"gset|{gid}"
        )])
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
                    "Не знаю такую валюту. Пришлите трёхбуквенный код,"
                    " напр. NOK.",
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
        heir = self.repo.next_owner(gid, uid) if self.repo.is_group_owner(gid, uid) else 0
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
    ) -> None:
        total = self.repo.count_group_expenses(gid)
        offset = page * EXPENSES_PER_PAGE
        if total > 0 and offset >= total:
            page = 0
            offset = 0

        items = self.repo.list_group_expenses(gid, EXPENSES_PER_PAGE, offset)

        if total == 0:
            await self._edit_or_send(
                update,
                "В группе пока нет трат.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")
                ]]),
            )
            return

        currency = self.repo.group_currency(gid)
        names = self.repo.names_for(
            {it["payer"] for it in items} | {it["created_by"] for it in items if it["created_by"]}
        )
        lines = [f"Траты группы #{gid} (страница {page + 1}), валюта {currency}:\n"]
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
            rows.append([InlineKeyboardButton(
                f"#{it['id']} {it['desc']} —"
                f" {format_cents(it['amount_cents'], currency)}{mark}",
                callback_data=f"expcard|{it['id']}|gid:{gid}|p:{page}",
            )])
        nav = [InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")]
        if page > 0:
            nav.insert(0, InlineKeyboardButton(
                "« Назад", callback_data=f"explist|{gid}|p:{page - 1}"
            ))
        if offset + EXPENSES_PER_PAGE < total:
            nav.append(InlineKeyboardButton(
                "Вперёд »", callback_data=f"explist|{gid}|p:{page + 1}"
            ))
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
            await self._edit_or_send(
                update,
                "Трата не найдена — возможно, её удалили.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "К списку трат", callback_data=f"explist|{gid}|p:{page}"
                    )
                ]]),
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
                item["amount_cents"], item["orig_amount_cents"],
                currency, item["orig_currency"],
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

        rows = []
        if item["receipt"]:
            rows.append([InlineKeyboardButton(
                "🧾 Показать чек", callback_data=f"expshow|{eid}|gid:{gid}|p:{page}"
            )])
        if self.repo.can_edit_expense(eid, gid, uid):
            rows.append([InlineKeyboardButton(
                "✏️ Изменить", callback_data=f"expedit|{eid}|gid:{gid}|p:{page}"
            )])
            rows.append([InlineKeyboardButton(
                "📎 Заменить чек" if item["receipt"] else "📎 Приложить чек",
                callback_data=f"exprcpt|{eid}|gid:{gid}|p:{page}",
            )])
            if item["receipt"]:
                rows.append([InlineKeyboardButton(
                    "🗑 Убрать чек", callback_data=f"exprdel|{eid}|gid:{gid}|p:{page}"
                )])
            rows.append([InlineKeyboardButton(
                "🗑 Удалить трату", callback_data=f"expdel|{eid}|gid:{gid}|p:{page}"
            )])
        rows.append([InlineKeyboardButton(
            "« К списку трат", callback_data=f"explist|{gid}|p:{page}"
        )])

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
        _set_ae(ctx, {
            "group_id": gid,
            "expense_id": eid,
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
        })
        current = format_cents(item["amount_cents"], currency)
        await update.effective_chat.send_message(
            f"Меняем трату #{eid}. Сейчас: {item['desc']} — {current}.\n"
            f"Пришлите новую сумму и описание одним сообщением"
            f" (валюта группы — {currency}).",
            reply_markup=inline_cancel(),
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
        self.repo.set_receipt(eid, gid, photos[-1].file_id)
        _del_receipt(ctx)
        await update.effective_chat.send_message(
            f"Чек приложен к трате #{eid}.", reply_markup=main_keyboard()
        )
        await self._send_expense_card(update, ctx, gid, eid, target.get("page", 0))

    async def _send_members_page(
        self, update: Update, gid: int, page: int
    ) -> None:
        members = self.repo.list_members_detailed(gid)
        total = len(members)

        if total == 0:
            await self._edit_or_send(
                update,
                "В группе пока нет участников.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")
                ]]),
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
                    rows.append([InlineKeyboardButton(
                        f"{m['name']}: {format_cents(balance, currency)} — не удалить",
                        callback_data=f"memnote|{gid}|p:{page}",
                    )])
                else:
                    rows.append([InlineKeyboardButton(
                        f"🚫 Удалить {m['name']}",
                        callback_data=f"memdel|{m['id']}|gid:{gid}|p:{page}",
                    )])

        nav = [InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")]
        if page > 0:
            nav.insert(0, InlineKeyboardButton(
                "« Назад", callback_data=f"members|{gid}|p:{page - 1}"
            ))
        if end < total:
            nav.append(InlineKeyboardButton(
                "Вперёд »", callback_data=f"members|{gid}|p:{page + 1}"
            ))
        rows.append(nav)

        await self._edit_or_send(update, "".join(lines), InlineKeyboardMarkup(rows))

    async def _send_settlements_page(
        self, update: Update, gid: int, page: int
    ) -> None:
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
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")
                ]]),
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
                rows.append([InlineKeyboardButton(
                    f"Отменить #{item['id']}",
                    callback_data=f"setdel|{item['id']}|gid:{gid}|p:{page}",
                )])

        nav = [InlineKeyboardButton("Назад к группе", callback_data=f"mgsel|{gid}")]
        if page > 0:
            nav.insert(0, InlineKeyboardButton(
                "« Назад", callback_data=f"setlist|{gid}|p:{page - 1}"
            ))
        if offset + SETTLEMENTS_PER_PAGE < total:
            nav.append(InlineKeyboardButton(
                "Вперёд »", callback_data=f"setlist|{gid}|p:{page + 1}"
            ))
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
            [InlineKeyboardButton(
                f"Плательщик: {m['name']}", callback_data=f"payer|{m['id']}"
            )]
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
            rows.append([InlineKeyboardButton(label, callback_data=f"toggle|{m['id']}")])
        rows.append([
            InlineKeyboardButton("Все", callback_data="part_all"),
            InlineKeyboardButton("Я и плательщик", callback_data="part_me_payer"),
        ])
        rows.append([
            InlineKeyboardButton("Очистить", callback_data="part_clear"),
            InlineKeyboardButton("Готово →", callback_data="part_done"),
        ])
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
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, st: dict
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
            await self._edit_or_send(update, "Участник не найден в группе. Начните заново.")
            return

        editing = st.get("expense_id") or 0
        if editing and not self.repo.can_edit_expense(editing, group_id, uid):
            _del_ae(ctx)
            await self._edit_or_send(
                update, "Менять трату может только тот, кто её добавил."
            )
            return

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
                )
        except Exception as e:
            logger.exception("failed to save expense")
            await self._edit_or_send(update, f"Ошибка сохранения траты: {e}")
            return

        page = st.get("page", 0)
        _del_ae(ctx)

        # Notify other participants of their share
        expense_shares = self.repo.get_expense_shares(expense_id)
        title = self.repo.get_group_title(group_id)
        base = st.get("currency") or self.repo.group_currency(group_id)
        paid_in = ""
        if st.get("orig_currency"):
            paid_in = (
                f" (оплачено"
                f" {format_cents(st['orig_amount_cents'], st['orig_currency'])})"
            )
        # An edit moves money between people just as an new expense does,
        # so everyone it touches hears about it.
        verb = "изменена трата" if editing else "добавлена трата"
        for pid, share in expense_shares.items():
            if pid == uid:
                continue
            try:
                await ctx.bot.send_message(
                    pid,
                    f"В группе #{group_id} ({title}) {verb} #{expense_id}:"
                    f" {st['description']} —"
                    f" {format_cents(st['amount_cents'], base)}{paid_in}.\n"
                    f"Ваша доля: {format_cents(share, base)}."
                    f" Плательщик: {self.repo.user_name(st['payer'])}.",
                )
            except Exception:
                logger.info("failed to notify expense participant %s", pid)

        await self._edit_or_send(
            update,
            f"Трата #{expense_id} {'изменена' if editing else 'добавлена'}."
            f" Сумма {format_cents(st['amount_cents'], base)}{paid_in},"
            f" плательщик {self.repo.user_name(st['payer'])}.",
            InlineKeyboardMarkup([[InlineKeyboardButton(
                "Открыть трату",
                callback_data=f"expcard|{expense_id}|gid:{group_id}|p:{page}",
            )]]),
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
                lines.append(f"• вы → {names[info['to']]}: {amount} — ждём подтверждения\n")
            else:
                lines.append(f"• {names[info['from']]} → вам: {amount} — подтвердите получение\n")
        return lines

    async def _show_debts(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
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
                rows.append([
                    InlineKeyboardButton(
                        f"✅ Получил(а) от {names[info['from']]}: {amount}",
                        callback_data=f"paycfm|{info['batch']}",
                    ),
                ])
                rows.append([
                    InlineKeyboardButton(
                        f"❌ Не получал(а) от {names[info['from']]}",
                        callback_data=f"payrej|{info['batch']}",
                    ),
                ])
            else:
                rows.append([InlineKeyboardButton(
                    f"↩️ Отменить запрос к {names[info['to']]}",
                    callback_data=f"payrej|{info['batch']}",
                )])

        for other, currency, amount, name, _ in you_owe:
            if (other, currency) in awaiting:
                continue
            rows.append([InlineKeyboardButton(
                f"Оплатил(а) {name}: {format_cents(amount, currency)}",
                callback_data=f"paynet|to:{other}|cur:{currency}|amt:{amount}",
            )])
            rows.append([InlineKeyboardButton(
                f"Отдал(а) часть {name} ({currency})",
                callback_data=f"paypart|to:{other}|cur:{currency}",
            )])
        for other, currency, name, _ in even:
            if (other, currency) in awaiting:
                continue
            rows.append([InlineKeyboardButton(
                f"✅ Закрыть расчёты с {name} ({currency})",
                callback_data=f"paynet|to:{other}|cur:{currency}|amt:0",
            )])

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
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтверждаю", callback_data=f"paycfm|{batch}")],
            [InlineKeyboardButton("❌ Не получал(а)", callback_data=f"payrej|{batch}")],
        ])
        try:
            await ctx.bot.send_message(other, text, reply_markup=markup)
        except Exception:
            logger.info("failed to notify %s about a payment claim", other)
        return True

# ---------- Entry point ----------

def main() -> None:
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        raise RuntimeError("BOT_TOKEN env required")

    db_path = os.environ.get("DB_PATH", "./data.db")
    repo = Repo(db_path)
    bot_app = App(repo)

    async def post_init(application: Application) -> None:
        me = await application.bot.get_me()
        bot_app.base = me.username
        logger.info("Starting bot @%s …", me.username)

    # Wizard state lives in user_data, so without persistence a restart in
    # the middle of adding an expense leaves the user answering questions
    # nobody is listening to any more.
    state_path = os.environ.get("STATE_PATH", "./bot_state.pickle")
    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .persistence(PicklePersistence(filepath=state_path))
        .build()
    )

    application.add_handler(CommandHandler("start", bot_app.on_start))
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
