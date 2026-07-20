"""CORS for the browser frontend (frontend enabler, post-M7).

The frontend origin (``frontend_base_url`` — the same origin outbound mail
links point at) may call the API cross-origin with credentials; any other
origin gets nothing. Credentialed CORS means exact origins, never ``*``.
"""

CONNECTIONS = "/api/v1/connections"
FRONTEND_ORIGIN = "http://localhost:5173"


async def test_preflight_allows_the_frontend_origin(client) -> None:
    response = await client.options(
        CONNECTIONS,
        headers={
            "Origin": FRONTEND_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-csrftoken",
        },
    )
    assert response.status_code in (200, 204), response.text
    assert response.headers["access-control-allow-origin"] == FRONTEND_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    allowed_headers = response.headers.get("access-control-allow-headers", "").lower()
    assert "x-csrftoken" in allowed_headers
    assert "content-type" in allowed_headers


async def test_simple_response_carries_cors_headers(client) -> None:
    response = await client.get("/health", headers={"Origin": FRONTEND_ORIGIN})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


async def test_foreign_origin_is_not_allowed(client) -> None:
    response = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert response.headers.get("access-control-allow-origin") != "https://evil.example"
