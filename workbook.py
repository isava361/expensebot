"""Excel workbook generation and group reports."""

import html
import re
import time
import zipfile
from io import BytesIO
from core import (
    now_unix,
    format_cents,
    tz_label,
    format_time,
)

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
        longest = max(
            (len(_cell_text(r[c])) for r in all_rows if c < len(r)), default=0
        )
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
        (
            '<sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            "</sheetView></sheetViews>"
        )
        if header
        else ""
    )

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


def _expenses_sheet(
    data: dict, order: list[int], tz: int
) -> tuple[str, list, list[list]]:
    """One row per expense, one share column per person.

    Reading across a row shows who paid and how the amount was cut up;
    reading down a person's column gives everything they consumed. The two
    totals at the bottom are what the balances sheet starts from.
    """
    names = data["names"]
    base = data["currency"]
    header = [
        "№",
        "Дата",
        "Описание",
        f"Сумма, {base}",
        f"Сумма долей, {base}",
        "Кто платил",
        "Кто добавил",
        "Оплачено в валюте",
        "Сумма в валюте",
        f"Курс к {base}",
        "Чек",
        "Изменена",
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
        [
            "",
            "",
            "ИТОГО",
            Money(total),
            Money(sum(share_totals.values())),
            "",
            "",
            "",
            None,
            None,
            "",
            "",
        ]
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
        "Участник",
        f"Оплатил, {base}",
        f"Его доля, {base}",
        f"Отдал по расчётам, {base}",
        f"Получил по расчётам, {base}",
        f"Баланс, {base}",
        "Итог",
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
        rows.append(
            [
                names[uid],
                Money(paid[uid]),
                Money(consumed[uid]),
                Money(sent[uid]),
                Money(received[uid]),
                Money(balance),
                verdict,
            ]
        )
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
        "№",
        "Дата",
        "Кто отдал",
        "Кому",
        f"Сумма, {data['currency']}",
        "Статус",
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
        [
            [line]
            for line in [
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
            ]
        ],
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

    return build_xlsx(
        [
            _expenses_sheet(data, order, tz),
            _totals_sheet(data, order),
            _transfers_sheet(data),
            _settlements_sheet(data, tz),
            _howto_sheet(data, tz),
        ]
    )
