"""
intents.py — decide WHAT the user wants, from data-driven rules.

Retrieval answers "which function is this about". Intents answer "which shape of
formula does this request need" — and the difference matters, because "sum
column B" and "sum column B where A is Chisinau" retrieve the same words but
need different formulas.

The rules live in data/intents.json so that extending coverage is an edit to a
data file, reviewable in a diff, rather than a change to Python control flow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .language import normalize

DEFAULT_INTENTS_PATH = Path(__file__).resolve().parents[2] / "data" / "intents.json"


@dataclass(frozen=True)
class Intent:
    id: str
    function: str          # knowledge-base function id, for explanations
    builder: str           # which builder in builder.py handles this shape
    aggregate: str         # the ENGLISH function name to emit
    condition: str         # "requires" | "forbids" | "ignore"
    priority: int
    triggers: tuple[str, ...]


class IntentMatcher:
    def __init__(self, path: Path | str = DEFAULT_INTENTS_PATH):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.condition_words = {normalize(w) for w in data["condition_words"]}
        self.intents = [
            Intent(
                id=entry["id"],
                function=entry["function"],
                builder=entry["builder"],
                aggregate=entry["aggregate"],
                condition=entry.get("condition", "ignore"),
                priority=entry.get("priority", 0),
                triggers=tuple(normalize(t) for t in entry["triggers"]),
            )
            for entry in data["intents"]
        ]

    def has_condition(self, description: str, criterion_found_in_table: bool) -> bool:
        """Is this a filtered request?

        Evidence from the table outranks phrasing: if a value the user typed
        appears in one of their own columns, they are filtering by it whether or
        not they used a word like 'where'.
        """
        if criterion_found_in_table:
            return True
        tokens = set(normalize(description).split())
        phrase = normalize(description)
        for word in self.condition_words:
            if " " in word:
                if word in phrase:
                    return True
            elif word in tokens:
                return True
        return False

    def match(self, description: str, criterion_found_in_table: bool = False) -> Intent | None:
        """Return the best-matching intent, or None if nothing fires."""
        tokens = set(normalize(description).split())
        phrase = normalize(description)
        conditional = self.has_condition(description, criterion_found_in_table)

        candidates: list[Intent] = []
        for intent in self.intents:
            if intent.condition == "requires" and not conditional:
                continue
            if intent.condition == "forbids" and conditional:
                continue

            for trigger in intent.triggers:
                hit = trigger in phrase if " " in trigger else trigger in tokens
                if hit:
                    candidates.append(intent)
                    break

        if not candidates:
            return None
        return max(candidates, key=lambda i: i.priority)
