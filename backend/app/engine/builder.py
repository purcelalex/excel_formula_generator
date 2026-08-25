"""
builder.py — resolved intent + resolved columns to a formula tree.

Each builder returns (node, rationale). The node is an AST from ast.py; the
rationale is a plain-language sentence explaining which column was used for
what, shown to the user so they can spot a wrong mapping immediately.

No builder produces text. If a builder cannot map columns confidently it
returns None and the caller falls back to the knowledge base's example, clearly
labelled as a pattern to adapt rather than an answer.
"""

from __future__ import annotations

from ..ingestion.models import ParsedTable
from .ast import Cell, Criterion as CriterionNode, Func, Node, Number, Text, col_range
from .intents import Intent
from .resolver import (
    Criterion,
    content_words,
    explicit_columns,
    explicit_columns_by_role,
    first_column_of_type,
    first_value_column,
    match_column,
)


def _criterion_node(criterion: Criterion | None) -> Node:
    """Turn a resolved criterion into an argument node.

    A missing criterion becomes the literal "?" on purpose. A visible marker the
    user must fill in is safer than a plausible guess they might not check.
    """
    if criterion is None:
        return Text("?")
    if criterion.operator:
        return CriterionNode(criterion.operator, criterion.value)
    if isinstance(criterion.value, (int, float)):
        return Number(criterion.value)
    return Text(str(criterion.value))


def _named_column_of_type(description: str, table: ParsedTable, type_name: str) -> int | None:
    """First column the user seems to name that has the required type."""
    for word in content_words(description):
        index = match_column(word, table)
        if index is not None and table.types[index] == type_name:
            return index
    return None


def _named_column_not_of_type(description: str, table: ParsedTable, type_name: str) -> int | None:
    for word in content_words(description):
        index = match_column(word, table)
        if index is not None and table.types[index] != type_name:
            return index
    return None


def _letter_to_index(letter: str, table: ParsedTable) -> int | None:
    try:
        return table.column_letters.index(letter)
    except ValueError:
        return None


def build_conditional_aggregate(
    intent: Intent, description: str, table: ParsedTable, criterion: Criterion | None
) -> tuple[Node, str] | None:
    """SUMIF / AVERAGEIF — a criteria range, a criterion, and a value range."""
    criteria_index = criterion.column_index if criterion else None
    if criteria_index is None:
        criteria_index = _named_column_not_of_type(description, table, "number")

    value_index = _named_column_of_type(description, table, "number")
    if value_index is None:
        value_index = first_value_column(table)

    # Explicit "column A / column C" references override inference entirely.
    # Roles come from position around the condition word, not from order of
    # appearance: in "sum column B where column A is a city", B is summed and A
    # is filtered.
    before, after = explicit_columns_by_role(description)
    if after:
        named_criteria = _letter_to_index(after[0], table)
        if named_criteria is not None:
            criteria_index = named_criteria
        if before:
            named_value = _letter_to_index(before[0], table)
            if named_value is not None:
                value_index = named_value
    elif len(before) >= 2:
        first, second = _letter_to_index(before[0], table), _letter_to_index(before[1], table)
        if first is not None and second is not None:
            criteria_index, value_index = first, second

    # A pure numeric comparison ("sum prices over 120") filters the value column
    # itself. Excel and Sheets both allow the two-argument form, where the
    # criteria range doubles as the sum range — clearer than repeating it.
    if criteria_index is None and criterion is not None and criterion.operator and value_index is not None:
        node = Func(
            intent.aggregate,
            [col_range(table.column_letters[value_index], table.n_rows), _criterion_node(criterion)],
        )
        rationale = (
            f"{intent.aggregate} over column {table.column_letters[value_index]} "
            f"({table.headers[value_index]}), keeping only values "
            f"{criterion.operator} {criterion.value}"
        )
        return node, rationale

    if criteria_index is None or value_index is None or criteria_index == value_index:
        return None

    rows = table.n_rows
    node = Func(
        intent.aggregate,
        [
            col_range(table.column_letters[criteria_index], rows),
            _criterion_node(criterion),
            col_range(table.column_letters[value_index], rows),
        ],
    )
    rationale = (
        f"filtering on column {table.column_letters[criteria_index]} "
        f"({table.headers[criteria_index]}), "
        f"{intent.aggregate.replace('IF', '').lower()} over column "
        f"{table.column_letters[value_index]} ({table.headers[value_index]})"
    )
    return node, rationale


def build_conditional_count(
    intent: Intent, description: str, table: ParsedTable, criterion: Criterion | None
) -> tuple[Node, str] | None:
    """COUNTIF — one range and one criterion, no separate value column."""
    index = criterion.column_index if criterion else None
    if index is None:
        for word in content_words(description):
            candidate = match_column(word, table)
            if candidate is not None:
                index = candidate
                break

    letters = explicit_columns(description)
    if letters:
        explicit = _letter_to_index(letters[0], table)
        if explicit is not None:
            index = explicit

    if index is None:
        return None

    node = Func(
        intent.aggregate,
        [col_range(table.column_letters[index], table.n_rows), _criterion_node(criterion)],
    )
    rationale = (
        f"counting rows in column {table.column_letters[index]} "
        f"({table.headers[index]}) that match the condition"
    )
    return node, rationale


def build_plain_aggregate(
    intent: Intent, description: str, table: ParsedTable, criterion: Criterion | None
) -> tuple[Node, str] | None:
    """SUM / AVERAGE / MAX / MIN / COUNTA over a single column."""
    index = _named_column_of_type(description, table, "number")
    if index is None:
        index = first_value_column(table)

    letters = explicit_columns(description)
    if letters:
        explicit = _letter_to_index(letters[0], table)
        if explicit is not None:
            index = explicit

    # COUNTA counts non-empty cells of any type, so it does not need a numeric
    # column; the others do.
    if index is None and intent.aggregate == "COUNTA" and table.headers:
        index = 0
    if index is None:
        return None

    node = Func(intent.aggregate, [col_range(table.column_letters[index], table.n_rows)])
    rationale = (
        f"{intent.aggregate} over column {table.column_letters[index]} "
        f"({table.headers[index]})"
    )
    return node, rationale


def build_lookup(
    intent: Intent, description: str, table: ParsedTable, criterion: Criterion | None
) -> tuple[Node, str] | None:
    """XLOOKUP — search a key column, return the matching value from another.

    The searched-for value goes in a cell the user fills in. XLOOKUP is used
    rather than VLOOKUP because it does not break when columns are reordered and
    it takes a not-found argument, which turns a confusing #N/A into a message.
    """
    key_index = criterion.column_index if criterion else None
    if key_index is None:
        key_index = _named_column_not_of_type(description, table, "number")
    if key_index is None:
        key_index = 0

    return_index = _named_column_of_type(description, table, "number")
    if return_index is None or return_index == key_index:
        return_index = first_value_column(table)
    if return_index is None or return_index == key_index:
        return_index = 1 if len(table.headers) > 1 else 0
    if return_index == key_index:
        return None

    # The input cell sits one column past the user's data, so it never overlaps.
    from ..ingestion.models import column_letter
    input_cell = f"{column_letter(len(table.headers))}2"

    rows = table.n_rows
    node = Func(
        intent.aggregate,
        [
            Cell(input_cell),
            col_range(table.column_letters[key_index], rows),
            col_range(table.column_letters[return_index], rows),
            Text("Not found"),
        ],
    )
    rationale = (
        f"searching column {table.column_letters[key_index]} ({table.headers[key_index]}) "
        f"and returning column {table.column_letters[return_index]} "
        f"({table.headers[return_index]}); put the value you are looking for in {input_cell}"
    )
    return node, rationale


BUILDERS = {
    "conditional_aggregate": build_conditional_aggregate,
    "conditional_count": build_conditional_count,
    "plain_aggregate": build_plain_aggregate,
    "lookup": build_lookup,
}


def build(
    intent: Intent, description: str, table: ParsedTable, criterion: Criterion | None
) -> tuple[Node, str] | None:
    builder = BUILDERS.get(intent.builder)
    if builder is None:
        return None
    return builder(intent, description, table, criterion)
