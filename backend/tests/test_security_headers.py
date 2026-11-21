import pytest
from fastapi.testclient import TestClient

from app.db import get_store
from app.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, get_store, lambda: None)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_store, None)


def test_security_headers_are_present(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Permitted-Cross-Domain-Policies"] == "none"
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Strict-Transport-Security" not in response.headers


def test_hsts_is_enabled_only_in_production(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"
