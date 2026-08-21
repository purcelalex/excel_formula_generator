# Running the project in Docker

## First run

Open PowerShell in the project folder (the one containing `formula_engine/`):

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The first build takes a minute or two. When it finishes, open
<http://localhost:8000/health> — you should see:

```json
{"status": "ok", "functions_loaded": 30}
```

Stop it with `Ctrl+C`, or `docker compose down` from another terminal.

## Everyday use

| Command | What it does |
|---|---|
| `docker compose up` | Start (no rebuild — use this normally) |
| `docker compose up --build` | Rebuild, needed only after changing `requirements.txt` |
| `docker compose down` | Stop and remove the container |
| `docker compose logs -f` | Follow the logs |
| `docker compose exec api bash` | Shell inside the container |

Editing any `.py` file in `formula_engine/` reloads the server automatically —
no rebuild, no restart.

## Try the API

```powershell
# Romanian, no table
curl.exe -X POST http://localhost:8000/generate -H "Content-Type: application/json" -d '{\"description\":\"aduna coloana B unde coloana A este un oras\"}'

# English, with a pasted table
curl.exe -X POST http://localhost:8000/generate_with_table -H "Content-Type: application/json" -d '{\"description\":\"sum price where city is Chisinau\",\"table\":\"City\tName\tPrice\nChisinau\tAna\t100\nIasi\tBogdan\t200\"}'
```

Easier: open <http://localhost:8000/docs> — FastAPI generates an interactive test
page for every endpoint, and you can fill the forms in the browser.

## Where the database lives

`analytics.db` is **not** in your project folder. It sits on a Docker named
volume called `excel-formula-generator_analytics`, inside Docker's own storage.
That is deliberate: an SQLite file inside OneDrive gets synced mid-write and
corrupts. This way OneDrive never sees it.

```powershell
docker volume ls                    # list volumes
docker compose down -v              # delete the container AND the analytics data
```

Note that `down -v` wipes the analytics database. Plain `down` keeps it.

## No virtual environment

There is no `.venv` on your laptop and there should not be one. Python and every
dependency live inside the image. If VS Code complains about unresolved imports,
either install the Dev Containers extension and "Reopen in Container", or ignore
it — the editor is looking at your laptop's Python, while the code runs in the
container's.

## Notes

- **Python 3.12 is required.** `formula_builder.py` uses backslashes inside
  f-string expressions, which is a syntax error on 3.11 and earlier. The image
  pins 3.12 for this reason.
- **`.env` is gitignored.** Never commit the real `ADMIN_TOKEN`. Generate one
  with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- **The container runs as a non-root user** (`appuser`, uid 1000).
- **`--reload` is for development only.** The production image runs the plain
  `CMD` in the Dockerfile, without reload.
