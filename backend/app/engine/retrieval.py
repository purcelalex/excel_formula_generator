"""
retrieval.py — find the right function in the knowledge base.

BM25 over each function's bilingual keyword and description text. Pure standard
library, no model, no network, no per-request cost.

A note on the architecture document, which proposed SQLite FTS5 here: measured
against the actual data, in-memory BM25 is the better choice and this module
keeps it. Even a complete catalogue of Excel and Sheets functions is roughly a
thousand short documents — scoring all of them is well under a millisecond, and
the index rebuilds from JSON at startup. FTS5 would add a build step and a
second source of truth to save time that was never being spent. If the
knowledge base ever grows documentation long enough to change that, the
`search()` signature is the seam to swap behind.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

from .language import normalize, tokenize

DEFAULT_KB_PATH = Path(__file__).resolve().parents[2] / "data" / "knowledge_base.json"


class BM25:
    """Standard BM25 ranking over pre-tokenised documents."""

    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.doc_count = len(documents)
        self.doc_lengths = [len(d) for d in documents]
        self.avg_length = (sum(self.doc_lengths) / self.doc_count) if self.doc_count else 0.0
        self.term_frequencies = [Counter(d) for d in documents]

        document_frequency: Counter = Counter()
        for document in documents:
            for term in set(document):
                document_frequency[term] += 1

        self.inverse_document_frequency = {
            term: math.log(1 + (self.doc_count - count + 0.5) / (count + 0.5))
            for term, count in document_frequency.items()
        }

    def score(self, query_terms: list[str], index: int) -> float:
        total = 0.0
        frequencies = self.term_frequencies[index]
        length = self.doc_lengths[index]
        for term in query_terms:
            frequency = frequencies.get(term)
            if not frequency:
                continue
            idf = self.inverse_document_frequency.get(term, 0.0)
            numerator = frequency * (self.k1 + 1)
            denominator = frequency + self.k1 * (
                1 - self.b + self.b * length / (self.avg_length or 1)
            )
            total += idf * numerator / denominator
        return total


class KnowledgeBase:
    """The function catalogue plus its search index."""

    KEYWORD_WEIGHT = 3          # keywords repeated N times when indexing
    PHRASE_BONUS = 2.5          # a full keyword phrase appearing in the query
    NAME_BONUS = 8.0            # the query IS a function name (see below)
    NAME_QUERY_MAX_TOKENS = 3   # how short a query must be to count as a name

    def __init__(self, path: Path | str = DEFAULT_KB_PATH):
        self.path = Path(path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.functions: list[dict] = data["functions"]
        self.by_id = {f["id"]: f for f in self.functions}

        documents: list[list[str]] = []
        self._keyword_phrases: list[set[str]] = []
        for function in self.functions:
            keywords = function.get("keywords", [])
            parts = [
                function["name"],
                function.get("name_ro") or "",
                function.get("category", ""),
                function.get("description_en", ""),
                function.get("description_ro", ""),
            ]
            # Keywords are the highest-signal field, so they count more than
            # prose that happens to mention a word once.
            parts.extend(keywords * self.KEYWORD_WEIGHT)
            documents.append(tokenize(" ".join(parts)))
            self._keyword_phrases.append({normalize(k) for k in keywords if k})

        self.index = BM25(documents)

    def __len__(self) -> int:
        return len(self.functions)

    def get(self, function_id: str) -> dict | None:
        return self.by_id.get(function_id)

    def search(self, text: str, top_n: int = 3, platform: str | None = None) -> list[tuple[float, dict]]:
        """Rank functions for a query. Returns (score, function) pairs."""
        query_terms = tokenize(text)
        query_normalized = normalize(text)

        results: list[tuple[float, dict]] = []
        for i, function in enumerate(self.functions):
            if platform and platform != "any" and platform not in function["platforms"]:
                continue

            score = self.index.score(query_terms, i)
            for phrase in self._keyword_phrases[i]:
                if phrase and phrase in query_normalized:
                    score += self.PHRASE_BONUS

            # Someone typing "TEXT" or "VALUE" wants that function. Without this
            # they would not get it: those words appear in dozens of other
            # entries' descriptions, so BM25 treats them as near-noise and the
            # function loses to its own vocabulary. The bonus applies only to
            # short queries, so a sentence like "count empty cells" is still
            # ranked on meaning rather than on containing the word "count".
            if len(query_terms) <= self.NAME_QUERY_MAX_TOKENS:
                if normalize(function["name"]) in query_terms:
                    score += self.NAME_BONUS

            if score > 0:
                results.append((score, function))

        results.sort(key=lambda pair: pair[0], reverse=True)
        return results[:top_n]
