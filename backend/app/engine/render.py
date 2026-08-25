"""
render.py — turn a formula tree into text for one specific target.

This is the ONLY module that produces formula text. Separators, quoting and
function-name localisation live here and nowhere else.

Targets
-------
excel_us   Excel with a US/UK locale      =SUMIF(A2:A5,"X",C2:C5)
excel_eu   Excel with a RO/EU locale      =SUMIF(A2:A5;"X";C2:C5)
sheets     Google Sheets                  separator follows the user's locale,
                                          function names are ALWAYS English

Two facts drive the design, both verified against Microsoft and Google docs:

1. Google Sheets uses English function names in every locale. Only the argument
   separator changes.
2. Modern Excel accepts English function names regardless of display language,
   so emitting English names is always safe. `display_names` exists for showing
   the user the localised name alongside, never for the formula itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ast import Cell, Criterion, Func, Node, Number, Range, Raw, Text


@dataclass(frozen=True)
class Dialect:
    """How one target spells a formula."""
    name: str
    separator: str          # "," or ";"

    @property
    def is_eu(self) -> bool:
        return self.separator == ";"


EXCEL_US = Dialect("excel_us", ",")
EXCEL_EU = Dialect("excel_eu", ";")
SHEETS_US = Dialect("sheets_us", ",")
SHEETS_EU = Dialect("sheets_eu", ";")


def dialect_for(platform: str, locale: str) -> Dialect:
    """Pick a dialect from the detected platform and locale.

    platform: "excel" | "sheets" | "any"
    locale:   "eu" | "us"
    """
    eu = locale == "eu"
    if platform == "sheets":
        return SHEETS_EU if eu else SHEETS_US
    return EXCEL_EU if eu else EXCEL_US


def _quote(value: str) -> str:
    """Wrap in double quotes, escaping embedded quotes the spreadsheet way.

    Excel and Sheets both escape a literal quote by doubling it: a value
    containing one quote character comes back with that character doubled and
    the whole value wrapped in quotes.
    """
    return '"' + str(value).replace('"', '""') + '"'


def _number(value) -> str:
    """Render a number without a trailing .0 on whole values.

    The decimal separator is always a dot inside a formula, even in EU locales —
    only the ARGUMENT separator changes. Getting this backwards is the classic
    localisation bug, so it is stated here explicitly.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def render(node: Node, dialect: Dialect) -> str:
    """Render a node to text. Does not prepend '=' — see render_formula."""
    if isinstance(node, Func):
        args = dialect.separator.join(render(a, dialect) for a in node.args)
        return f"{node.name}({args})"

    if isinstance(node, Range):
        c = node.column
        return f"{c}{node.first_row}:{c}{node.last_row}"

    if isinstance(node, Cell):
        return node.ref

    if isinstance(node, Text):
        return _quote(node.value)

    if isinstance(node, Number):
        return _number(node.value)

    if isinstance(node, Criterion):
        value = _number(node.value) if isinstance(node.value, (int, float)) else str(node.value)
        return _quote(f"{node.op}{value}")

    if isinstance(node, Raw):
        return _localise_raw(node.text, dialect)

    raise TypeError(f"cannot render node of type {type(node).__name__}")


def render_formula(node: Node, dialect: Dialect) -> str:
    """Render a complete, ready-to-paste formula, '=' included."""
    body = render(node, dialect)
    return body if body.startswith("=") else f"={body}"


def _localise_raw(text: str, dialect: Dialect) -> str:
    """Swap separators in pre-built template text without touching string contents.

    Only used for knowledge-base examples, which are authored with commas. A
    comma inside a quoted string is data, not a separator, so the scan tracks
    whether it is inside quotes — and treats a doubled quote as an escaped quote
    rather than the end of the string.
    """
    if not dialect.is_eu:
        return text

    out: list[str] = []
    in_string = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            if in_string and i + 1 < len(text) and text[i + 1] == '"':
                out.append('""')     # escaped quote inside a string
                i += 2
                continue
            in_string = not in_string
            out.append(ch)
        elif ch == "," and not in_string:
            out.append(";")
        else:
            out.append(ch)
        i += 1
    return "".join(out)
