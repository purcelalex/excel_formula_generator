# CLAUDE.md — Project context for the Formula Engine

This file is standing context for Claude Code. It summarizes decisions made while
building the AI part of this project in a separate planning chat, so work can
continue in VS Code without losing the reasoning behind the code.

## What this project is

A web app that generates Excel and Google Sheets formulas from a plain-text
description (Romanian OR English), optionally using a copy-pasted sample of the
user's table (headers + a few rows). Frontend + backend. To be embedded later in
a landing page hosted on Cloudflare. Design will be provided later.

## Key architectural decision: NO external LLM in the core

The formula engine is fully LOCAL — a searchable knowledge base of functions plus
retrieval and slot-filling. This was a deliberate choice to avoid per-request LLM
API cost and bill-abuse risk. It is a *recommender + template-filler*, not a
general generator: it reliably picks the right function and fills common patterns
(conditional sum/count/average, lookup, plain aggregates) with real column
references, but cannot invent arbitrary novel compositions. Upgrade paths for true
generation (multilingual embeddings, self-hosted open model, or a rate-limited API
for the hard ~10%) are documented in README.md and are additive — the retrieval
result is the grounding context such a model would use.

## Files

- `knowledge_base.json` — function catalog (seed of 30, bilingual keywords). Extend this.
- `engine.py` — BM25 retrieval, language/platform/locale detection, intent boosts, slot-filling. Pure stdlib.
- `table_parser.py` — parses pasted TSV/CSV/HTML tables; sanitizes input; infers column types.
- `formula_builder.py` — table-aware builder; maps user words to real columns, fills real references.
- `api.py` — FastAPI backend: /generate, /generate_with_table, /admin/analytics; input caps; per-IP rate limit; SQLite logging.
- `demo.py`, `demo_table.py` — runnable examples (no install needed).
- `requirements.txt` — only needed for the FastAPI backend.

## Security posture (important — get this right)

Users PASTE tables as plain text, not files. Text can't carry a virus (nothing
executes), so antivirus scanning is NOT the model here. The real threat is
CSV/formula (DDE) injection: cells starting with = + - @. The parser detects and
DEFANGS these (prefixes with ') and reports them, plus hard caps on
chars/rows/cols/cell-length. If the app ever accepts UPLOADED .xlsx files instead,
that IS a real malware surface (VBA macros) needing actual scanning — a separate
feature, not built yet.

Backend security that matters for a public free-per-request tool: per-IP rate
limit (move to Redis if multi-instance), daily spend cap (if an LLM is ever added),
input size caps, admin analytics behind REAL auth (currently a placeholder bearer
token — must replace before launch). GDPR: store the description + outcome for
analytics, NOT raw screenshots.

## Romanian locale notes

- Google Sheets always uses ENGLISH function names; only the argument separator localizes.
- Modern Excel accepts English function names too.
- The big locale issue is the argument separator: EU/RO uses ';' not ','. The
  engine handles this via apply_locale(), which never touches commas inside strings.
- `name_ro` in the knowledge base is filled only where verified; complete the rest
  from Microsoft's official localized function list before relying on display names.

## Known limitations (stated honestly, by design)

- Column-role matching is heuristic; can misassign on ambiguous headers. The
  builder returns a `columns` map and a `rationale` so the user can verify/correct.
- Unfillable criterion slots become "?" on purpose — a visible "fill this in"
  marker rather than a silent wrong guess.

## Sensible next steps (not yet done)

- Add a `.gitignore` (ignore __pycache__, *.pyc, analytics.db, .venv).
- Expand `knowledge_base.json` toward the full Excel + Sheets function list.
- Build a small frontend: description box + paste box; show formula, detected
  columns, and the safety report.
- Replace the placeholder admin auth before any public launch.
- Decide the deployment shape (Variant A: one FastAPI service on a managed host,
  fronted by Cloudflare) — see README.

## How to run

- `python3 demo.py` and `python3 demo_table.py` — no install needed (pure stdlib core).
- Backend: `pip install fastapi uvicorn` then `uvicorn api:app --reload`.
