"""
main.py — the HTTP surface.

Routes
    POST /api/generate          description only
    POST /api/generate/paste    description + pasted table text
    POST /api/generate/file     description + uploaded .xlsx/.csv
    POST /api/admin/login       password in, session token out
    GET  /api/admin/analytics   usage summary, session token required
    GET  /api/health            readiness, and which platform is serving

One app, two homes: a container under uvicorn, and a Cloudflare Python Worker.
Nothing here branches on which — everything environment-specific is resolved by
`runtime_for(request)` in runtime.py. That is why the routes read the same in
both places and why the tests exercise the real code path rather than a
server-only variant of it.
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .config import settings
from .engine.pipeline import FormulaService
from .ingestion.paste import parse_paste
from .ingestion.sanitize import MAX_CHARS
from .ingestion.upload import MAX_FILE_BYTES, UploadRejected, parse_upload
from .runtime import client_ip, runtime_for
from .security.auth import issue_session, verify_password, verify_session
from .security.ratelimit import RateLimiter

app = FastAPI(
    title="Excel Formula Generator",
    description="Generates Excel and Google Sheets formulas from a plain-language "
                "description in Romanian or English. No external AI service is used.",
    version="0.3.0",
)

# Built once per process (or per Worker isolate) and reused. Loading the
# catalogue and building the index is the only startup cost, and it is small.
service = FormulaService()
limiter = RateLimiter(settings.RATE_LIMIT_REQUESTS, settings.RATE_LIMIT_WINDOW_SECONDS)

CORS_HEADERS = "Content-Type, Authorization"
CORS_METHODS = "GET, POST, OPTIONS"


# ---------------------------------------------------------------------------
# CORS, resolved per request
# ---------------------------------------------------------------------------
# FastAPI's CORSMiddleware takes its allow-list at import time. On Workers the
# configuration does not exist until a request arrives, so the check is done
# here instead. Same policy, later decision.

@app.middleware("http")
async def cors(request: Request, call_next):
    origin = request.headers.get("origin")
    allowed = runtime_for(request).allowed_origins
    permitted = bool(origin) and origin in allowed

    if request.method == "OPTIONS" and origin:
        if not permitted:
            # A refused preflight must not look like a successful one, or the
            # browser reports a confusing failure on the real request instead.
            return Response(status_code=403)
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": CORS_METHODS,
                "Access-Control-Allow-Headers": CORS_HEADERS,
                "Access-Control-Max-Age": "600",
                "Vary": "Origin",
            },
        )

    response = await call_next(request)
    if permitted:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def enforce_rate_limit(request: Request) -> None:
    runtime = runtime_for(request)
    limiter.max_requests = runtime.rate_limit_requests
    limiter.window_seconds = runtime.rate_limit_window_seconds

    allowed, retry_after = limiter.check(client_ip(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a moment and try again.",
            headers={"Retry-After": str(retry_after)},
        )


def require_admin(request: Request) -> None:
    runtime = runtime_for(request)
    if not runtime.admin_enabled:
        raise HTTPException(status_code=503, detail="Admin access is not configured on this server.")

    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token or not verify_session(runtime.admin_session_secret, token):
        raise HTTPException(status_code=401, detail="Not authorised.")


async def record(request: Request, description: str, result, source: str) -> None:
    """Log the request, whichever store this environment uses.

    The file-backed store is synchronous; the D1 store is not. Awaiting only
    when there is something to await keeps both routes identical.
    """
    store = runtime_for(request).store
    if store is None:
        return
    outcome = store.record(description, result, source)
    if inspect.isawaitable(outcome):
        await outcome


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
async def generate(payload: DescriptionRequest, request: Request) -> dict:
    description = payload.description.strip()
    result = service.generate(description)
    await record(request, description, result, source="none")
    return result.as_dict()


@app.post("/api/generate/paste", dependencies=[Depends(enforce_rate_limit)])
async def generate_from_paste(payload: PasteRequest, request: Request) -> dict:
    description = payload.description.strip()
    table = parse_paste(payload.table, has_header=payload.has_header)
    if not table.report.ok:
        raise HTTPException(status_code=422, detail="No table could be read from the pasted text.")

    result = service.generate(description, table)
    await record(request, description, result, source="paste")
    return result.as_dict()


@app.post("/api/generate/file", dependencies=[Depends(enforce_rate_limit)])
async def generate_from_file(
    request: Request,
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
    await record(request, description, result, source=table.report.source)
    return result.as_dict()


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.post("/api/admin/login", dependencies=[Depends(enforce_rate_limit)])
def admin_login(payload: LoginRequest, request: Request) -> dict:
    runtime = runtime_for(request)
    if not runtime.admin_enabled:
        raise HTTPException(status_code=503, detail="Admin access is not configured on this server.")

    if not verify_password(payload.password, runtime.admin_password_hash):
        # A small delay makes online guessing impractical without a lockout that
        # could be abused to lock the owner out of their own analytics.
        time.sleep(0.5)
        raise HTTPException(status_code=401, detail="Incorrect password.")

    return {"token": issue_session(runtime.admin_session_secret)}


@app.get("/api/admin/analytics", dependencies=[Depends(require_admin)])
async def admin_analytics(request: Request, limit: int = 20) -> dict:
    store = runtime_for(request).store
    if store is None:
        raise HTTPException(status_code=503, detail="No analytics database is configured.")

    outcome = store.summary(limit=max(1, min(limit, 100)))
    if inspect.isawaitable(outcome):
        outcome = await outcome
    return outcome


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health(request: Request) -> dict:
    runtime = runtime_for(request)
    return {
        "status": "ok",
        "functions_loaded": len(service.kb),
        "intents_loaded": len(service.intents.intents),
        "admin_configured": runtime.admin_enabled,
        "analytics_configured": runtime.store is not None,
        "platform": runtime.platform,
    }


@app.exception_handler(404)
async def not_found(request: Request, exc) -> JSONResponse:
    return JSONResponse({"detail": "Not found."}, status_code=404)


# ---------------------------------------------------------------------------
# The page itself — uvicorn only
# ---------------------------------------------------------------------------
# Serving the page from the API puts both on one origin, so a browser has no
# cross-origin request to block. On Cloudflare this block does nothing: there is
# no filesystem, and the page is served by the Worker's static-assets binding
# ahead of any route here.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
