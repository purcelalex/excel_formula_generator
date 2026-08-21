---
title: Recommended Architecture — Excel Formula Generator
status: proposal for approval
date: 2026-08-21
---

# Recommended Architecture

## 1. What is already in the folder

`C:\Users\User\OneDrive\01_Freelancing\03_Portfolio\Excel_Formula_Generator`

| Path | State |
|---|---|
| `formula_engine/knowledge_base.json` | 30 functions, bilingual keywords, good schema |
| `formula_engine/engine.py` | BM25 retrieval + RO/EN detection + slot filling, pure stdlib |
| `formula_engine/table_parser.py` | Pasted-table parser, sanitization, injection defanging |
| `formula_engine/formula_builder.py` | Table-aware builder, real cell references |
| `formula_engine/api.py` | FastAPI: `/generate`, `/generate_with_table`, `/admin/analytics` |
| `formula_engine/demo.py`, `demo_table.py` | Runnable examples |
| `index.html` | **Empty (0 bytes)** — no frontend yet |
| `excel_generator_spa_style.png`, `purcelalex-logo.png` | Design reference + logo |
| `README.md`, `formula_engine/CLAUDE.md` | Documentation, current and accurate |

Missing: frontend, `.gitignore`, tests, a real knowledge base (30 of ~500 functions),
file-upload support, real admin auth.

**Two folder-level cautions.** The project lives in OneDrive: `analytics.db` (SQLite)
and `.venv/` must be excluded from sync — a synced open database file corrupts, and a
virtual environment syncs thousands of files. Add `.gitignore` and a OneDrive exclusion
before the first run.

## 2. The one change that matters most: two entry points, one table

Your new requirement adds **file upload** next to copy-paste. The temptation is to write
two pipelines. Don't. Define one internal representation — the existing `ParsedTable`
(headers, rows, column letters, inferred types, safety report) — and give it two
adapters:

```
paste (text/TSV/CSV/HTML) ─┐
                           ├─► ParsedTable ─► FormulaBuilder ─► Formula
file (.xlsx/.csv/.ods)  ───┘
```

Everything downstream — column-role resolution, formula building, localization,
analytics — stays identical and gets tested once. Adding a third input later (a
screenshot with OCR, say) is then one more adapter, not a fork of the app.

## 3. Recommended layer stack

```
┌─────────────────────────────────────────────────────────────┐
│ Cloudflare Pages — static frontend (HTML/CSS/vanilla JS)    │
│   • description textarea   • paste box   • file picker      │
│   • client-side pre-trim of the file (SheetJS)              │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTPS, JSON, CORS-locked to your domain
┌───────────────────────────▼─────────────────────────────────┐
│ Cloudflare proxy — WAF, bot fight, rate limiting at edge    │
└───────────────────────────┬─────────────────────────────────┘
┌───────────────────────────▼─────────────────────────────────┐
│ FastAPI (single service, Python)                            │
│  ① ingestion    paste adapter | upload adapter + validation │
│  ② engine       retrieval → intent → builder → renderer     │
│  ③ analytics    log description + outcome                   │
│  ④ admin        /admin/* behind real auth                   │
└──────────┬─────────────────────────────┬────────────────────┘
           │                             │
   ┌───────▼────────┐            ┌───────▼────────┐
   │ kb.sqlite      │            │ analytics.db   │
   │ read-only      │            │ read-write     │
   │ in git, FTS5   │            │ gitignored     │
   └────────────────┘            └────────────────┘
```

**One service, not microservices.** For a portfolio project with one developer, a
single FastAPI process is the correct answer: one deploy, one log stream, no network
hop between engine and API. The layers above are module boundaries inside that process,
which is where the design value actually is.

### Two databases, deliberately separate

You asked for "a database with all Excel and Google Sheets formulas and documentation."
That is a **read-only, versioned artifact** — it belongs in git, changes only when you
edit it, and should be diffable. Runtime analytics are the opposite: write-heavy,
disposable, must never be committed. Keeping them in one file mixes those lifecycles
and makes every analytics write dirty your repo.

- `data/knowledge_base.json` — the editable source of truth, in git.
- `kb.sqlite` — built from it by `scripts/build_kb.py`, using **FTS5** for full-text
  search over bilingual keywords and documentation. FTS5 is built into Python's
  `sqlite3`, needs no dependency, and replaces the hand-written BM25 loop with an
  indexed query that stays fast at 1,000+ functions where the current linear scan
  would not.
- `analytics.db` — runtime only, gitignored, OneDrive-excluded.

## 4. The engine: build an AST, not a string

This is my main technical recommendation, and it is what turns "find a formula" into
"**write** a formula" — which is what you asked for.

Today `formula_builder.py` produces formula strings directly, then patches the
separator afterwards. Instead, build a small structured object and render it:

```
intent + resolved columns  ─►  FormulaNode tree  ─►  renderer
                                                     ├─ Excel EN  (=SUMIF(A2:A5,"X",C2:C5))
                                                     ├─ Excel RO  (=SUMIF(A2:A5;"X";C2:C5))
                                                     └─ Sheets    (English names always)
```

Why it is worth it:

- **Composition becomes possible.** Wrapping a node in `IFERROR`, or nesting `SUMIFS`
  inside `ROUND`, is a tree operation. In string-land it is fragile text surgery. This
  is the difference between a lookup tool and a generator.
- **Localization stops being a string hack.** The renderer owns separators and function
  names; `apply_locale()`'s quote-scanning workaround disappears.
- **It is testable.** You assert on the tree, not on punctuation.
- **It is the portfolio differentiator.** "I wrote a formula compiler" is a much
  stronger interview story than "I wrote a keyword search."

Keep BM25/FTS retrieval as the **function finder**; the AST builder is the **formula
writer**. Retrieval answers "which function", the builder answers "with what arguments".

### Make intents data, not code

`_INTENT_BOOSTS` is currently a hardcoded Python list of 7 entries. Move intent rules
into the knowledge base as data (trigger words, required co-occurring words, target
function, required slots). Then extending coverage from 7 intents to 60 is editing JSON
— no code change, no redeploy of logic, and the rules become reviewable.

## 5. File upload: honest security design

You asked Python to "check that the file doesn't have any viruses." Here is the truthful
version, because getting this right is itself a portfolio asset.

**A real antivirus scan needs an antivirus engine.** Python cannot detect malware by
itself; a `pip` package claiming to would be false comfort. The realistic options are
ClamAV (`clamd`) running as a sidecar container, or a cloud scanning API. ClamAV is free
and works, but adds ~1 GB of RAM for signature databases — real cost on a small host.

**But almost all of the risk can be designed away instead.** The threats that actually
apply to a spreadsheet upload are:

| Threat | Defense |
|---|---|
| VBA macros (`.xlsm`, `.xlsb`, old `.xls`) | Reject these extensions outright. Accept only `.xlsx`, `.csv`, `.ods`. Also reject any `.xlsx` whose zip contains `vbaProject.bin`. |
| Zip bomb (a 1 MB `.xlsx` expanding to 10 GB) | `.xlsx` is a zip: inspect the member list and **uncompressed** sizes *before* extracting; reject on ratio or total. |
| XXE / XML entity attacks in the sheet XML | `openpyxl` uses `defusedxml` when installed — make it a hard dependency, not optional. |
| Formula/DDE injection in cell values | Already solved in `table_parser.py`. Reuse it — the upload adapter feeds the same sanitizer. |
| Denial of service by huge file | Cap at ~2 MB. Stream and abort past the cap; never read the whole body first. |
| Malware persisting on your server | **Never write the upload to disk.** Parse in memory, take headers + 4 rows, discard. Nothing to infect, nothing to store, nothing under GDPR. |

`openpyxl` in `read_only=True, data_only=True` mode does not execute anything — it reads
XML. Combined with the table above, the residual risk is small enough that ClamAV becomes
optional hardening rather than the load-bearing control. Document that reasoning in the
README; it reads as engineering judgment, which is what a reviewer is looking for.

### The 3–4 row rule — enforce it in the browser first

Your validation rule ("all columns, only 3–4 rows") has a much better implementation than
rejecting files server-side: **extract the sample in the browser** with SheetJS, and send
only headers + 4 rows as JSON. The file itself then never leaves the user's computer.
No upload, no malware surface, no privacy exposure, instant response.

I recommend building **both**, with the client-side path as the default:

1. **Default:** browser trims the file → posts JSON → backend treats it exactly like a
   paste. Fast, and the safest thing that can happen to a file is nothing.
2. **Fallback:** `POST /generate_with_file` accepts the real file with the full hardening
   above — for users with JS disabled, and because a hardened Python upload path is
   exactly the kind of code worth showing in a portfolio.

On row count, prefer **truncate with notice** over rejection: read the first 4 data rows
and tell the user "using the first 4 rows of 12,000." Rejecting a large file forces the
user to edit their spreadsheet before they can use your tool — a rule that protects you
should not become homework for them. The engine only needs headers, types, and a few
sample values; extra rows add nothing.

## 6. Analytics and admin

Keep logging description + detected language + matched function + status + whether the
formula was fillable. Add one field the current schema lacks: **`no_match` queries are
the most valuable data you will collect** — they are a ranked to-do list of missing
knowledge base entries. Surface them as their own panel in the admin menu.

Do not log the table contents. Headers alone are borderline (they can be
business-identifying); log column *types* and count instead. That keeps the analytics
useful and the GDPR story trivial: no personal data stored.

Replace the placeholder bearer token before anything is public. For a single-admin tool,
a password hash in an environment variable plus a signed session cookie is sufficient and
honest; `ADMIN_TOKEN=change-me-before-launch` in the repo is not.

## 7. Deployment

- **Frontend:** Cloudflare Pages, same account as your landing page. Free, fast, and it
  is where the design you provide later will drop in.
- **Backend:** a small always-on Python host — Fly.io, Render, or Railway. Cloudflare
  Workers cannot run this (no CPython, no openpyxl); Cloudflare in front of the API for
  WAF and edge rate limiting is still worth doing.
- **Cost:** €0–7/month. No per-request AI cost, by design — which remains the single
  best decision in this project.

## 8. Proposed final structure

```
Excel_Formula_Generator/
├── backend/
│   ├── app/
│   │   ├── main.py               FastAPI app, routes, CORS, middleware
│   │   ├── config.py             settings from environment
│   │   ├── ingestion/
│   │   │   ├── paste.py          text/TSV/CSV/HTML  → ParsedTable
│   │   │   ├── upload.py         .xlsx/.csv/.ods    → ParsedTable
│   │   │   ├── sanitize.py       shared cell cleaning + injection defanging
│   │   │   └── models.py         ParsedTable, SafetyReport
│   │   ├── engine/
│   │   │   ├── retrieval.py      SQLite FTS5 search over the KB
│   │   │   ├── language.py       RO/EN, platform, locale detection
│   │   │   ├── intents.py        data-driven intent matching
│   │   │   ├── resolver.py       user words → real columns
│   │   │   ├── ast.py            FormulaNode tree
│   │   │   └── render.py         tree → Excel EN / Excel RO / Sheets
│   │   ├── analytics/            logging + admin queries
│   │   └── security/             rate limit, auth, upload validation
│   ├── data/knowledge_base.json  source of truth, in git
│   ├── scripts/build_kb.py       JSON → kb.sqlite (FTS5)
│   ├── tests/
│   │   ├── test_golden.py        description → expected formula, RO + EN
│   │   └── test_security.py      injection, zip bomb, macro, oversize
│   └── requirements.txt
├── frontend/
│   ├── index.html
│   ├── css/  js/  assets/
│   └── js/sheet-trim.js          SheetJS client-side sampling
├── docs/
├── .gitignore
└── README.md
```

The existing `formula_engine/` files migrate into this layout — `engine.py` splits into
`retrieval.py` + `language.py`, `table_parser.py` into `paste.py` + `sanitize.py`,
`formula_builder.py` into `resolver.py` + `ast.py` + `render.py`. Nothing is thrown away.

## 9. Golden tests — the part that makes a rules engine credible

A rules-based engine without a regression suite decays the moment the knowledge base
grows: every new keyword can steal matches from an existing one. Build
`tests/golden.yaml` from the start — pairs of (description, expected function, expected
formula) in both languages — and run it on every change. A hundred passing bilingual
cases is also the most convincing thing you can show a reviewer, far more than a demo
that works once.

## 10. Suggested build order

1. `.gitignore`, OneDrive exclusions, `backend/` skeleton, migrate existing files.
2. Knowledge base build script + FTS5, expand toward the full function list.
3. AST + renderer, with golden tests written alongside.
4. Ingestion adapters — paste first (already working), then upload with full validation.
5. Frontend against the real API, styled to your PNG reference.
6. Admin auth + analytics panel, including the `no_match` list.
7. Deploy: Pages + API host, CORS locked, Cloudflare in front.

## Open decisions for you

1. **Row rule:** truncate-with-notice (recommended) or hard reject over 4 rows?
2. **Upload path:** build both client-side trim and server-side upload (recommended), or
   server-side only?
3. **ClamAV:** skip it and rely on the layered validation (recommended for a portfolio),
   or include it as a container sidecar to demonstrate the integration?
4. **Frontend:** vanilla JS (recommended — no build step, drops straight onto Cloudflare
   Pages) or React?
