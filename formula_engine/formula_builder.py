"""
formula_builder.py — turn (description + parsed table) into a filled formula.

Uses the parsed table's real headers, sample values, and inferred column types
to resolve which column is which, so the output uses REAL column letters instead
of "?" placeholders. Falls back to the retrieval engine's template when the
table doesn't give enough to fill confidently.
"""

from __future__ import annotations

import re
import unicodedata

from engine import FormulaEngine, apply_locale, detect_language, detect_locale, detect_platform
from table_parser import ParsedTable

# bilingual field synonyms -> canonical concept, to match user words to headers
_FIELD_SYNONYMS = {
    "price": ["price", "pret", "cost", "amount", "suma", "valoare", "value", "total"],
    "city": ["city", "oras", "location", "locatie", "town", "localitate"],
    "status": ["status", "stare", "state"],
    "name": ["name", "nume", "product", "produs", "denumire", "client"],
    "date": ["date", "data", "day", "zi"],
    "quantity": ["quantity", "cantitate", "qty", "count", "numar", "buc"],
    "category": ["category", "categorie", "type", "tip", "grup", "group"],
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def _header_score(word: str, header: str) -> float:
    w, h = _norm(word), _norm(header)
    if not w or not h:
        return 0.0
    if w == h:
        return 3.0
    if w in h or h in w:
        return 2.0
    wt, ht = set(w.split()), set(h.split())
    overlap = wt & ht
    if overlap:
        return 1.0 + 0.3 * len(overlap)
    # synonym bridge: BOTH the user's word and the header must belong to the
    # same concept group (fixes headers like "City" matching any random word).
    for syns in _FIELD_SYNONYMS.values():
        norm_syns = {_norm(s) for s in syns}
        w_in = any(s == w or s in wt for s in norm_syns)
        h_in = any(s == h or s in ht for s in norm_syns)
        if w_in and h_in:
            return 1.5
    return 0.0


def _match_column(word: str, table: ParsedTable):
    """Return (index, letter, header) of the best header match for `word`, or None."""
    best, best_i = 0.0, -1
    for i, h in enumerate(table.headers):
        sc = _header_score(word, h)
        if sc > best:
            best, best_i = sc, i
    if best_i >= 0 and best >= 1.0:
        return best_i, table.column_letters[best_i], table.headers[best_i]
    return None


def _value_in_column(value: str, table: ParsedTable):
    """If `value` appears among a column's sample cells, return that column index."""
    v = _norm(value)
    if not v:
        return None
    for i in range(len(table.headers)):
        for row in table.rows:
            if i < len(row) and _norm(row[i]) == v:
                return i
    return None


def _first_numeric_col(table: ParsedTable):
    for i, t in enumerate(table.types):
        if t == "number":
            return i
    return None


def _extract_fields(text: str):
    """Pull candidate field words the user mentioned (nouns around key verbs)."""
    t = _norm(text)
    return t.split()


def _extract_criterion(text: str, table: ParsedTable):
    """Find the filter value + which column it belongs to.
    Priority: quoted value -> value found in a sample column -> number w/ operator."""
    m = re.search(r'"([^"]+)"|\'([^\']+)\'', text)
    if m:
        val = m.group(1) or m.group(2)
        col = _value_in_column(val, table)
        return f'"{val}"', col
    # number with comparison operator
    mnum = re.search(r"(>=|<=|>|<|=)?\s*(\d+(?:\.\d+)?)", text)
    # try to match any capitalized / proper token against sample cells
    for tok in re.findall(r"[A-Za-zĂÂÎȘȚăâîșț]{3,}", text):
        col = _value_in_column(tok, table)
        if col is not None:
            return f'"{tok}"', col
    if mnum:
        op = mnum.group(1) or ">"
        return f'"{op}{mnum.group(2)}"', None
    return None, None


def _R(letter: str, n_rows: int) -> str:
    """Data range for a column, header excluded: e.g. B2:B<last>."""
    last = n_rows + 1  # +1 because row 1 is the header
    return f"{letter}2:{letter}{last}"


_SUM_WORDS = {"sum", "aduna", "adauga", "suma", "total", "insumare"}
_AVG_WORDS = {"average", "mean", "avg", "medie", "media"}
_COUNT_WORDS = {"count", "numara", "cate", "cati", "how"}   # "how many" -> how+many
_LOOKUP_WORDS = {"lookup", "look", "cauta", "cautare", "gaseste", "vlookup", "xlookup", "find"}
_COND_WORDS = {"where", "if", "only", "unde", "daca", "conditie", "conditii",
               "have", "has", "with", "for", "per", "pentru", "cu", "care"}


def _resolve_intent(description, table, crit_col_idx, retrieval_fid):
    """Decide the function using query verbs + table evidence (ground truth),
    falling back to the retrieval result. A criterion value found inside a
    sample column is strong evidence that a *conditional* variant is wanted."""
    toks = set(_norm(description).split())
    has_condition = (crit_col_idx is not None) or bool(toks & _COND_WORDS)

    if toks & _LOOKUP_WORDS:
        return "vlookup"
    if toks & _SUM_WORDS:
        return "sumif" if has_condition else "sum"
    if toks & _AVG_WORDS:
        return "averageif" if has_condition else "average"
    if toks & _COUNT_WORDS:
        return "countif" if has_condition else "count"
    return retrieval_fid


class TableAwareBuilder:
    def __init__(self, engine: FormulaEngine | None = None):
        self.engine = engine or FormulaEngine()

    def build(self, description: str, table: ParsedTable):
        language = detect_language(description)
        platform = detect_platform(description)
        locale = detect_locale(description, language)
        n = len(table.rows)

        hits = self.engine.search(description, top_n=3, platform_filter=platform)
        best = hits[0][1] if hits else None
        words = _extract_fields(description)

        formula = None
        rationale = []

        crit_val, crit_col_idx = _extract_criterion(description, table)
        # table-grounded intent overrides keyword-retrieval quirks
        fid = _resolve_intent(description, table, crit_col_idx,
                              best["id"] if best else None)

        # ---- conditional SUM ----
        if fid in ("sumif", "sumifs"):
            # criteria column: from matched value, else best header match to a mentioned field
            crit_col = crit_col_idx
            if crit_col is None:
                for w in words:
                    m = _match_column(w, table)
                    if m and table.types[m[0]] != "number":
                        crit_col = m[0]; break
            # sum column: a numeric column the user named, else first numeric column
            sum_col = None
            for w in words:
                m = _match_column(w, table)
                if m and table.types[m[0]] == "number":
                    sum_col = m[0]; break
            if sum_col is None:
                sum_col = _first_numeric_col(table)
            if crit_col is not None and sum_col is not None:
                cl, sl = table.column_letters[crit_col], table.column_letters[sum_col]
                formula = f"=SUMIF({_R(cl,n)}, {crit_val or '\"?\"'}, {_R(sl,n)})"
                rationale.append(f"criteria = column {cl} ({table.headers[crit_col]}), "
                                 f"sum = column {sl} ({table.headers[sum_col]})")

        # ---- conditional COUNT ----
        elif fid in ("countif", "countifs"):
            col = crit_col_idx
            if col is None:
                for w in words:
                    m = _match_column(w, table)
                    if m:
                        col = m[0]; break
            if col is not None:
                cl = table.column_letters[col]
                formula = f"=COUNTIF({_R(cl,n)}, {crit_val or '\">0\"'})"
                rationale.append(f"count over column {cl} ({table.headers[col]})")

        # ---- conditional AVERAGE ----
        elif fid == "averageif":
            crit_col = crit_col_idx
            avg_col = _first_numeric_col(table)
            for w in words:
                m = _match_column(w, table)
                if m and table.types[m[0]] == "number":
                    avg_col = m[0]
                elif m and crit_col is None and table.types[m[0]] != "number":
                    crit_col = m[0]
            if crit_col is not None and avg_col is not None:
                cl, al = table.column_letters[crit_col], table.column_letters[avg_col]
                formula = f"=AVERAGEIF({_R(cl,n)}, {crit_val or '\"?\"'}, {_R(al,n)})"
                rationale.append(f"criteria = {cl}, average = {al}")

        # ---- lookup ----
        elif fid in ("vlookup", "xlookup", "index"):
            # key column = a text/name column, return column = a numeric/value column
            key_col = ret_col = None
            for w in words:
                m = _match_column(w, table)
                if not m:
                    continue
                if table.types[m[0]] == "number" and ret_col is None:
                    ret_col = m[0]
                elif key_col is None:
                    key_col = m[0]
            if key_col is None:
                key_col = 0
            if ret_col is None:
                ret_col = _first_numeric_col(table) or (1 if len(table.headers) > 1 else 0)
            kl, rl = table.column_letters[key_col], table.column_letters[ret_col]
            formula = (f'=XLOOKUP(G2, {_R(kl,n)}, {_R(rl,n)}, "Not found")')
            rationale.append(f"look up in column {kl} ({table.headers[key_col]}), "
                             f"return column {rl} ({table.headers[ret_col]}); "
                             f"put the value you search for in G2")

        # ---- plain SUM / AVERAGE / COUNT over a matched numeric column ----
        elif fid in ("sum", "average", "count"):
            col = _first_numeric_col(table)
            for w in words:
                m = _match_column(w, table)
                if m and table.types[m[0]] == "number":
                    col = m[0]; break
            if col is not None:
                cl = table.column_letters[col]
                fn = {"sum": "SUM", "average": "AVERAGE", "count": "COUNT"}[fid]
                formula = f"={fn}({_R(cl,n)})"
                rationale.append(f"{fn} over column {cl} ({table.headers[col]})")

        status = "filled" if formula else "template"
        if not formula and best:
            formula = best["example"]["formula"]
            rationale.append("no confident column mapping — showing pattern to adapt")

        resolved = next((f for f in self.engine.functions if f["id"] == fid), best)

        return {
            "language": language,
            "platform": platform,
            "locale": locale,
            "status": status,
            "formula": apply_locale(formula, locale) if formula else None,
            "best_function": resolved["name"] if resolved else None,
            "columns": [
                {"letter": l, "header": h, "type": t}
                for l, h, t in zip(table.column_letters, table.headers, table.types)
            ],
            "rationale": "; ".join(rationale),
            "safety": {
                "rows": table.report.rows,
                "cols": table.report.cols,
                "delimiter": table.report.delimiter,
                "injection_cells_defanged": table.report.injection_cells,
                "warnings": table.report.warnings,
            },
        }
