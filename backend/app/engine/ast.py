"""
ast.py — a formula as a tree, not a string.

The whole engine builds one of these and hands it to the renderer. Nothing else
in the codebase concatenates formula text.

Why a tree matters
------------------
Wrapping a result in IFERROR, nesting SUMIFS inside ROUND, or switching the
argument separator for a Romanian locale are all trivial operations on a tree
and fragile text surgery on a string. Localisation, quoting and escaping happen
in exactly one place (render.py), so they are correct everywhere by construction
instead of correct wherever someone remembered to call a helper.

Nodes
-----
Func     a function call:        SUMIF(A2:A5, "Chisinau", C2:C5)
Range    a column range:         A2:A5
Cell     a single reference:     G2
Text     a string literal:       "Chisinau"       (quoted + escaped on render)
Number   a numeric literal:      100
Criterion a comparison:          ">=100"          (operator + value, quoted)
Raw      an escape hatch for knowledge-base example templates
"""

from __future__ import annotations

from dataclasses import dataclass, field


class Node:
    """Base class. Every node knows how to describe itself for debugging only —
    real output always goes through render.py."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.__dict__})"


@dataclass
class Func(Node):
    name: str                      # canonical ENGLISH name, e.g. "SUMIF"
    args: list = field(default_factory=list)


@dataclass
class Range(Node):
    """A column range with the header row excluded.

    `first_row` defaults to 2 because row 1 is the header in every table we
    parse. `last_row` is the last row of real data.
    """
    column: str                    # "A", "B", ... "AA"
    first_row: int = 2
    last_row: int = 100


@dataclass
class Cell(Node):
    ref: str                       # "G2"


@dataclass
class Text(Node):
    value: str


@dataclass
class Number(Node):
    value: float | int


@dataclass
class Criterion(Node):
    """A comparison used as a function argument.

    Excel and Sheets want the whole comparison inside one string:
    ">=100", "<>", "Chisinau". `op` is empty for plain equality.
    """
    op: str                        # "", ">", ">=", "<", "<=", "<>"
    value: str | float | int


@dataclass
class Raw(Node):
    """Pre-built formula text from the knowledge base's example field.

    Used only for the fallback path where no confident tree can be built. It is
    still localised by the renderer, but its internals are not inspected.
    """
    text: str


# ---------------------------------------------------------------------------
# Small constructors — the builder reads better with these than with raw nodes.
# ---------------------------------------------------------------------------

def col_range(letter: str, n_data_rows: int) -> Range:
    """Range covering the data rows of a column, header excluded.

    n_data_rows is the count of rows BELOW the header, so the last row index is
    n_data_rows + 1.
    """
    return Range(column=letter, first_row=2, last_row=n_data_rows + 1)


def wrap_iferror(node: Node, fallback: Node) -> Func:
    """Compose: IFERROR(<node>, <fallback>).

    This function existing at all is the point of the AST — it is three lines
    here and a regex nightmare in string-land.
    """
    return Func("IFERROR", [node, fallback])
