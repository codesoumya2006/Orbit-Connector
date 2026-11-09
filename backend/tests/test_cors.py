import pytest
from fastapi.testclient import TestClient

from app.main import app


ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
)


@app.get("/__test/cors-server-error")
def raise_cors_test_error():
    raise RuntimeError("CORS server error test")


@pytest.mark.parametrize("origin", ORIGINS)
def test_cors_preflight_allows_authenticated_organization_request(origin):
    with TestClient(app) as client:
        response = client.options(
            "/api/v1/organizations",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "GET" in response.headers["access-control-allow-methods"]
    assert "OPTIONS" in response.headers["access-control-allow-methods"]
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


@pytest.mark.parametrize("origin", ORIGINS)
def test_cors_headers_are_present_on_server_error(origin):
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/__test/cors-server-error",
            headers={"Origin": origin},
        )

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"


@pytest.mark.parametrize("origin", ORIGINS)
def test_cors_headers_are_present_on_organization_auth_error(origin):
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/organizations",
            headers={"Origin": origin},
        )

    assert response.status_code in (401, 403)
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
