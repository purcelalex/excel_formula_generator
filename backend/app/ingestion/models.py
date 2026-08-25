"""
models.py — the one internal representation of a user's table.

Every input route (pasted text, uploaded .xlsx, uploaded .csv, and anything
added later) produces a ParsedTable and nothing else. The engine has no idea
where a table came from, which is what keeps a second input format from
becoming a second copy of the whole pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SafetyReport:
    """What the ingestion layer found and what it did about it.

    Returned to the user, not just logged: if we silently truncated their table
    or defanged a cell, they need to see it to trust the result.
    """
    source: str = ""                  # "paste" | "xlsx" | "csv"
    original_chars: int = 0
    rows_seen: int = 0                # rows present in the input
    rows_used: int = 0                # rows actually kept (the 4-row rule)
    cols: int = 0
    delimiter: str = ""
    truncated_rows: bool = False
    truncated_cols: bool = False
    truncated_chars: bool = False
    injection_cells: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Parsed into something usable. Defanged cells are not a failure —
        they are a handled event."""
        return self.rows_used > 0 and self.cols > 0

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "rows_seen": self.rows_seen,
            "rows_used": self.rows_used,
            "cols": self.cols,
            "delimiter": self.delimiter,
            "truncated_rows": self.truncated_rows,
            "truncated_cols": self.truncated_cols,
            "injection_cells_defanged": self.injection_cells,
            "warnings": self.warnings,
        }


@dataclass
class ParsedTable:
    headers: list[str]
    rows: list[list[str]]             # data rows only, header excluded
    column_letters: list[str]         # ["A", "B", ...] aligned with headers
    types: list[str]                  # per column: number|date|text|bool|empty
    report: SafetyReport

    @property
    def n_rows(self) -> int:
        """Number of data rows the formula's ranges should cover.

        Normally the rows we actually parsed. A table described in words rather
        than pasted has no sample rows but still needs a sensible range length,
        which the ingestion report carries.
        """
        if self.rows:
            return len(self.rows)
        return self.report.rows_used

    def columns_as_dicts(self) -> list[dict]:
        return [
            {"letter": letter, "header": header, "type": type_}
            for letter, header, type_ in zip(self.column_letters, self.headers, self.types)
        ]


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters
