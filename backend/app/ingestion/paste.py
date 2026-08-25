"""
paste.py — copy-pasted table text to ParsedTable.

What arrives from an Excel or Sheets copy-paste is plain text in one of three
shapes: tab-separated (the usual case), comma-separated, or an HTML <table>
fragment (Google Sheets rich paste). Text cannot carry executable code, so the
job here is structure plus the sanitisation in sanitize.py — not malware
scanning, which would be theatre on a string.
"""

from __future__ import annotations

from html.parser import HTMLParser

from .models import ParsedTable, SafetyReport, column_letter
from .sanitize import (
    MAX_CHARS,
    MAX_COLS,
    MAX_DATA_ROWS,
    clean_cell,
    infer_type,
    looks_like_header,
)


class _TableHTMLParser(HTMLParser):
    """Minimal <table> reader for Google Sheets' rich-paste flavour.

    Deliberately ignores every tag except tr/td/th: we want the grid, not the
    styling, and not following anything else means no surprises from markup.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] = []
        self._cell: list[str] = []
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._in_cell = False
            self._row.append("".join(self._cell))
        elif tag == "tr" and self._row:
            self.rows.append(self._row)

    def handle_data(self, data):
        if self._in_cell:
            self._cell.append(data)


def _split_grid(text: str, report: SafetyReport) -> list[list[str]]:
    if "<table" in text.lower():
        report.delimiter = "html"
        parser = _TableHTMLParser()
        parser.feed(text)
        return parser.rows

    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return []

    # Tabs win ties: a spreadsheet copy-paste is tab-separated, and real data
    # often contains commas inside text cells.
    tabs = sum(line.count("\t") for line in lines)
    commas = sum(line.count(",") for line in lines)
    delimiter = "\t" if tabs >= commas else ","
    report.delimiter = "tab" if delimiter == "\t" else "comma"
    return [line.split(delimiter) for line in lines]


def parse_paste(pasted: str, has_header: bool | None = None) -> ParsedTable:
    """Parse pasted text into the shared table representation."""
    report = SafetyReport(source="paste", original_chars=len(pasted))

    if len(pasted) > MAX_CHARS:
        pasted = pasted[:MAX_CHARS]
        report.truncated_chars = True
        report.warnings.append(f"input truncated to {MAX_CHARS} characters")

    grid = _split_grid(pasted, report)
    if not grid:
        return ParsedTable([], [], [], [], report)

    return build_table(grid, report, has_header=has_header)


def build_table(
    grid: list[list[str]],
    report: SafetyReport,
    has_header: bool | None = None,
) -> ParsedTable:
    """Shared tail of every ingestion route: clean, split header, infer types.

    Both the paste route and the upload route land here, so the 4-row rule, the
    column cap and the injection defanging apply identically no matter how the
    table arrived.
    """
    n_cols = min(max(len(row) for row in grid), MAX_COLS)
    if any(len(row) > MAX_COLS for row in grid):
        report.truncated_cols = True
        report.warnings.append(f"only the first {MAX_COLS} columns were read")

    cleaned: list[list[str]] = []
    for row_index, row in enumerate(grid):
        padded = (row + [""] * n_cols)[:n_cols]
        cleaned.append(
            [clean_cell(padded[c], report, row_index + 1, c + 1) for c in range(n_cols)]
        )

    if has_header is None:
        has_header = looks_like_header(cleaned[0], cleaned[1:])

    if has_header:
        headers, data = cleaned[0], cleaned[1:]
    else:
        headers = [f"Column {column_letter(i)}" for i in range(n_cols)]
        data = cleaned

    report.rows_seen = len(data)

    # The 4-row rule: truncate with a notice rather than reject. The engine only
    # needs headers, types and a few sample values; making the user edit their
    # spreadsheet before they can use the tool would be a rule that protects us
    # by inconveniencing them.
    if len(data) > MAX_DATA_ROWS:
        data = data[:MAX_DATA_ROWS]
        report.truncated_rows = True
        report.warnings.append(
            f"using the first {MAX_DATA_ROWS} rows of {report.rows_seen}"
        )

    letters = [column_letter(i) for i in range(n_cols)]
    types = [infer_type([row[i] for row in data]) for i in range(n_cols)]

    report.rows_used = len(data)
    report.cols = n_cols
    return ParsedTable(headers, data, letters, types, report)
