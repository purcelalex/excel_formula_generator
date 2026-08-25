"""
config.py — every tunable in one place, read from the environment.

Defaults are chosen so that a fresh clone runs correctly with no .env file, and
so that no default is dangerous in production. The admin password hash has no
usable default at all: without one, the admin routes refuse to serve rather
than falling back to a known secret.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _default_db_path() -> str:
    """Where analytics.db lives when DB_PATH is unset.

    In a container it is /data, a mounted volume. On Windows it is LOCALAPPDATA,
    outside any synced folder — an SQLite file inside OneDrive or Dropbox gets
    uploaded mid-write and corrupts, so the default must not put it there.
    """
    if os.path.isdir("/data"):
        return "/data/analytics.db"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return str(Path(base) / "ExcelFormulaGenerator" / "analytics.db")
    return str(BASE_DIR / "analytics.db")


class Settings:
    # --- storage ---
    DB_PATH: str = os.environ.get("DB_PATH") or _default_db_path()
    KB_PATH: Path = Path(os.environ.get("KB_PATH") or BASE_DIR / "data" / "knowledge_base.json")
    INTENTS_PATH: Path = Path(os.environ.get("INTENTS_PATH") or BASE_DIR / "data" / "intents.json")

    # --- input limits ---
    MAX_DESCRIPTION_CHARS: int = int(os.environ.get("MAX_DESCRIPTION_CHARS", 500))

    # --- rate limiting (per client IP) ---
    RATE_LIMIT_REQUESTS: int = int(os.environ.get("RATE_LIMIT_REQUESTS", 20))
    RATE_LIMIT_WINDOW_SECONDS: int = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", 60))

    # --- admin ---
    # A bcrypt/pbkdf2 hash, never a plaintext password. Empty means the admin
    # routes are disabled, which is the correct behaviour for an unconfigured
    # deployment: no access beats default access.
    ADMIN_PASSWORD_HASH: str = os.environ.get("ADMIN_PASSWORD_HASH", "")
    ADMIN_SESSION_SECRET: str = os.environ.get("ADMIN_SESSION_SECRET", "")

    # --- CORS ---
    # Comma-separated list of origins allowed to call the API. The default is
    # local development only; production must set this to the real domain.
    ALLOWED_ORIGINS: list[str] = [
        origin.strip()
        for origin in os.environ.get(
            "ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
        ).split(",")
        if origin.strip()
    ]

    @property
    def admin_enabled(self) -> bool:
        return bool(self.ADMIN_PASSWORD_HASH and self.ADMIN_SESSION_SECRET)


settings = Settings()
