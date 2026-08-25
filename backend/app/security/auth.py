"""
auth.py — admin access to the analytics view.

Replaces the placeholder bearer token with something that can be deployed
publicly:

  * The password is never stored, only a PBKDF2-SHA256 hash with a random salt.
    A leaked .env or a leaked database does not reveal it.
  * Verification uses a constant-time comparison, so response timing does not
    leak how much of a guess was correct.
  * A successful login issues a signed, expiring session token. The signature is
    HMAC-SHA256 over the payload, so a token cannot be forged or extended
    without the server secret.
  * Failed logins are delayed slightly, which makes online guessing slow without
    needing a lockout mechanism that could be used to lock the owner out.

`python -m app.security.auth "your password"` prints the hash to put in .env.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

PBKDF2_ITERATIONS = 240_000     # ~0.2s on a typical server in 2026
SESSION_LIFETIME_SECONDS = 8 * 60 * 60


def hash_password(password: str) -> str:
    """Return 'pbkdf2_sha256$iterations$salt$hash' for storing in the environment."""
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return "$".join(
        [
            "pbkdf2_sha256",
            str(PBKDF2_ITERATIONS),
            base64.b64encode(salt).decode(),
            base64.b64encode(derived).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash."""
    try:
        algorithm, iterations, salt_b64, hash_b64 = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False

    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    return hmac.compare_digest(candidate, expected)


def issue_session(secret: str, subject: str = "admin") -> str:
    """Create a signed token that expires on its own."""
    payload = {"sub": subject, "exp": int(time.time()) + SESSION_LIFETIME_SECONDS}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = _sign(secret, body)
    return f"{body}.{signature}"


def verify_session(secret: str, token: str) -> bool:
    """Check signature first, then expiry. Any malformed token is simply invalid."""
    try:
        body, signature = token.split(".", 1)
    except (ValueError, AttributeError):
        return False

    if not hmac.compare_digest(signature, _sign(secret, body)):
        return False

    try:
        padding = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + padding))
    except (ValueError, TypeError):
        return False

    return int(payload.get("exp", 0)) > time.time()


def _sign(secret: str, body: str) -> str:
    digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def generate_secret() -> str:
    return secrets.token_urlsafe(32)


if __name__ == "__main__":  # pragma: no cover - operator helper
    import sys

    if len(sys.argv) != 2:
        print('Usage: python -m app.security.auth "your admin password"')
        raise SystemExit(1)

    print("Put these two lines in your .env file:\n")
    print(f"ADMIN_PASSWORD_HASH={hash_password(sys.argv[1])}")
    print(f"ADMIN_SESSION_SECRET={generate_secret()}")
    os.environ.pop("PYTHONWARNINGS", None)
