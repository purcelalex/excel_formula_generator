# Deploying

Everything runs on Cloudflare: the page and the Python backend, one Worker, one
domain, deployed from GitHub.

```
  git push  ──►  GitHub Actions  ──►  Cloudflare Worker
                 tests must pass       ├── public/index.html   (static assets)
                                       ├── backend/app         (FastAPI, Python)
                                       └── D1                  (analytics)
```

**Why this works now.** Cloudflare Workers run Python via Pyodide, FastAPI is
supported, and pure-Python packages install from PyPI — which is all four of
this project's dependencies. Earlier the answer would have been "Cloudflare
cannot run the backend"; that is no longer true.

**What it means in practice.** The page and the API share one origin, so there
is no CORS to configure and no second host to pay for. `window.EFG_API_BASE`
is not needed and is not in the deployed page.

---

## One-time setup

### 1. Install the tooling

```powershell
npm install -g wrangler
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"   # uv, for pywrangler
wrangler login
```

### 2. The analytics database — already done

`excel-formula-generator` exists in your Cloudflare account (id
`700a1fda-5c25-421b-bd75-26731d3d1a94`, region EEUR), the `requests` table and
both indexes are created, and the id is already in `wrangler.toml`. It was
created through the Cloudflare API, and `d1_migrations` records `0001` as
applied so `wrangler` will not try to run it again.

Nothing to do here. To verify:

```powershell
wrangler d1 execute excel-formula-generator --remote --command "SELECT name FROM sqlite_master WHERE type='table'"
```

### 3. Set the admin secrets

Generate the pair — this prints both values:

```powershell
docker compose exec api python -m app.security.auth "choose a strong password"
```

Then store them as Cloudflare secrets. They are prompted for, so they never
appear in your shell history or in `wrangler.toml`:

```powershell
wrangler secret put ADMIN_PASSWORD_HASH
wrangler secret put ADMIN_SESSION_SECRET
```

Until both are set, the admin routes return 503 — the correct behaviour for an
unconfigured deployment, and the reason there is no default password.

### 4. Deploy

```powershell
uvx workers-py deploy
```

Check it:

```powershell
curl https://excel-formula-generator.<your-subdomain>.workers.dev/api/health
```

Expect `platform: "workers"`, `functions_loaded: 94`, and both
`admin_configured` and `analytics_configured` `true`. If either is `false`,
step 2 or 3 did not take.

---

## Putting it on formula.purcelalex.com

`wrangler.toml` already declares the route, so the deploy claims the hostname
itself. Two things have to exist for it to work:

**1. The DNS record.** Cloudflare dashboard → **purcelalex.com → DNS → Add
record**:

| Field | Value |
|---|---|
| Type | `CNAME` |
| Name | `formula` |
| Target | anything, e.g. `purcelalex.com` — the Worker route overrides it |
| Proxy status | **Proxied** (orange cloud) — required |

A custom-domain route replaces whatever the record points at, so the target is
a placeholder. The orange cloud is not optional: an unproxied record never
reaches Workers.

**2. The deploy.** `uvx workers-py deploy` binds the Worker to the hostname.

If the DNS record does not exist yet, comment out the `routes = [...]` block in
`wrangler.toml` for the first deploy. The Worker is still reachable at
`excel-formula-generator.<your-subdomain>.workers.dev` and you can test it
there, then uncomment and redeploy.

### Why a subdomain and not purcelalex.com/formula-generator

This Worker serves the page **and** `/api/*`. On a path it would have to claim
`purcelalex.com/api/*` too — a broad claim on the apex domain that collides the
day the main site wants its own API. On its own hostname there is one route,
the page sits at `/`, the API at `/api/`, they share an origin so there is no
CORS, and the main site is untouched.

If you later prefer the path anyway, it needs two routes
(`purcelalex.com/formula-generator*` and `purcelalex.com/api/*`) and the page
file moved to `public/formula-generator/index.html` so the asset path matches.

### The link from the main site — already done

`<li><a href="https://formula.purcelalex.com/">Formula Generator</a></li>` was
added to the header of all seven pages in the landing-page repo
(`02_Project_Landing_Page_Figma`): index, services, data-cleaner-demo, about,
contact, privacy, terms. One line per file, nothing else touched.

That repo had uncommitted work in progress when the edit was made, so it was
deliberately **not** committed. Review it, then commit and deploy the site as
you normally do.

The generator page's own header now mirrors the site's — same links, same
Contact button — so moving between them feels like one site rather than two.

## Automatic deployment from GitHub

`.github/workflows/deploy.yml` deploys on every push to `main`, and only after
the tests pass.

Add one repository secret — **Settings → Secrets and variables → Actions**:

| Name | Where it comes from |
|---|---|
| `CLOUDFLARE_API_TOKEN` | Cloudflare → My Profile → API Tokens → Create Token → **Edit Cloudflare Workers** template |

Optionally add a repository **variable** `CLOUDFLARE_SUBDOMAIN` (your
`*.workers.dev` prefix) and each deploy will verify itself by calling
`/api/health` afterwards.

The admin secrets are deliberately not part of this. They live in Cloudflare
and never pass through a CI log.

---

## Working on it locally

Two ways, and both are useful for different reasons.

**Docker** — the fastest loop, and what the tests run against:

```powershell
docker compose up
```

**pywrangler** — the real Workers runtime, with a local D1:

```powershell
wrangler d1 migrations apply excel-formula-generator --local
uvx workers-py dev
```

Use Docker while writing code. Use `pywrangler dev` before deploying, because
it is the only thing that exercises Pyodide. The test suite covers the Workers
code path with a stand-in `env` and a stand-in D1 — enough to prove the routing
and every SQL statement, not enough to prove Pyodide package behaviour.

---

## Costs

| | |
|---|---|
| Workers, free plan | 100,000 requests/day |
| D1, free plan | 5 GB storage, 5 million rows read/day |
| Static assets | free, and served without invoking the Worker |
| AI service | none — the engine is local, so no per-request cost |

The one limit worth watching on the free plan is **10 ms CPU per request**.
Retrieval over 94 functions is well inside it; parsing a large uploaded `.xlsx`
is the case that could exceed it. The paid plan ($5/month) raises the ceiling to
30 seconds. If uploads start failing while pasted tables keep working, that is
the limit you have met.

---

## Redeploying

| Change | What to do |
|---|---|
| Anything in `backend/` or `public/` | `git push` — Actions deploys it |
| Manually, without a push | `uvx workers-py deploy` |
| A secret | `wrangler secret put NAME` |
| The analytics schema | add a file to `worker/migrations/`, then `wrangler d1 migrations apply excel-formula-generator --remote` |

---

## Appendix: running it on a container host instead

The `Dockerfile` and `fly.toml` still work, and nothing about the application
prevents it. Use them if you want a filesystem, no CPU-time ceiling, or a
long-running process.

```powershell
fly volumes create efg_data --region ams --size 1
fly secrets set ADMIN_PASSWORD_HASH='...' ADMIN_SESSION_SECRET='...' ALLOWED_ORIGINS='https://purcelalex.com'
fly deploy
```

On that path the page and the API are on different domains, so two things that
Cloudflare made unnecessary come back: set `window.EFG_API_BASE` in the page to
the API address, and list your site in `ALLOWED_ORIGINS`. Both are read at
runtime — no code changes.
