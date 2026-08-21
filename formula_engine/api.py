"""
Minimal FastAPI backend around the local FormulaEngine.

Security posture baked in (the parts that matter for a public, free-per-request tool):
  - hard input-size cap (rejects giant payloads before any work)
  - simple per-IP rate limit (swap for Redis in production / multi-instance)
  - every request logged to SQLite for the analytics menu
  - admin analytics behind a bearer token (replace with real auth before launch)

Run:  pip install fastapi uvicorn
      uvicorn api:app --reload
"""

import os
import sqlite3
import time
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request, Header
from pydantic import BaseModel, Field

from engine import FormulaEngine
from table_parser import parse_table, MAX_CHARS as TABLE_MAX_CHARS
from formula_builder import TableAwareBuilder

MAX_INPUT_CHARS = 500          # a formula description is short; reject the rest
RATE_LIMIT = 20                # requests
RATE_WINDOW = 60               # seconds, per IP
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "change-me-before-launch")
DB_PATH = os.environ.get("DB_PATH", "analytics.db")

app = FastAPI(title="Formula Engine")
engine = FormulaEngine()
builder = TableAwareBuilder(engine)
_hits: dict[str, deque] = defaultdict(deque)


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS inputs ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts REAL, language TEXT, platform TEXT,"
        " description TEXT, best_function TEXT, status TEXT)"
    )
    return conn


def rate_limited(ip: str) -> bool:
    now = time.time()
    dq = _hits[ip]
    while dq and dq[0] < now - RATE_WINDOW:
        dq.popleft()
    if len(dq) >= RATE_LIMIT:
        return True
    dq.append(now)
    return False


class GenerateRequest(BaseModel):
    description: str = Field(..., max_length=MAX_INPUT_CHARS)


@app.post("/generate")
def generate(req: GenerateRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if rate_limited(ip):
        raise HTTPException(status_code=429, detail="Too many requests")

    text = req.description.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty description")

    result = engine.generate(text)

    # log for analytics (store the description + outcome, NOT screenshots)
    conn = _db()
    conn.execute(
        "INSERT INTO inputs (ts, language, platform, description, best_function, status)"
        " VALUES (?,?,?,?,?,?)",
        (time.time(), result.get("language"), result.get("platform"),
         text, result.get("best_function"), result.get("status")),
    )
    conn.commit()
    conn.close()
    return result


class TableRequest(BaseModel):
    description: str = Field(..., max_length=MAX_INPUT_CHARS)
    table: str = Field(..., max_length=TABLE_MAX_CHARS)   # pasted TSV/CSV/HTML text
    has_header: bool | None = None


@app.post("/generate_with_table")
def generate_with_table(req: TableRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if rate_limited(ip):
        raise HTTPException(status_code=429, detail="Too many requests")

    text = req.description.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty description")

    parsed = parse_table(req.table, has_header=req.has_header)
    if not parsed.report.safe:
        raise HTTPException(status_code=422,
                            detail="Could not parse a table from the pasted text")

    result = builder.build(text, parsed)

    conn = _db()
    conn.execute(
        "INSERT INTO inputs (ts, language, platform, description, best_function, status)"
        " VALUES (?,?,?,?,?,?)",
        (time.time(), result.get("language"), result.get("platform"),
         text, result.get("best_function"), result.get("status")),
    )
    conn.commit()
    conn.close()
    return result


@app.get("/admin/analytics")
def analytics(authorization: str = Header(default="")):
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = _db()
    top_desc = conn.execute(
        "SELECT description, COUNT(*) c FROM inputs"
        " GROUP BY description ORDER BY c DESC LIMIT 20"
    ).fetchall()
    top_func = conn.execute(
        "SELECT best_function, COUNT(*) c FROM inputs"
        " GROUP BY best_function ORDER BY c DESC LIMIT 20"
    ).fetchall()
    by_lang = conn.execute(
        "SELECT language, COUNT(*) c FROM inputs GROUP BY language"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM inputs").fetchone()[0]
    conn.close()
    return {
        "total_requests": total,
        "top_descriptions": [{"text": d, "count": c} for d, c in top_desc],
        "top_functions": [{"function": f, "count": c} for f, c in top_func],
        "by_language": [{"language": l, "count": c} for l, c in by_lang],
    }


@app.get("/health")
def health():
    return {"status": "ok", "functions_loaded": len(engine.functions)}
