# Formula Engine — local, no external LLM

The "AI part" of the formula-generator project, built in Python with **zero
per-request cost** and no dependency on Claude / ChatGPT APIs. It takes a plain
text description (Romanian or English) and returns an Excel / Google Sheets
formula, plus alternatives and an explanation.

## What this is (and isn't)

It is a **retrieval + slot-filling engine**: a searchable knowledge base of
functions, matched semantically to the user's words, with a template-filling
layer that produces ready-to-paste formulas for common patterns.

It is **not** a general formula generator. It cannot invent arbitrary
compositional formulas (e.g. `SUMIFS` nested inside `IFERROR` inside
`ARRAYFORMULA` with the user's exact columns) for requests it has no template
for. Those need a generative model. See "Upgrade paths" below — the retrieval
result is exactly the grounding context such a model would use, so nothing here
is throwaway.

## Files

| File | Purpose |
|------|---------|
| `knowledge_base.json` | The function catalog (seed of 30). Edit/extend this. |
| `engine.py` | Retrieval (BM25), language/platform/locale detection, slot-filling. No deps. |
| `demo.py` | Runs sample EN/RO queries. `python3 demo.py` |
| `api.py` | Optional FastAPI backend: input limits, rate limit, SQLite analytics logging, admin endpoint. |
| `requirements.txt` | Only needed for `api.py`. |

Try it: `python3 engine.py "adună coloana B unde coloana A este un oraș"`

## How it works

1. **Normalize** — lowercase, strip diacritics so `sumă`, `suma`, `suma` all match.
2. **Detect** — language (RO/EN), target platform (Excel/Sheets/any), locale.
3. **Retrieve** — BM25 over each function's bilingual keyword + description text,
   with a boost when a full keyword phrase appears, and an **intent layer** that
   recognizes co-occurrence (e.g. "sum" + "where/dacă" → `SUMIF`, not plain `SUM`).
4. **Slot-fill** — for known intents, extract columns/values and emit a filled
   formula. Unfillable slots become `"?"` on purpose — a visible "fill this in"
   marker rather than a silent wrong guess.
5. **Localize output** — swaps the argument separator `,` → `;` for RO/EU locale,
   never touching commas inside quoted strings.

## Known limitations (by design, stated honestly)

- **Column roles.** Word order alone can't always tell the *sum* column from the
  *criteria* column, so slot-filling may order them wrong or emit `"?"`. Fine for
  a "here's the pattern, adjust the ranges" UX; not for blind paste.
- **Novel compositions** fall back to a template of the top function.
- **Romanian function-name display.** `name_ro` is filled only where verified.
  Two things reduce how much this matters: Google Sheets always uses **English**
  function names (only the separator localizes), and modern Excel accepts English
  names too. Complete the `name_ro` fields from Microsoft's official localized
  function list before relying on display names.

## Extending to the full catalog

The seed has 30 functions. To reach "all Excel + all Sheets functions", append
entries to `knowledge_base.json` in the same shape. The highest-leverage field is
`keywords` — put every phrase a real user (RO **and** EN) might type. Sources to
bulk-import from: Microsoft's function list and Google's function list. Keep the
schema identical and the engine picks new entries up with no code change.

## Upgrade paths (when retrieval isn't enough)

- **Better matching, still local & free:** swap BM25 for multilingual
  embeddings (`sentence-transformers`). Same interface; RO/EN handled natively;
  needs a one-time model download. Wire it as an alternate `search()` backend.
- **True generation without per-request API cost:** self-host a small
  open-weights instruct model; feed it the top retrieved functions as context so
  it composes correct, locale-aware formulas. Fixed hosting cost, no per-call fee.
- **Hosted LLM for the hard 10% only:** send just the requests the local engine
  can't fill confidently to a text LLM, behind the rate limit + daily spend cap.
  Text-only calls are cents; this caps both cost and abuse.

## Pasted-table mode (description + copy-pasted cells)

`table_parser.py` + `formula_builder.py` add the flow where the user pastes their
columns and a few rows. With real headers, sample values, and inferred types, the
builder resolves which column is which and emits **real cell references** instead
of `"?"` placeholders. Endpoint: `POST /generate_with_table {description, table}`.

Example — pasting a 4-column table and asking (in Romanian) to sum price by city
returns `=SUMIF(A2:A4; "Chișinău"; C2:C4)`, having identified City as the criteria
column and Price as the numeric sum column, and localized the separator to `;`.

### Security: it's validation, not antivirus (and that's correct)

A copy-paste from Excel/Sheets arrives as **plain text** (usually tab-separated).
Text can't carry a virus — nothing executes when Python parses a string — so
antivirus scanning would be theater. The real, well-known threat is **CSV /
formula (DDE) injection**: cells beginning with `=`, `+`, `-`, or `@`. The parser:

- caps chars / rows / cols / cell length (stops denial-of-service by giant paste),
- normalizes encoding and strips control characters,
- **detects and defangs** injection cells (prefixes them with `'` so they're
  literal text everywhere) and reports them in `safety.injection_cells_defanged`,
- treats every cell strictly as data, never as an instruction.

If you later accept **uploaded `.xlsx` files** instead of paste, that IS a real
malware surface (VBA macros) and needs actual scanning (e.g. ClamAV) plus macro
stripping — a separate feature, flagged in the code. Paste mode avoids all of it.

### Honest limits of this mode

- Column-role resolution is heuristic (header fuzzy-match + type inference +
  matching the criterion value against sample cells). It's good on clear cases and
  can misassign on ambiguous headers — show the `rationale` and `columns` so the
  user can correct it.
- It fills the patterns it knows (conditional sum/count/average, lookup, plain
  aggregates). Genuinely novel compositions still need the generative upgrade path.

## Where it fits the backend

`api.py` is the integration seam for Variant A from the architecture discussion:
one FastAPI service, `/generate` for users, `/admin/analytics` behind auth for
you. It logs the **description + outcome** (not screenshots) to SQLite so the
analytics menu can show most-popular inputs and unmet needs. Before launch:
replace the bearer-token admin check with real auth, and move the in-memory rate
limiter to Redis if you run more than one instance.
