"""
resolver.py — work out WHICH columns the user meant.

This is the hardest part of the whole engine and the one that fails most
visibly, so every decision it makes is reported back to the user as a
`rationale` string rather than applied silently.

Three sources of evidence, in order of strength:

1. The criterion value appears in a column's sample cells. Strongest — this is
   the user's own data confirming the mapping, not a guess about their words.
2. A word in the description matches a header (exactly, as a substring, or
   through a bilingual synonym group).
3. Column type. A SUMIF's sum range must be numeric; its criteria range
   generally is not.
"""

from __future__ import annotations

import re

from ..ingestion.models import ParsedTable
from .language import normalize

# Bilingual synonym groups. Both the user's word AND the header must belong to
# the same group for the bridge to apply — otherwise 'City' would match almost
# anything through a chain of loose associations.
FIELD_SYNONYMS = {
    "price": ["price", "cost", "amount", "value", "pret", "suma", "valoare", "total", "venit"],
    "city": ["city", "town", "location", "oras", "localitate", "locatie"],
    "status": ["status", "state", "stare"],
    "name": ["name", "product", "client", "customer", "nume", "produs", "denumire", "articol"],
    "date": ["date", "day", "month", "data", "zi", "luna"],
    "quantity": ["quantity", "qty", "count", "cantitate", "numar", "bucati", "buc"],
    "volume": ["ounces", "ounce", "oz", "volume", "size", "uncii", "uncie", "volum", "marime",
               "ml", "litri", "litre", "liters"],
    "strength": ["abv", "alcohol", "strength", "alcool", "taria", "concentratie", "ibu"],
    "category": ["category", "type", "group", "categorie", "tip", "grup"],
    "region": ["region", "country", "county", "regiune", "tara", "judet"],
}

# A word shorter than this is never matched to a header by substring. Real
# column names are longer, and short function words appear inside them by
# coincidence far more often than by meaning.
_MIN_SUBSTRING_MATCH = 3

_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "is", "are",
    "all", "each", "every", "column", "columns", "row", "rows", "where", "with",
    "what", "how", "many", "much", "me", "my", "please", "value", "values",
    "coloana", "coloane", "rand", "randuri", "din", "pe", "la", "si", "sau",
    "este", "sunt", "unde", "care", "toate", "un", "o", "cu", "pentru",
    "de", "al", "ale", "lui", "ei", "mi", "sa", "se", "calculeaza", "calculati",
    "vreau", "as", "vrea", "te", "rog", "valoare", "valori",
}

# Explicit column references: "column B", "coloana B", "col. B"
_EXPLICIT_COLUMN = re.compile(r"(?:column|coloana|col\.?)\s+([a-z])\b")


def explicit_columns(description: str) -> list[str]:
    """Column letters the user named directly, e.g. 'column B'."""
    return [m.upper() for m in _EXPLICIT_COLUMN.findall(normalize(description))]


# Words that separate the thing being aggregated from the thing being filtered.
_CONDITION_MARKERS = (
    "where", "when", "if", "only", "with", "having",
    "unde", "cand", "daca", "doar", "numai", "care",
)


def explicit_columns_by_role(description: str) -> tuple[list[str], list[str]]:
    """Split explicitly named columns around the condition word.

    In both languages the aggregated column comes before the condition and the
    filtered column after it:

        sum column B where column A is a city
            ^ value          ^ criteria

    Position, not order of appearance, is what tells the two apart — which is
    why "sum column B where column A" and "count column A where column B" both
    resolve correctly. Getting this wrong silently swaps the arguments of every
    SUMIF, so it is worth the few lines.
    """
    normalized = normalize(description)
    matches = [(m.group(1).upper(), m.start()) for m in _EXPLICIT_COLUMN.finditer(normalized)]
    if not matches:
        return [], []

    tokens = normalized.split()
    marker_position = None
    offset = 0
    for token in tokens:
        if token in _CONDITION_MARKERS:
            marker_position = offset
            break
        offset += len(token) + 1

    if marker_position is None:
        return [letter for letter, _ in matches], []

    before = [letter for letter, pos in matches if pos < marker_position]
    after = [letter for letter, pos in matches if pos > marker_position]
    return before, after


def header_score(word: str, header: str) -> float:
    """How well one word matches one header. 0 means no match."""
    w, h = normalize(word), normalize(header)
    if not w or not h or w in _STOPWORDS:
        return 0.0
    if w == h:
        return 3.0
    # Substring matching only for words long enough to be meaningful. Without
    # this, the Romanian preposition "de" matches the header "index", and a
    # request to average ABV averages a row counter instead.
    if len(w) >= _MIN_SUBSTRING_MATCH and len(h) >= _MIN_SUBSTRING_MATCH and (w in h or h in w):
        return 2.0

    w_tokens, h_tokens = set(w.split()), set(h.split())
    overlap = w_tokens & h_tokens
    if overlap:
        return 1.0 + 0.3 * len(overlap)

    for synonyms in FIELD_SYNONYMS.values():
        group = {normalize(s) for s in synonyms}
        if (w in group or w_tokens & group) and (h in group or h_tokens & group):
            return 1.5
    return 0.0


def match_column(word: str, table: ParsedTable) -> int | None:
    """Index of the header best matching `word`, or None."""
    best_score, best_index = 0.0, -1
    for i, header in enumerate(table.headers):
        score = header_score(word, header)
        if score > best_score:
            best_score, best_index = score, i
    return best_index if best_index >= 0 and best_score >= 1.0 else None


def column_containing(value: str, table: ParsedTable) -> int | None:
    """Index of the column whose sample cells contain `value`."""
    target = normalize(value)
    if not target:
        return None
    for i in range(len(table.headers)):
        for row in table.rows:
            if i < len(row) and normalize(row[i]) == target:
                return i
    return None


def first_column_of_type(table: ParsedTable, type_name: str) -> int | None:
    for i, column_type in enumerate(table.types):
        if column_type == type_name:
            return i
    return None


# Headers whose numbers are labels, not quantities. Averaging a row index or
# summing a product code is never what anyone asked for, so these are the last
# columns to fall back to rather than the first.
IDENTIFIER_HEADERS = {
    "id", "ids", "index", "idx", "key", "code", "row", "no", "nr", "num",
    "number", "rank", "position", "pos", "cod", "numar", "pozitie", "rand",
}


def _is_identifier_header(header: str) -> bool:
    normalized = normalize(header)
    if not normalized:
        return False
    tokens = set(normalized.split())
    return (
        normalized in IDENTIFIER_HEADERS
        or bool(tokens & IDENTIFIER_HEADERS)
        or normalized.endswith(" id")
        or normalized.endswith("_id")
    )


def first_value_column(table: ParsedTable) -> int | None:
    """The first numeric column that holds a quantity rather than a label.

    Used whenever a numeric column has to be guessed. Preferring any numeric
    column produced answers like AVERAGE over an "index" column — technically a
    number, obviously not what was meant. Identifier-looking columns are
    considered only when there is nothing else.
    """
    numeric = [i for i, t in enumerate(table.types) if t == "number"]
    if not numeric:
        return None
    for i in numeric:
        if not _is_identifier_header(table.headers[i]):
            return i
    return numeric[0]


_OPERATOR_WORDS = [
    (r"\b(greater than or equal|at least|cel putin|minim(um)?)\b", ">="),
    (r"\b(less than or equal|at most|cel mult|maxim(um)?)\b", "<="),
    (r"\b(greater than|more than|over|above|bigger|larger|peste|mai mar[ei])\b", ">"),
    (r"\b(less than|lower than|under|below|smaller|sub|mai mic[ei]?)\b", "<"),
    (r"\b(not equal|different|diferit[e]?)\b", "<>"),
]

_NUMBER_IN_TEXT = re.compile(r"(>=|<=|<>|>|<|=)?\s*(\d+(?:[.,]\d+)?)")
_QUOTED = re.compile(r'"([^"]+)"|\'([^\']+)\'')


class Criterion:
    """A resolved filter: the value, its operator, and where it lives."""

    def __init__(self, operator: str, value, column_index: int | None, source: str):
        self.operator = operator
        self.value = value
        self.column_index = column_index
        self.source = source          # how it was found, for the rationale

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Criterion({self.operator!r}, {self.value!r}, col={self.column_index})"


def resolve_criterion(description: str, table: ParsedTable | None) -> Criterion | None:
    """Find the filter value the user is asking about, and its column.

    Order matters. A quoted value is unambiguous. A value found in the table is
    next, because the user's own data confirms it. A bare number with a
    comparison word is last, since numbers appear in requests for many reasons.
    """
    normalized = normalize(description)

    # 1. Quoted value — the user told us exactly.
    quoted = _QUOTED.search(description)
    if quoted:
        value = quoted.group(1) or quoted.group(2)
        column = column_containing(value, table) if table else None
        return Criterion("", value, column, "quoted in the request")

    # 2. A word from the request that appears in the table's sample data.
    if table:
        # Two characters is the minimum: real category values like "IT" or "RO"
        # are short, and a token only counts if it matches a cell exactly.
        for token in re.findall(r"[A-Za-zĂÂÎȘȚăâîșț][\wĂÂÎȘȚăâîșț-]+", description):
            if normalize(token) in _STOPWORDS:
                continue
            column = column_containing(token, table)
            if column is not None:
                return Criterion("", token, column, f"found in column {table.column_letters[column]}")

    # 3. A number, with an operator taken from words or symbols.
    number = _NUMBER_IN_TEXT.search(description)
    if number:
        operator = number.group(1) or ""
        if not operator:
            for pattern, symbol in _OPERATOR_WORDS:
                if re.search(pattern, normalized):
                    operator = symbol
                    break
        raw = number.group(2).replace(",", ".")
        value = float(raw) if "." in raw else int(raw)
        # A bare number with no comparison at all is more likely part of the
        # phrasing than a filter, so it only counts when an operator is present.
        if operator:
            return Criterion(operator, value, None, "numeric comparison in the request")

    return None


def content_words(description: str) -> list[str]:
    """Words worth trying against headers — stopwords and numbers removed."""
    return [
        token
        for token in normalize(description).split()
        if token not in _STOPWORDS and not token.isdigit()
    ]
