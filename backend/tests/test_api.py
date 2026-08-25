"""
test_api.py — the HTTP layer: status codes, limits, and who can see what.

Uses FastAPI's TestClient, so these run without a server or a container.
"""

from __future__ import annotations

import importlib
import io

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

ADMIN_PASSWORD = "test-admin-password"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A fresh app with its own database and a known admin password."""
    from app.security.auth import generate_secret, hash_password

    monkeypatch.setenv("DB_PATH", str(tmp_path / "analytics.db"))
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(ADMIN_PASSWORD))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", generate_secret())
    # Generous limit so ordinary tests are not throttled by each other.
    monkeypatch.setenv("RATE_LIMIT_REQUESTS", "500")

    import app.config
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.main)
    return TestClient(app.main.app)


def test_health(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["functions_loaded"] > 0
    assert body["intents_loaded"] > 0


def test_generate_from_description(client) -> None:
    response = client.post("/api/generate", json={"description": "sum column B where column A is a city"})
    assert response.status_code == 200

    body = response.json()
    assert body["formula"].startswith("=SUMIF(")
    assert body["rationale"]


def test_generate_from_paste(client) -> None:
    response = client.post(
        "/api/generate/paste",
        json={
            "description": "sum price where city is Chisinau",
            "table": "City\tName\tPrice\nChisinau\tAna\t100\nIasi\tBogdan\t200",
        },
    )
    assert response.status_code == 200

    body = response.json()
    assert body["formula"] == '=SUMIF(A2:A3,"Chisinau",C2:C3)'
    assert body["safety"]["source"] == "paste"
    assert [c["header"] for c in body["columns"]] == ["City", "Name", "Price"]


def test_generate_from_csv_upload(client) -> None:
    csv = b"City,Price\nChisinau,100\nIasi,200\n"
    response = client.post(
        "/api/generate/file",
        data={"description": "sum price where city is Chisinau"},
        files={"file": ("data.csv", io.BytesIO(csv), "text/csv")},
    )
    assert response.status_code == 200
    assert response.json()["formula"] == '=SUMIF(A2:A3,"Chisinau",B2:B3)'


def test_upload_of_a_macro_workbook_is_refused(client) -> None:
    response = client.post(
        "/api/generate/file",
        data={"description": "sum price"},
        files={"file": ("book.xlsm", io.BytesIO(b"PK\x03\x04junk"), "application/vnd.ms-excel")},
    )
    assert response.status_code == 422
    assert "macro" in response.json()["detail"].lower()


def test_overlong_description_is_rejected(client) -> None:
    response = client.post("/api/generate", json={"description": "x" * 5000})
    assert response.status_code == 422


def test_empty_description_is_rejected(client) -> None:
    response = client.post("/api/generate", json={"description": ""})
    assert response.status_code == 422


def test_unreadable_paste_is_rejected(client) -> None:
    response = client.post("/api/generate/paste", json={"description": "sum it", "table": "   "})
    assert response.status_code == 422


# ---- admin ---------------------------------------------------------------

def test_analytics_requires_a_token(client) -> None:
    assert client.get("/api/admin/analytics").status_code == 401


def test_analytics_rejects_a_made_up_token(client) -> None:
    response = client.get("/api/admin/analytics", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401


def test_login_with_the_wrong_password_fails(client) -> None:
    assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401


def test_login_then_read_analytics(client) -> None:
    client.post("/api/generate", json={"description": "sum column B"})

    login = client.post("/api/admin/login", json={"password": ADMIN_PASSWORD})
    assert login.status_code == 200
    token = login.json()["token"]

    response = client.get("/api/admin/analytics", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200

    body = response.json()
    assert body["total_requests"] >= 1
    assert "unmet_needs" in body


def test_analytics_does_not_store_table_contents(client) -> None:
    """Column types are logged; the user's actual data is not."""
    client.post(
        "/api/generate/paste",
        json={
            "description": "sum price where city is Chisinau",
            "table": "City\tPrice\nChisinau\t100",
        },
    )
    token = client.post("/api/admin/login", json={"password": ADMIN_PASSWORD}).json()["token"]
    body = client.get("/api/admin/analytics", headers={"Authorization": f"Bearer {token}"}).text

    assert "Chisinau" not in body or "sum price where city is Chisinau" in body, (
        "table cell values leaked into analytics"
    )


def test_rate_limit_returns_429(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "rate.db"))
    monkeypatch.setenv("RATE_LIMIT_REQUESTS", "3")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")

    import app.config
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.main)
    limited_client = TestClient(app.main.app)

    payload = {"description": "sum column B"}
    for _ in range(3):
        assert limited_client.post("/api/generate", json=payload).status_code == 200

    blocked = limited_client.post("/api/generate", json=payload)
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
