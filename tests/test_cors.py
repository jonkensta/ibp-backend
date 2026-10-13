"""CORS is disabled unless origins are explicitly configured."""

import configparser

import pytest
from fastapi.testclient import TestClient

from ibp import base

CROSS_ORIGIN = "https://evil.example.com"


@pytest.fixture(name="client")
def fixture_client() -> TestClient:
    """Client for the app without running the lifespan (no logging setup)."""
    return TestClient(base.app)


def test_no_cors_headers_on_simple_request(client: TestClient) -> None:
    """A cross-origin request gets no Access-Control-* headers by default."""
    response = client.get("/openapi.json", headers={"Origin": CROSS_ORIGIN})
    assert response.status_code == 200
    assert not any(h.lower().startswith("access-control-") for h in response.headers)


def test_no_cors_headers_on_preflight(client: TestClient) -> None:
    """A CORS preflight is not answered with an allow-origin header by default."""
    response = client.options(
        "/openapi.json",
        headers={
            "Origin": CROSS_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-credentials" not in response.headers


def _config(text: str) -> configparser.ConfigParser:
    parser = base.EnvInterpolatingConfigParser()
    parser.read_string(text)
    return parser


def test_cors_origins_default_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing section or empty value means no CORS origins."""
    monkeypatch.setattr(base, "config", _config(""))
    assert not base.get_cors_origins()
    monkeypatch.setattr(base, "config", _config("[server]\ncors_origins =\n"))
    assert not base.get_cors_origins()


def test_cors_origins_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Origins may be separated by commas and/or whitespace."""
    text = "[server]\ncors_origins = http://localhost:5173,\n  http://a.test b.test\n"
    monkeypatch.setattr(base, "config", _config(text))
    assert base.get_cors_origins() == [
        "http://localhost:5173",
        "http://a.test",
        "b.test",
    ]
