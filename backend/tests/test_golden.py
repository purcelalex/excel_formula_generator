"""
test_golden.py — description in, exact formula out.

The single most valuable test file in this project. A rules engine degrades
quietly: adding one keyword to the knowledge base can steal a match from an
existing entry, and no exception is raised when it does. These cases are what
turns that silent regression into a red test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.pipeline import FormulaService
from app.ingestion.paste import parse_paste

GOLDEN = json.loads((Path(__file__).parent / "golden.json").read_text(encoding="utf-8"))
CASES = GOLDEN["cases"]
TABLES = GOLDEN["tables"]

service = FormulaService()


def _table_for(case: dict):
    name = case.get("table")
    return parse_paste(TABLES[name]) if name else None


def _case_id(case: dict) -> str:
    return case["name"]


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_golden_case(case: dict) -> None:
    result = service.generate(case["description"], _table_for(case))

    if case.get("expect") == "no_answer":
        assert result.status in ("template", "no_match"), (
            f"engine claimed a confident answer for nonsense input: {result.formula}"
        )
        return

    assert result.status == "filled", (
        f"expected a built formula, got status={result.status} "
        f"formula={result.formula!r} rationale={result.rationale!r}"
    )
    assert result.formula == case["formula"], (
        f"\n  description: {case['description']}"
        f"\n  expected:    {case['formula']}"
        f"\n  actual:      {result.formula}"
        f"\n  rationale:   {result.rationale}"
    )
    if "intent" in case:
        assert result.intent == case["intent"]


def test_every_filled_result_explains_itself() -> None:
    """A formula with no rationale is unreviewable by the user."""
    for case in CASES:
        if case.get("expect") == "no_answer":
            continue
        result = service.generate(case["description"], _table_for(case))
        assert result.rationale.strip(), f"no rationale for: {case['description']}"


def test_romanian_and_english_agree_on_structure() -> None:
    """The same request in either language must produce the same formula shape.

    Only the argument separator may differ. If a translation changes which
    function is chosen, one of the two keyword lists is wrong.
    """
    table = parse_paste(TABLES["sales"])
    english = service.generate("sum price where city is Chisinau", table)
    romanian = service.generate("aduna pretul unde orasul este Chisinau", table)

    assert english.language == "en" and romanian.language == "ro"
    assert english.intent == romanian.intent
    assert english.formula.replace(",", ";") == romanian.formula
