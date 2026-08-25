"""
test_retrieval.py — does the knowledge base still surface the right function?

The golden tests cover formulas the engine builds. This file covers the step
before that: given a plain-language request, does the correct function rank
first among the candidates?

This is where a growing catalogue breaks. Adding NETWORKDAYS with the keyword
"zile" can quietly outrank DAYS for a request about plain days, and nothing
raises an error — the user simply gets a worse answer. These cases turn that
into a failing test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.language import detect_platform
from app.engine.retrieval import KnowledgeBase

CASES = json.loads((Path(__file__).parent / "retrieval.json").read_text(encoding="utf-8"))["cases"]

kb = KnowledgeBase()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["description"][:45])
def test_expected_function_ranks_first(case: dict) -> None:
    description = case["description"]
    hits = kb.search(description, top_n=3, platform=detect_platform(description))
    names = [function["name"] for _, function in hits]

    assert names, f"no function matched at all: {description!r}"
    assert names[0] == case["expect"], (
        f"\n  request:  {description}"
        f"\n  expected: {case['expect']} first"
        f"\n  got:      {names}"
    )

    for also in case.get("also_expected", []):
        assert also in names, f"{also} missing from the top three for {description!r}: {names}"


def test_every_function_is_reachable_by_its_own_name() -> None:
    """Typing a function's name must find that function.

    A catalogue entry nothing can retrieve is dead weight, and this catches the
    case where a new entry's keywords are so generic that a different function
    outranks it even on its own name.
    """
    unreachable = []
    for function in kb.functions:
        hits = kb.search(function["name"], top_n=3)
        if function["name"] not in [f["name"] for _, f in hits]:
            unreachable.append(function["name"])

    assert not unreachable, f"functions not findable by name: {unreachable}"


def test_no_duplicate_ids_or_names() -> None:
    ids = [f["id"] for f in kb.functions]
    names = [f["name"] for f in kb.functions]
    assert len(ids) == len(set(ids)), "duplicate function ids in the knowledge base"
    assert len(names) == len(set(names)), "duplicate function names in the knowledge base"


def test_every_entry_is_complete() -> None:
    """Each entry needs the fields the engine and the UI both rely on."""
    for function in kb.functions:
        for field in ("id", "name", "category", "platforms", "syntax", "keywords"):
            assert function.get(field), f"{function.get('id')} is missing {field}"

        assert function["platforms"], f"{function['id']} lists no platform"
        assert set(function["platforms"]) <= {"excel", "sheets"}, function["id"]

        example = function.get("example", {})
        assert example.get("formula", "").startswith("="), f"{function['id']} has no usable example"
        assert example.get("explains_en"), f"{function['id']} has no English explanation"
        assert example.get("explains_ro"), f"{function['id']} has no Romanian explanation"


def test_every_entry_is_usable_in_romanian() -> None:
    """Romanian users must be able to find a function without knowing English.

    Detecting the language of a keyword automatically is not reliable — many
    Romanian search terms are spelled without diacritics on purpose, and some
    words are identical in both languages. So this checks the two things that
    can be checked honestly: a Romanian description exists, and the entry has
    enough keywords to have covered both languages. The retrieval cases above
    are what actually prove Romanian requests find the right function.
    """
    MINIMUM_KEYWORDS = 8

    thin = [
        f["id"] for f in kb.functions
        if len(f.get("keywords", [])) < MINIMUM_KEYWORDS
        or not f.get("description_ro", "").strip()
    ]
    assert not thin, (
        f"entries with fewer than {MINIMUM_KEYWORDS} keywords or no Romanian "
        f"description: {thin}"
    )


def test_related_functions_all_exist() -> None:
    ids = {f["id"] for f in kb.functions}
    dangling = {
        f["id"]: [r for r in f.get("related", []) if r not in ids]
        for f in kb.functions
        if any(r not in ids for r in f.get("related", []))
    }
    assert not dangling, f"related entries pointing at missing functions: {dangling}"
