"""
main.py — the HTTP surface.

Routes
    POST /api/generate          description only
    POST /api/generate/paste    description + pasted table text
    POST /api/generate/file     description + uploaded .xlsx/.csv
    POST /api/admin/login       password in, session token out
    GET  /api/admin/analytics   usage summary, session token required
    GET  /api/health            readiness

Every generate route funnels into the same FormulaService and the same
analytics record, so behaviour cannot drift between input methods.
"""

from __future__ import annotations

import time

from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .analytics.store import AnalyticsStore
from .config import settings
from .engine.pipeline import FormulaService
from .ingestion.paste import parse_paste
from .ingestion.sanitize import MAX_CHARS
from .ingestion.upload import MAX_FILE_BYTES, UploadRejected, parse_upload
from .security.auth import issue_session, verify_password, verify_session
from .security.ratelimit import RateLimiter

app = FastAPI(
    title="Excel Formula Generator",
    description="Generates Excel and Google Sheets formulas from a plain-language "
                "description in Romanian or English. No external AI service is used.",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)

service = FormulaService()
store = AnalyticsStore(settings.DB_PATH)
limiter = RateLimiter(settings.RATE_LIMIT_REQUESTS, settings.RATE_LIMIT_WINDOW_SECONDS)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def client_ip(request: Request) -> str:
    """The connecting address.

    X-Forwarded-For is ignored on purpose: it is attacker-controlled unless a
    trusted proxy is known to overwrite it. When this runs behind Cloudflare,
    read CF-Connecting-IP here and only then.
    """
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(request: Request) -> None:
    allowed, retry_after = limiter.check(client_ip(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a moment and try again.",
            headers={"Retry-After": str(retry_after)},
        )


def require_admin(authorization: str = Header(default="")) -> None:
    if not settings.admin_enabled:
        raise HTTPException(status_code=503, detail="Admin access is not configured on this server.")
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not verify_session(settings.ADMIN_SESSION_SECRET, token):
        raise HTTPException(status_code=401, detail="Not authorised.")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class DescriptionRequest(BaseModel):
    description: str = Field(..., min_length=1, max_length=settings.MAX_DESCRIPTION_CHARS)


class PasteRequest(DescriptionRequest):
    table: str = Field(..., min_length=1, max_length=MAX_CHARS)
    has_header: bool | None = None


class LoginRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=256)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

@app.post("/api/generate", dependencies=[Depends(enforce_rate_limit)])
def generate(payload: DescriptionRequest) -> dict:
    description = payload.description.strip()
    result = service.generate(description)
    store.record(description, result, source="none")
    return result.as_dict()


@app.post("/api/generate/paste", dependencies=[Depends(enforce_rate_limit)])
def generate_from_paste(payload: PasteRequest) -> dict:
    description = payload.description.strip()
    table = parse_paste(payload.table, has_header=payload.has_header)
    if not table.report.ok:
        raise HTTPException(status_code=422, detail="No table could be read from the pasted text.")

    result = service.generate(description, table)
    store.record(description, result, source="paste")
    return result.as_dict()


@app.post("/api/generate/file", dependencies=[Depends(enforce_rate_limit)])
async def generate_from_file(
    description: str = Form(..., max_length=settings.MAX_DESCRIPTION_CHARS),
    file: UploadFile = File(...),
) -> dict:
    # Read with a hard ceiling rather than trusting Content-Length, which the
    # client controls. One byte over the limit is enough to reject.
    data = await file.read(MAX_FILE_BYTES + 1)
    await file.close()

    try:
        table = parse_upload(file.filename or "", data)
    except UploadRejected as rejection:
        raise HTTPException(status_code=422, detail=str(rejection)) from rejection

    if not table.report.ok:
        raise HTTPException(status_code=422, detail="No table could be read from this file.")

    description = description.strip()
    result = service.generate(description, table)
    store.record(description, result, source=table.report.source)
    return result.as_dict()


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.post("/api/admin/login", dependencies=[Depends(enforce_rate_limit)])
def admin_login(payload: LoginRequest) -> dict:
    if not settings.admin_enabled:
        raise HTTPException(status_code=503, detail="Admin access is not configured on this server.")

    if not verify_password(payload.password, settings.ADMIN_PASSWORD_HASH):
        # A small delay makes online guessing impractical without a lockout that
        # could be abused to lock the owner out of their own analytics.
        time.sleep(0.5)
        raise HTTPException(status_code=401, detail="Incorrect password.")

    return {"token": issue_session(settings.ADMIN_SESSION_SECRET)}


@app.get("/api/admin/analytics", dependencies=[Depends(require_admin)])
def admin_analytics(limit: int = 20) -> dict:
    return store.summary(limit=max(1, min(limit, 100)))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "functions_loaded": len(service.kb),
        "intents_loaded": len(service.intents.intents),
        "admin_configured": settings.admin_enabled,
    }


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------
# Serving the frontend from the API is a development convenience with a real
# purpose: it puts the page and the API on the SAME ORIGIN, so the browser has
# no cross-origin request to block. Opening index.html straight from disk gives
# it a file:// address, which every browser refuses to let call localhost.
#
# In production this does not apply — Cloudflare Pages serves the page and the
# API lives on its own domain, which is what ALLOWED_ORIGINS is for. The mount
# is last so that it can never shadow an /api route.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
