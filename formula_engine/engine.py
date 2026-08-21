"""
Formula engine: local, no external LLM.

Pipeline:
  1. Normalize the user's text (lowercase, strip diacritics for robust RO/EN matching).
  2. Detect language, target platform (Excel / Sheets), and locale separator.
  3. Retrieve candidate functions with BM25 over bilingual keyword+description fields,
     plus an exact keyword-overlap boost.
  4. Try a lightweight slot-filling pass: if the request matches a known INTENT
     (conditional sum/count, simple lookup, ...) AND we can extract columns/values,
     emit a ready-to-paste formula. Otherwise return the top function as a template.
  5. Format output for the requested platform + locale (argument separator , vs ;).

This is a *recommender with template filling*, not a general generator. Novel
compositional formulas need a generative model (self-hosted or API) bolted on
top -- the retrieval result makes a good grounding context for that step.
"""

import json
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path

KB_PATH = Path(__file__).parent / "knowledge_base.json"


# ---------- text utilities ----------

def strip_diacritics(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(s: str) -> str:
    s = strip_diacritics(s.lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokenize(s: str) -> list[str]:
    return normalize(s).split()


# Romanian signal words (diacritics already stripped) for cheap language detection.
_RO_MARKERS = {
    "daca", "coloana", "aduna", "suma", "numara", "medie", "valori", "unde",
    "rand", "randuri", "cauta", "cautare", "gaseste", "celule", "mai", "mare",
    "decat", "intre", "primele", "ultimele", "text", "curenta", "distincte",
    "conditie", "conditii", "pret", "nume", "oras", "sau", "si", "pe", "din",
}


def detect_language(text: str) -> str:
    toks = set(tokenize(text))
    ro_hits = len(toks & _RO_MARKERS)
    # any original diacritics is a strong RO signal
    if any(ord(c) > 127 for c in text) or ro_hits >= 2:
        return "ro"
    return "en"


def detect_platform(text: str) -> str:
    t = normalize(text)
    if any(k in t for k in ("google", "sheets", "foi de calcul", "spreadsheet")):
        return "sheets"
    if "excel" in t:
        return "excel"
    return "any"


def detect_locale(text: str, language: str) -> str:
    """Which argument separator the OUTPUT should use.
    RO/EU locales generally use ';'. Default EN to ','."""
    t = normalize(text)
    if any(k in t for k in ("semicolon", "punct si virgula", "european", "romana", "romanian")):
        return "eu"
    return "eu" if language == "ro" else "us"


def apply_locale(formula: str, locale: str) -> str:
    """Swap argument separators for EU locale, without touching commas inside strings."""
    if locale != "eu":
        return formula
    out, in_str = [], False
    for ch in formula:
        if ch == '"':
            in_str = not in_str
            out.append(ch)
        elif ch == "," and not in_str:
            out.append(";")
        else:
            out.append(ch)
    return "".join(out)


# ---------- BM25 index ----------

class BM25:
    def __init__(self, docs: list[list[str]], k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.docs = docs
        self.N = len(docs)
        self.doclen = [len(d) for d in docs]
        self.avgdl = (sum(self.doclen) / self.N) if self.N else 0.0
        self.tf = [Counter(d) for d in docs]
        df = Counter()
        for d in docs:
            for term in set(d):
                df[term] += 1
        self.idf = {
            t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()
        }

    def score(self, query_terms: list[str], i: int) -> float:
        s, tf, dl = 0.0, self.tf[i], self.doclen[i]
        for t in query_terms:
            if t not in tf:
                continue
            idf = self.idf.get(t, 0.0)
            num = tf[t] * (self.k1 + 1)
            den = tf[t] + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
            s += idf * num / den
        return s


# ---------- engine ----------

class FormulaEngine:
    def __init__(self, kb_path=KB_PATH):
        data = json.loads(Path(kb_path).read_text(encoding="utf-8"))
        self.functions = data["functions"]
        self._docs, self._keyword_sets = [], []
        for f in self.functions:
            parts = [f["name"], f.get("name_ro") or "", f["category"],
                     f.get("description_en", ""), f.get("description_ro", "")]
            # keywords weighted x3 -- they are the highest-signal field
            kws = f.get("keywords", [])
            parts += kws * 3
            toks = tokenize(" ".join(parts))
            self._docs.append(toks)
            self._keyword_sets.append({normalize(k) for k in kws})
        self.bm25 = BM25(self._docs)

    # Intent boosts: when these signal words co-occur, nudge the compositional
    # function above the bare one (e.g. "sum" + "where" should beat plain SUM).
    _COND_WORDS = {"where", "if", "only", "unde", "daca", "conditie", "conditii", "cand"}
    _INTENT_BOOSTS = [
        ("sum",   _COND_WORDS, {"sumif": 12, "sumifs": 8}),
        ("aduna", _COND_WORDS, {"sumif": 12, "sumifs": 8}),
        ("suma",  _COND_WORDS, {"sumif": 12, "sumifs": 8}),
        ("count", _COND_WORDS, {"countif": 10, "countifs": 7}),
        ("numara", _COND_WORDS, {"countif": 10, "countifs": 7}),
        ("average", _COND_WORDS, {"averageif": 10}),
        ("medie", _COND_WORDS, {"averageif": 10}),
    ]

    def _intent_boost(self, fid: str, tokens: set) -> float:
        boost = 0.0
        for trigger, cond_words, targets in self._INTENT_BOOSTS:
            if trigger in tokens and tokens & cond_words and fid in targets:
                boost += targets[fid]
        return boost

    def search(self, text: str, top_n=3, platform_filter=None):
        q = tokenize(text)
        q_set = set(q)
        q_norm = normalize(text)
        results = []
        for i, f in enumerate(self.functions):
            if platform_filter and platform_filter != "any":
                if platform_filter not in f["platforms"]:
                    continue
            score = self.bm25.score(q, i)
            # boost when a full keyword phrase appears in the query
            for kw in self._keyword_sets[i]:
                if kw and kw in q_norm:
                    score += 2.5
            score += self._intent_boost(f["id"], q_set)
            if score > 0:
                results.append((score, f))
        results.sort(key=lambda x: x[0], reverse=True)
        return results[:top_n]

    # ---- intent / slot filling for common ready-to-paste cases ----

    _COL = r"(?:column|coloana|col\.?)\s*([a-z])"

    def _cols(self, text: str):
        return [m.upper() for m in re.findall(self._COL, normalize(text))]

    def _number(self, text: str):
        m = re.search(r"(>=|<=|>|<|=)?\s*(\d+(?:\.\d+)?)", text)
        if m:
            op = m.group(1) or ">"
            return f'"{op}{m.group(2)}"'
        return None

    def _quoted_value(self, text: str):
        m = re.search(r'"([^"]+)"', text) or re.search(r"'([^']+)'", text)
        return f'"{m.group(1)}"' if m else None

    def try_slot_fill(self, text: str, top_func: dict):
        """Return a filled formula string, or None if we can't confidently fill it."""
        t = normalize(text)
        cols = self._cols(text)
        fid = top_func["id"]

        # conditional sum: needs a "sum" column + a condition column
        if fid in ("sumif", "sumifs") and len(cols) >= 2:
            crit_col, sum_col = cols[0], cols[1]
            crit = self._quoted_value(text) or self._number(text) or '"?"'
            return f'=SUMIF({crit_col}2:{crit_col}1000, {crit}, {sum_col}2:{sum_col}1000)'

        # conditional count: one column + a condition
        if fid in ("countif", "countifs") and len(cols) >= 1:
            col = cols[0]
            crit = self._number(text) or self._quoted_value(text) or '">0"'
            return f'=COUNTIF({col}2:{col}1000, {crit})'

        # simple lookup: value found in one col, return from another
        if fid in ("vlookup", "xlookup") and len(cols) >= 2:
            key_col, ret_col = cols[0], cols[1]
            return (f'=XLOOKUP(D2, {key_col}2:{key_col}1000, '
                    f'{ret_col}2:{ret_col}1000, "Not found")')

        # average with condition
        if fid == "averageif" and len(cols) >= 2:
            crit_col, avg_col = cols[0], cols[1]
            crit = self._quoted_value(text) or self._number(text) or '"?"'
            return f'=AVERAGEIF({crit_col}2:{crit_col}1000, {crit}, {avg_col}2:{avg_col}1000)'

        return None

    # ---- top-level ----

    def generate(self, text: str, top_n=3):
        language = detect_language(text)
        platform = detect_platform(text)
        locale = detect_locale(text, language)
        hits = self.search(text, top_n=top_n, platform_filter=platform)

        if not hits:
            return {
                "language": language, "platform": platform, "locale": locale,
                "status": "no_match",
                "message_en": "No matching function found. Try rephrasing.",
                "message_ro": "Nu a fost găsită nicio funcție potrivită. Reformulați.",
                "candidates": [],
            }

        best_score, best = hits[0]
        filled = self.try_slot_fill(text, best)
        template = best["example"]["formula"]
        primary = apply_locale(filled or template, locale)

        candidates = []
        for score, f in hits:
            candidates.append({
                "name": f["name"],
                "name_ro": f.get("name_ro"),
                "platforms": f["platforms"],
                "syntax": f["syntax"],
                "example": apply_locale(f["example"]["formula"], locale),
                "explanation": f["example"]["explains_ro" if language == "ro" else "explains_en"],
                "score": round(score, 2),
            })

        return {
            "language": language,
            "platform": platform,
            "locale": locale,
            "status": "filled" if filled else "template",
            "formula": primary,
            "best_function": best["name"],
            "confidence": round(best_score, 2),
            "candidates": candidates,
        }


if __name__ == "__main__":
    import sys
    eng = FormulaEngine()
    query = " ".join(sys.argv[1:]) or "sum column B where column A equals a city"
    out = eng.generate(query)
    print(json.dumps(out, ensure_ascii=False, indent=2))
