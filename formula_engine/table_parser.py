"""
table_parser.py — read a COPY-PASTED spreadsheet table (plain text) safely.

What arrives from an Excel/Sheets copy-paste is text, not a file:
  - usually TAB-separated (TSV)
  - sometimes comma-separated (CSV)
  - occasionally an HTML <table> (Google Sheets rich paste)

So the security job here is INPUT VALIDATION + SANITIZATION, not antivirus:
  - hard caps on chars / rows / cols / cell length  (stops DoS-by-paste)
  - encoding normalization + control-char stripping
  - CSV / formula-injection detection & defanging  (cells starting = + - @)
  - treat every cell strictly as DATA, never as an instruction

Returns a ParsedTable (headers, rows, column letters, inferred types) and a
SafetyReport describing exactly what was found and neutralized.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser

# ---- limits (tune to taste; user pastes only a few rows) ----
MAX_CHARS = 20_000
MAX_ROWS = 50
MAX_COLS = 50
MAX_CELL_LEN = 500

# Cells beginning with these are the classic formula/DDE-injection vectors.
_INJECTION_PREFIXES = ("=", "+", "-", "@")
_CONTROL_CHARS = {c for c in map(chr, range(32)) if c not in "\t\n\r"}


@dataclass
class SafetyReport:
    original_chars: int = 0
    used_chars: int = 0
    rows: int = 0
    cols: int = 0
    delimiter: str = ""
    truncated_chars: bool = False
    truncated_rows: bool = False
    truncated_cols: bool = False
    injection_cells: list[str] = field(default_factory=list)   # "R2C3: =cmd..."
    warnings: list[str] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        # parseable and within limits; injection cells are defanged, not fatal
        return self.rows > 0 and self.cols > 0


@dataclass
class ParsedTable:
    headers: list[str]
    rows: list[list[str]]              # data rows only (header excluded)
    column_letters: list[str]          # ["A","B","C", ...] aligned to headers
    types: list[str]                   # per-column: number|date|text|bool|empty
    report: SafetyReport


# ---------- helpers ----------

def _col_letter(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA ..."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _clean_cell(cell: str, report: SafetyReport, r: int, c: int) -> str:
    # normalize unicode, strip control chars, cap length
    cell = unicodedata.normalize("NFC", cell)
    cell = "".join(ch for ch in cell if ch not in _CONTROL_CHARS)
    cell = cell.strip()
    if len(cell) > MAX_CELL_LEN:
        cell = cell[:MAX_CELL_LEN]
        report.warnings.append(f"R{r}C{c}: cell truncated to {MAX_CELL_LEN} chars")
    # formula / DDE injection: flag and defang (prefix with ')
    if cell[:1] in _INJECTION_PREFIXES:
        preview = cell[:24] + ("…" if len(cell) > 24 else "")
        report.injection_cells.append(f"R{r}C{c}: {preview}")
        cell = "'" + cell            # defang: treated as literal text everywhere
    return cell


class _TableHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell, self._in = [], [], [], False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._in, self._cell = True, []

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._in = False
            self._row.append("".join(self._cell))
        elif tag == "tr":
            if self._row:
                self.rows.append(self._row)

    def handle_data(self, data):
        if self._in:
            self._cell.append(data)


def _detect_and_split(text: str, report: SafetyReport) -> list[list[str]]:
    if "<table" in text.lower():
        report.delimiter = "html"
        p = _TableHTMLParser()
        p.feed(text)
        return p.rows
    lines = [ln for ln in text.split("\n") if ln.strip() != ""]
    if not lines:
        return []
    tabs = sum(ln.count("\t") for ln in lines)
    commas = sum(ln.count(",") for ln in lines)
    delim = "\t" if tabs >= commas else ","
    report.delimiter = "tab" if delim == "\t" else "comma"
    return [ln.split(delim) for ln in lines]


def _infer_type(values: list[str]) -> str:
    vals = [v for v in values if v != ""]
    if not vals:
        return "empty"
    def is_num(v):
        return bool(re.fullmatch(r"-?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|-?\d+(?:[.,]\d+)?", v.replace(" ", "")))
    def is_date(v):
        return bool(re.fullmatch(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", v))
    def is_bool(v):
        return v.lower() in ("true", "false", "da", "nu", "yes", "no")
    if all(is_num(v) for v in vals):
        return "number"
    if all(is_date(v) for v in vals):
        return "date"
    if all(is_bool(v) for v in vals):
        return "bool"
    return "text"


def _looks_like_header(first: list[str], rest: list[list[str]]) -> bool:
    """Heuristic: header row is mostly non-numeric while data below has numbers."""
    if not rest:
        return True
    def numeric_frac(row):
        n = sum(1 for c in row if re.fullmatch(r"-?\d+(?:[.,]\d+)?", c.strip()))
        return n / max(len(row), 1)
    head_num = numeric_frac(first)
    body_num = sum(numeric_frac(r) for r in rest) / len(rest)
    return head_num < 0.3 and body_num > head_num


# ---------- public API ----------

def parse_table(pasted: str, has_header: bool | None = None) -> ParsedTable:
    report = SafetyReport(original_chars=len(pasted))
    if len(pasted) > MAX_CHARS:
        pasted = pasted[:MAX_CHARS]
        report.truncated_chars = True
        report.warnings.append(f"input truncated to {MAX_CHARS} chars")
    report.used_chars = len(pasted)

    grid = _detect_and_split(pasted, report)
    if len(grid) > MAX_ROWS:
        grid = grid[:MAX_ROWS]
        report.truncated_rows = True
    if not grid:
        return ParsedTable([], [], [], [], report)

    ncols = min(max(len(r) for r in grid), MAX_COLS)
    if any(len(r) > MAX_COLS for r in grid):
        report.truncated_cols = True
    # pad/truncate every row to ncols, and clean every cell
    clean = []
    for ri, row in enumerate(grid):
        row = (row + [""] * ncols)[:ncols]
        clean.append([_clean_cell(row[ci], report, ri + 1, ci + 1) for ci in range(ncols)])

    if has_header is None:
        has_header = _looks_like_header(clean[0], clean[1:])

    if has_header:
        headers, data = clean[0], clean[1:]
    else:
        headers = [f"Column {_col_letter(i)}" for i in range(ncols)]
        data = clean

    letters = [_col_letter(i) for i in range(ncols)]
    types = [_infer_type([row[i] for row in data]) for i in range(ncols)]

    report.rows = len(data)
    report.cols = ncols
    return ParsedTable(headers, data, letters, types, report)
