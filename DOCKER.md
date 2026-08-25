# Running the project in Docker

## First run

In PowerShell, from the project folder:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Open <http://localhost:8000/api/health> — you should see the function and intent
counts. Then open <http://localhost:8000/docs>, where FastAPI provides a page to
try every endpoint from the browser.

Stop with `Ctrl+C`, or `docker compose down` from another terminal.

## Everyday commands

| Command | What it does |
|---|---|
| `docker compose up` | Start (no rebuild — the normal case) |
| `docker compose up --build` | Rebuild; needed only after `requirements.txt` changes |
| `docker compose down` | Stop and remove the container |
| `docker compose logs -f` | Follow the logs |
| `docker compose exec api python -m pytest -q` | Run the test suite |
| `docker compose exec api bash` | A shell inside the container |

Editing any file under `backend/` reloads the server automatically.

## Enabling the admin analytics

The admin routes return **503 until configured** — an unconfigured deployment
gets no admin access rather than a default one.

```powershell
docker compose exec api python -m app.security.auth "choose a strong password"
```

It prints two lines. Paste both into `.env`, then `docker compose restart api`.

Log in by POSTing the password to `/api/admin/login`; it returns a token that
expires after eight hours. Send it as `Authorization: Bearer <token>` to
`/api/admin/analytics`. The password itself is never stored — only a PBKDF2
hash, which cannot be reversed.

## Where the database lives

`analytics.db` is **not** in your project folder. It sits on a Docker named
volume, `excel-formula-generator_analytics`, inside Docker's own storage. An
SQLite file inside a syncing folder gets uploaded mid-write and corrupts; this
avoids the problem rather than working around it.

```powershell
docker volume ls                    # list volumes
docker compose down                 # stop, KEEPING the analytics data
docker compose down -v              # stop and DELETE the analytics data
```

## Trying it from the command line

```powershell
# Romanian, no table
curl.exe -X POST http://localhost:8000/api/generate -H "Content-Type: application/json" -d '{\"description\":\"aduna coloana B unde coloana A este un oras\"}'

# English, with a pasted table
curl.exe -X POST http://localhost:8000/api/generate/paste -H "Content-Type: application/json" -d '{\"description\":\"sum price where city is Chisinau\",\"table\":\"City\tName\tPrice\nChisinau\tAna\t100\nIasi\tBogdan\t200\"}'

# A file upload
curl.exe -X POST http://localhost:8000/api/generate/file -F "description=sum price where city is Chisinau" -F "file=@sample.csv"
```

The `/docs` page is easier for anything more than a quick check.

## Notes

- **Python 3.12 or newer is required.** The image pins 3.12.
- **`.env` is gitignored.** The admin hash and session secret belong there and
  nowhere else.
- **The container runs as a non-root user** (`appuser`, uid 1000).
- **`--reload` is development only.** The production image runs the plain `CMD`
  in the Dockerfile, without it.
- **No `.venv` on your laptop.** If VS Code reports unresolved imports, it is
  looking at your machine's Python while the code runs in the container's. The
  Dev Containers extension resolves it.
