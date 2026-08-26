"""
runtime.py — where configuration and storage come from, per request.

The same FastAPI app runs in two very different places, and they disagree about
something fundamental: WHEN configuration is available.

  Docker / Fly    settings live in os.environ and are known at import time.
                  Analytics go to a SQLite file on a mounted volume.

  Cloudflare      settings arrive as a per-request `env` binding object, which
  Workers         does not exist at import time. There is no filesystem, so
                  analytics go to D1, Cloudflare's SQLite service.

Resolving both through one `Runtime` object means the routes never branch on
where they are running. Everything environment-specific is decided here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import config
from .analytics.store import AnalyticsStore

# Imported as a module rather than `from .config import settings` on purpose:
# the tests reload app.config to exercise different limits, and a direct import
# would keep a reference to the settings object from before the reload.


def worker_env(request) -> Any | None:
    """The Cloudflare `env` binding for this request, or None when not on Workers.

    Cloudflare's ASGI adapter puts the bindings object into the ASGI scope. Its
    absence is exactly how we know we are running under uvicorn instead.
    """
    try:
        return request.scope.get("env")
    except (AttributeError, TypeError):  # pragma: no cover - defensive
        return None


def _binding(env: Any, name: str) -> Any | None:
    """Read one binding or variable off the Workers env object.

    The object is a JavaScript proxy, so a missing attribute may raise rather
    than return None, and an empty string counts as absent.
    """
    if env is None:
        return None
    try:
        value = getattr(env, name, None)
    except Exception:  # pragma: no cover - JS proxy access can throw
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


@dataclass
class Runtime:
    admin_password_hash: str
    admin_session_secret: str
    allowed_origins: list[str]
    max_description_chars: int
    rate_limit_requests: int
    rate_limit_window_seconds: int
    store: Any                      # AnalyticsStore or D1AnalyticsStore
    platform: str                   # "workers" | "server"

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_password_hash and self.admin_session_secret)


# One SQLite store for the whole process when running under uvicorn. On Workers
# this is never constructed — there is no filesystem to construct it on.
_sqlite_store: AnalyticsStore | None = None


def _server_store() -> AnalyticsStore:
    global _sqlite_store
    if _sqlite_store is None:
        _sqlite_store = AnalyticsStore(config.settings.DB_PATH)
    return _sqlite_store


def _split_origins(value: str) -> list[str]:
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def runtime_for(request) -> Runtime:
    """Build the Runtime for one request."""
    env = worker_env(request)

    if env is None:
        return Runtime(
            admin_password_hash=config.settings.ADMIN_PASSWORD_HASH,
            admin_session_secret=config.settings.ADMIN_SESSION_SECRET,
            allowed_origins=config.settings.ALLOWED_ORIGINS,
            max_description_chars=config.settings.MAX_DESCRIPTION_CHARS,
            rate_limit_requests=config.settings.RATE_LIMIT_REQUESTS,
            rate_limit_window_seconds=config.settings.RATE_LIMIT_WINDOW_SECONDS,
            store=_server_store(),
            platform="server",
        )

    from .analytics.d1_store import D1AnalyticsStore

    database = _binding(env, "DB")
    origins = _binding(env, "ALLOWED_ORIGINS")

    def number(name: str, fallback: int) -> int:
        raw = _binding(env, name)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return fallback

    return Runtime(
        admin_password_hash=_binding(env, "ADMIN_PASSWORD_HASH") or "",
        admin_session_secret=_binding(env, "ADMIN_SESSION_SECRET") or "",
        # On Workers the page is served from the same Worker, so same-origin
        # requests carry no Origin header and need no allowance. This list only
        # matters if you call the API from another site.
        allowed_origins=_split_origins(origins) if origins else [],
        max_description_chars=number("MAX_DESCRIPTION_CHARS", config.settings.MAX_DESCRIPTION_CHARS),
        rate_limit_requests=number("RATE_LIMIT_REQUESTS", config.settings.RATE_LIMIT_REQUESTS),
        rate_limit_window_seconds=number(
            "RATE_LIMIT_WINDOW_SECONDS", config.settings.RATE_LIMIT_WINDOW_SECONDS
        ),
        store=D1AnalyticsStore(database) if database is not None else None,
        platform="workers",
    )


def client_ip(request) -> str:
    """The caller's address.

    Behind Cloudflare, CF-Connecting-IP is set by Cloudflare itself and cannot
    be forged by the client, so it is trustworthy — unlike X-Forwarded-For,
    which anyone can set and which is therefore ignored everywhere else.
    """
    if worker_env(request) is not None:
        forwarded = request.headers.get("cf-connecting-ip")
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"
