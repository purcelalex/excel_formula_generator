"""
language.py — what language, which spreadsheet, which locale.

Three cheap detections that steer everything downstream. All of them are
deliberately lexical: they run in microseconds, need no model, and are easy to
correct by editing a word list.
"""

from __future__ import annotations

import re
import unicodedata


def strip_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """Lowercase, strip diacritics, drop punctuation.

    Stripping diacritics is what lets 'sumă', 'suma' and 'SUMA' match one entry
    without three keyword variants — Romanian users type all three depending on
    their keyboard layout.
    """
    text = strip_diacritics(text.lower())
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list[str]:
    return normalize(text).split()


# Romanian signal words, diacritics already stripped.
_RO_MARKERS = {
    "daca", "coloana", "coloane", "aduna", "suma", "numara", "medie", "valori",
    "unde", "rand", "randuri", "cauta", "cautare", "gaseste", "celule", "mai",
    "mare", "mic", "decat", "intre", "primele", "ultimele", "curenta",
    "distincte", "conditie", "conditii", "pret", "nume", "oras", "sau", "si",
    "pe", "din", "cate", "cati", "total", "cantitate", "data", "care",
}

# English words that also appear in Romanian text and must not tip detection.
_AMBIGUOUS = {"total", "data"}


def detect_language(text: str) -> str:
    """Return 'ro' or 'en'.

    Any Romanian diacritic is decisive. Otherwise two or more Romanian marker
    words are needed, so a single ambiguous word like 'total' — which is spelled
    the same in both languages — cannot flip an English request to Romanian.
    """
    if any(ch in "ăâîșțĂÂÎȘȚşţŞŢ" for ch in text):
        return "ro"
    tokens = set(tokenize(text))
    hits = (tokens & _RO_MARKERS) - _AMBIGUOUS
    return "ro" if len(hits) >= 2 else "en"


def detect_platform(text: str) -> str:
    """Return 'excel', 'sheets', or 'any'."""
    lowered = normalize(text)
    if any(word in lowered for word in ("google", "sheets", "spreadsheet", "foi de calcul")):
        return "sheets"
    if "excel" in lowered:
        return "excel"
    return "any"


def detect_locale(text: str, language: str) -> str:
    """Which argument separator the OUTPUT should use: 'eu' (;) or 'us' (,).

    Romanian Excel installations use the semicolon, so a Romanian request
    defaults to EU. An explicit mention of either style overrides the guess.
    """
    lowered = normalize(text)
    tokens = set(lowered.split())

    # Multi-word phrases are matched as substrings; single words are matched as
    # whole tokens, so 'us' cannot fire on 'customers' and 'comma' cannot fire
    # on 'command'.
    if "punct si virgula" in lowered or tokens & {"semicolon", "european", "romana", "romanian"}:
        return "eu"
    if tokens & {"comma", "virgula", "us", "american"}:
        return "us"
    return "eu" if language == "ro" else "us"
