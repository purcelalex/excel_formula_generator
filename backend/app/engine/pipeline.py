"""
pipeline.py — the whole engine, in the order it runs.

    description (+ optional table)
        -> detect language / platform / locale
        -> resolve the criterion (needs the table, if there is one)
        -> match an intent
        -> build a formula tree
        -> render for the target dialect
        -> package with candidates and a rationale

One entry point, `FormulaService.generate`, used by every route. The table is
optional: without one the engine still answers, using generic ranges and a
visible "?" wherever it cannot know the user's columns.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ingestion.models import ParsedTable, SafetyReport
from . import builder as builders
from .ast import Raw
from .intents import IntentMatcher
from .language import detect_language, detect_locale, detect_platform
from .render import dialect_for, render_formula
from .resolver import explicit_columns, resolve_criterion
from .retrieval import KnowledgeBase


@dataclass
class GenerationResult:
    language: str
    platform: str
    locale: str
    status: str                 # "filled" | "template" | "no_match"
    formula: str | None
    function: str | None
    intent: str | None
    rationale: str
    candidates: list[dict]
    columns: list[dict]
    safety: dict | None

    def as_dict(self) -> dict:
        return {
            "language": self.language,
            "platform": self.platform,
            "locale": self.locale,
            "status": self.status,
            "formula": self.formula,
            "function": self.function,
            "intent": self.intent,
            "rationale": self.rationale,
            "candidates": self.candidates,
            "columns": self.columns,
            "safety": self.safety,
        }


# How far down a generic formula reaches when the user gave no table. Chosen to
# match the convention in the knowledge base's own examples (B2:B100).
GENERIC_LAST_ROW = 100


def virtual_table(letters: list[str]) -> ParsedTable | None:
    """Build a stand-in table from column letters the user named directly.

    'sum column B where column A is a city' contains no table, but it does name
    its columns, which is enough to build a real formula. Without this, such a
    request would fall back to a generic example — a worse answer than the user
    has already given us the information for.
    """
    if not letters:
        return None

    span = max(ord(letter) - 64 for letter in letters if len(letter) == 1)
    if span < 1:
        return None

    column_letters = [chr(64 + i) for i in range(1, span + 1)]
    report = SafetyReport(source="described")
    report.cols = len(column_letters)
    report.rows_used = GENERIC_LAST_ROW - 1
    return ParsedTable(
        headers=[f"column {letter}" for letter in column_letters],
        rows=[],
        column_letters=column_letters,
        # Unknown types: the explicit letters decide the mapping, so no type
        # inference is needed or possible.
        types=["unknown"] * len(column_letters),
        report=report,
    )


class FormulaService:
    def __init__(self, knowledge_base: KnowledgeBase | None = None,
                 intent_matcher: IntentMatcher | None = None):
        self.kb = knowledge_base or KnowledgeBase()
        self.intents = intent_matcher or IntentMatcher()

    def generate(self, description: str, table: ParsedTable | None = None) -> GenerationResult:
        language = detect_language(description)
        platform = detect_platform(description)
        locale = detect_locale(description, language)
        dialect = dialect_for(platform, locale)

        criterion = resolve_criterion(description, table)
        # Either kind of criterion means the request is filtered: a value found
        # in the user's own data, or an explicit comparison like "over 120".
        filtered = bool(criterion and (criterion.column_index is not None or criterion.operator))
        intent = self.intents.match(description, filtered)

        hits = self.kb.search(description, top_n=3, platform=platform)
        candidates = [self._candidate(score, function, dialect, language) for score, function in hits]

        # With no pasted or uploaded table, fall back to any columns the user
        # named in the sentence itself.
        working_table = table
        described = False
        if working_table is None or not working_table.headers:
            working_table = virtual_table(explicit_columns(description))
            described = working_table is not None

        node = None
        rationale = ""
        if intent is not None and working_table is not None and working_table.headers:
            built = builders.build(intent, description, working_table, criterion)
            if built is not None:
                node, rationale = built
                if described:
                    rationale += " (from the columns named in your request)"

        if node is not None:
            return GenerationResult(
                language=language,
                platform=platform,
                locale=locale,
                status="filled",
                formula=render_formula(node, dialect),
                function=self._display_name(intent.function) if intent else None,
                intent=intent.id if intent else None,
                rationale=rationale,
                candidates=candidates,
                columns=working_table.columns_as_dicts(),
                safety=table.report.as_dict() if table else None,
            )

        # Fallback: show the best-matching function's example as a pattern.
        if hits:
            best = hits[0][1]
            return GenerationResult(
                language=language,
                platform=platform,
                locale=locale,
                status="template",
                formula=render_formula(Raw(best["example"]["formula"]), dialect),
                function=best["name"],
                intent=intent.id if intent else None,
                rationale=(
                    "no confident column mapping — this is the pattern to adapt"
                    if table and table.headers
                    else "no table provided — this is the pattern to adapt"
                ),
                candidates=candidates,
                columns=table.columns_as_dicts() if table else [],
                safety=table.report.as_dict() if table else None,
            )

        return GenerationResult(
            language=language,
            platform=platform,
            locale=locale,
            status="no_match",
            formula=None,
            function=None,
            intent=None,
            rationale=(
                "Nu a fost găsită nicio funcție potrivită. Reformulați cererea."
                if language == "ro"
                else "No matching function found. Try rephrasing the request."
            ),
            candidates=[],
            columns=table.columns_as_dicts() if table else [],
            safety=table.report.as_dict() if table else None,
        )

    # ---- helpers ----------------------------------------------------------

    def _display_name(self, function_id: str) -> str | None:
        function = self.kb.get(function_id)
        return function["name"] if function else None

    def _candidate(self, score: float, function: dict, dialect, language: str) -> dict:
        example = function.get("example", {})
        return {
            "name": function["name"],
            "name_ro": function.get("name_ro"),
            "platforms": function["platforms"],
            "syntax": function.get("syntax", ""),
            "example": render_formula(Raw(example.get("formula", "")), dialect)
            if example.get("formula")
            else None,
            "explanation": example.get(
                "explains_ro" if language == "ro" else "explains_en", ""
            ),
            "score": round(score, 2),
        }
