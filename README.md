# Excel Formula Generator

Generates Excel and Google Sheets formulas from a plain-language description in
**Romanian or English**, optionally using a sample of the user's own table.

**No external AI service is involved.** The engine is a local Python program: a
knowledge base of spreadsheet functions, a retrieval step, a set of intent
rules, and a formula compiler. There is no API key, no per-request cost, and no
user data leaving the server.

```
"aduna pretul unde orasul este Chisinau"  +  a pasted table
                        │
                        ▼
        =SUMIF(A2:A4; "Chisinau"; C2:C4)
        filtering on column A (City), sum over column C (Price)
```

## Quick start

```bash
cp .env.example .env
docker compose up --build
```

Open <http://localhost:8000/docs> for an interactive page where every endpoint
can be tried from the browser. See [DOCKER.md](DOCKER.md) for day-to-day use.

Running the tests:

```bash
docker compose exec api python -m pytest -q
```

## How it works

Five stages, each in its own module, each replaceable on its own.

| Stage | Module | What it does |
|---|---|---|
| Ingestion | `app/ingestion/` | Pasted text or an uploaded file becomes one `ParsedTable` |
| Language | `app/engine/language.py` | Romanian or English, Excel or Sheets, comma or semicolon |
| Retrieval | `app/engine/retrieval.py` | BM25 over the bilingual knowledge base |
| Intent | `app/engine/intents.py` | Which *shape* of formula is being asked for |
| Build & render | `app/engine/builder.py`, `ast.py`, `render.py` | Columns to a formula tree, tree to text |

### The formula is a tree, not a string

`ast.py` defines nodes — `Func`, `Range`, `Text`, `Criterion` — and `render.py`
is the only module in the codebase that produces formula text. Two consequences
follow, and they are the reason for the design:

**Composition is possible.** Wrapping a result in `IFERROR` or nesting one
function inside another is an operation on a tree. In string form it is text
surgery that breaks on the first quoted comma.

**Localisation is correct by construction.** The argument separator (`,` versus
`;`) and string escaping live in one function. There is no path through the code
that can emit a formula the renderer has not localised.

### Two entry points, one table

Paste and file upload both produce a `ParsedTable` and nothing else. The engine
cannot tell which route a table came from, so a fix or a limit applied to one
applies to both. Adding a third input later is one more adapter, not a second
pipeline.

## Security

Full reasoning in [ARCHITECTURE.md](ARCHITECTURE.md). In short:

- **Formula/CSV injection** — cells beginning with `=`, `+`, `-` or `@` are
  detected, defanged with a leading apostrophe, and reported back to the user.
- **Uploads** — only `.xlsx` and `.csv`; the bytes must match the extension;
  macro-bearing formats are refused by name *and* by inspecting the archive;
  zip bombs are caught before extraction; `defusedxml` is a hard dependency;
  files are parsed in memory and never written to disk.
- **Honest limit** — none of the above is antivirus. Python cannot detect
  malware. What it does is remove the conditions under which a malicious file
  could act. ClamAV as a sidecar would add signature scanning; it is a
  reasonable later addition, not a substitute for any check above.
- **Admin** — PBKDF2 password hash, constant-time comparison, HMAC-signed
  expiring session tokens. The admin routes refuse to serve until configured.
- **Rate limiting** — per client IP, in process. `X-Forwarded-For` is ignored
  because it is attacker-controlled unless a trusted proxy overwrites it.

## Analytics

Every request records the description, the detected language, the matched
function and whether a formula could be built. The **unmet needs** panel —
requests that produced no confident answer — is the point of the feature: it is
a ranked list of what the knowledge base is missing, written by real users in
their own words.

Table contents are never stored. Column count and types are, which is enough to
understand usage and contains no personal data.

## Extending it

**More functions:** add entries to `backend/data/knowledge_base.json` in the
existing shape. The highest-value field is `keywords` — every phrase a real user
might type, in both languages. No code change is needed.

Two rules learned the hard way. Keep keywords *specific*: a generic word like
"table" or "value" matches sentences that have nothing to do with the function.
And never add a keyword that is a near-copy of a test query — that fits the data
to the test and proves nothing.

`name_ro` is left empty on newer entries on purpose. Google Sheets uses English
function names in every locale and modern Excel accepts them, so a missing
localised name costs nothing while a wrong one is a real defect. Fill these from
Microsoft's official localised function list before displaying them as
authoritative.

**More formula patterns:** add an entry to `backend/data/intents.json` and, if
the shape is new, a builder function in `builder.py`.

**Always add a test.** Two files, for the two halves:

- `backend/tests/retrieval.json` — description to the function that must rank
  first. Add a case for every function you add.
- `backend/tests/golden.json` — description to the exact formula produced. Add a
  case for every intent or bug fix.

This is not ceremony. Expanding the catalogue from 30 to 80 functions broke two
existing matches immediately: a new Romanian keyword for PRODUCT hijacked
"cauta pretul dupa produs" (in Romanian, *produs* means both a multiplication
result and a product you sell), and a generic "lookup table" keyword on VLOOKUP
outranked SORT for "sort the table by price". Neither raised an error. Both
were caught by a red test and fixed in the data.

## Status

Working: bilingual parsing, paste and upload ingestion, a knowledge base of 94
functions, nine intents, formula building with real cell references,
localisation, analytics, admin auth, the frontend, and 116 passing tests.

**Two numbers, deliberately different.** The engine *recognises and explains*
all 94 functions. It *builds a finished formula from your columns* for the nine
intents in `data/intents.json` — conditional and plain sum, count and average,
plus lookup, maximum and minimum. Everything else is returned as a pattern to
adapt, labelled as such.

**Catalogue coverage** was checked against published "most used Excel
functions" lists rather than chosen by feel: every function named across
excel-easy's top ten, Sheetgo's top ten for 2026 and a 30-formula roundup is
present, Google-Sheets-only entries included.

Next: more intents, so that more of the catalogue can be built rather than
suggested — date differences and text extraction are the obvious candidates.
