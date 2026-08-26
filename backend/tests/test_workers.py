"""
test_workers.py — the Cloudflare Workers code path, exercised on the ground.

Workers differ from the container in three ways that matter, and all three are
invisible to the other test files:

  1. Configuration arrives as a per-request `env` object, not from os.environ.
  2. There is no filesystem, so analytics go to D1 instead of a SQLite file.
  3. The caller's address comes from CF-Connecting-IP.

The real runtime cannot run here, so `env` and D1 are stood in for: a plain
object with the bindings, and a D1 client backed by an in-memory SQLite
database with the same async surface. That is enough to prove the routing,
the configuration resolution and every SQL statement in d1_store.py — which is
where the mistakes would be. What it cannot prove is Pyodide-specific
behaviour, so `pywrangler dev` remains the last check before deploying.
"""

from __future__ import annotations

import importlib
import sqlite3

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

ADMIN_PASSWORD = "worker-admin-password"
SITE = "https://purcelalex.com"


# ---------------------------------------------------------------------------
# Stand-ins for the Cloudflare runtime
# ---------------------------------------------------------------------------

class FakeD1Result:
    """What D1 hands back: an object carrying `.results`."""

    def __init__(self, rows: list[dict]):
        self.results = rows


class FakeD1Statement:
    def __init__(self, connection: sqlite3.Connection, sql: str):
        self.connection = connection
        self.sql = sql
        self.params: tuple = ()

    def bind(self, *params):
        self.params = params
        return self

    def _binds(self):
        """D1 uses ?1, ?2 numbered placeholders natively.

        Python's sqlite3 treats those as NAMED parameters and refuses a plain
        sequence (an error, not a warning, from 3.14), so the tuple is turned
        into the mapping it wants. This is a quirk of the stand-in, not of D1.
        """
        return {str(i): value for i, value in enumerate(self.params, start=1)}

    async def run(self):
        self.connection.execute(self.sql, self._binds())
        self.connection.commit()
        return FakeD1Result([])

    async def all(self):
        cursor = self.connection.execute(self.sql, self._binds())
        columns = [c[0] for c in cursor.description or []]
        return FakeD1Result([dict(zip(columns, row)) for row in cursor.fetchall()])


class FakeD1:
    """The D1 binding: prepare() returning a bindable, awaitable statement."""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.executescript(
            """
            CREATE TABLE requests (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at    REAL NOT NULL,
                language      TEXT,
                platform      TEXT,
                locale        TEXT,
                source        TEXT,
                description   TEXT NOT NULL,
                intent        TEXT,
                function      TEXT,
                status        TEXT,
                column_count  INTEGER,
                column_types  TEXT
            );
            """
        )

    def prepare(self, sql: str) -> FakeD1Statement:
        return FakeD1Statement(self.connection, sql)

    def rows(self) -> list[tuple]:
        return self.connection.execute("SELECT * FROM requests").fetchall()


class FakeEnv:
    """The Workers `env`: bindings and variables as plain attributes."""

    def __init__(self, **bindings):
        for name, value in bindings.items():
            setattr(self, name, value)


class InjectEnv:
    """ASGI wrapper that puts `env` into the scope, as Cloudflare's adapter does."""

    def __init__(self, app, env):
        self.app = app
        self.env = env

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["env"] = self.env
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def database() -> FakeD1:
    return FakeD1()


@pytest.fixture()
def client(database, monkeypatch, tmp_path):
    from app.security.auth import generate_secret, hash_password

    # No filesystem store should ever be touched on this path; point it
    # somewhere harmless so a mistake shows up as an empty file, not as a write
    # into the repository.
    monkeypatch.setenv("DB_PATH", str(tmp_path / "must-stay-unused.db"))

    import app.config
    import app.main
    import app.runtime

    importlib.reload(app.config)
    importlib.reload(app.runtime)
    importlib.reload(app.main)

    env = FakeEnv(
        DB=database,
        ADMIN_PASSWORD_HASH=hash_password(ADMIN_PASSWORD),
        ADMIN_SESSION_SECRET=generate_secret(),
        ALLOWED_ORIGINS=SITE,
        RATE_LIMIT_REQUESTS="500",
    )
    return TestClient(InjectEnv(app.main.app, env))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_health_reports_the_workers_platform(client) -> None:
    body = client.get("/api/health").json()

    assert body["platform"] == "workers"
    assert body["analytics_configured"] is True
    assert body["admin_configured"] is True
    assert body["functions_loaded"] > 0


def test_generate_works_and_writes_to_d1(client, database) -> None:
    response = client.post(
        "/api/generate/paste",
        json={
            "description": "sum price where city is Chisinau",
            "table": "City\tPrice\nChisinau\t100\nIasi\t200",
        },
    )
    assert response.status_code == 200
    assert response.json()["formula"] == '=SUMIF(A2:A3,"Chisinau",B2:B3)'

    rows = database.rows()
    assert len(rows) == 1, "the request was not recorded in D1"


def test_d1_analytics_summary_reads_back(client) -> None:
    for description in ("sum the prices", "sum the prices", "qwerty zxcvb"):
        client.post("/api/generate", json={"description": description})

    token = client.post("/api/admin/login", json={"password": ADMIN_PASSWORD}).json()["token"]
    body = client.get("/api/admin/analytics", headers={"Authorization": f"Bearer {token}"}).json()

    assert body["total_requests"] == 3
    assert body["popular_requests"][0]["uses"] == 2
    assert any(row["description"] == "qwerty zxcvb" for row in body["unmet_needs"])


def test_analytics_still_needs_a_token(client) -> None:
    assert client.get("/api/admin/analytics").status_code == 401


def test_table_contents_are_not_written_to_d1(client, database) -> None:
    """The same privacy rule as the file store: descriptions yes, cells no."""
    client.post(
        "/api/generate/paste",
        json={
            "description": "sum price where city is Chisinau",
            "table": "City\tPrice\nSECRETCLIENT\t100",
        },
    )
    assert "SECRETCLIENT" not in str(database.rows())


def test_cors_allows_the_configured_origin(client) -> None:
    preflight = client.options(
        "/api/generate",
        headers={"Origin": SITE, "Access-Control-Request-Method": "POST"},
    )
    assert preflight.status_code == 204
    assert preflight.headers["Access-Control-Allow-Origin"] == SITE

    response = client.post(
        "/api/generate", json={"description": "sum column B"}, headers={"Origin": SITE}
    )
    assert response.headers.get("Access-Control-Allow-Origin") == SITE


def test_cors_refuses_an_unknown_origin(client) -> None:
    preflight = client.options(
        "/api/generate",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert preflight.status_code == 403

    response = client.post(
        "/api/generate",
        json={"description": "sum column B"},
        headers={"Origin": "https://evil.example"},
    )
    # The call itself is not blocked server-side — the missing header is what
    # makes the browser withhold the response from the calling page.
    assert "Access-Control-Allow-Origin" not in response.headers


def test_client_ip_comes_from_cloudflares_header(client) -> None:
    from app.runtime import client_ip

    class Request:
        scope = {"env": object()}
        headers = {"cf-connecting-ip": "203.0.113.7"}
        client = None

    assert client_ip(Request()) == "203.0.113.7"


def test_missing_database_binding_is_reported_not_crashed(monkeypatch, tmp_path) -> None:
    """A Worker deployed without a D1 binding should say so, not fall over."""
    from app.security.auth import generate_secret, hash_password

    monkeypatch.setenv("DB_PATH", str(tmp_path / "unused.db"))

    import app.config
    import app.main
    import app.runtime

    importlib.reload(app.config)
    importlib.reload(app.runtime)
    importlib.reload(app.main)

    env = FakeEnv(
        ADMIN_PASSWORD_HASH=hash_password(ADMIN_PASSWORD),
        ADMIN_SESSION_SECRET=generate_secret(),
    )
    unbound = TestClient(InjectEnv(app.main.app, env))

    assert unbound.get("/api/health").json()["analytics_configured"] is False
    # Generating still works; only the logging is unavailable.
    assert unbound.post("/api/generate", json={"description": "sum column B"}).status_code == 200

    token = unbound.post("/api/admin/login", json={"password": ADMIN_PASSWORD}).json()["token"]
    response = unbound.get("/api/admin/analytics", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
