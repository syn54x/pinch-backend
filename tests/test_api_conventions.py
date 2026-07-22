"""M3 CP1 seam: the API v1 conventions every later endpoint copies (issue #9).

Cursor pagination, the error envelope, and the served OpenAPI document are
asserted here once, against the session list — the first consumer — so that
M4+ endpoints inherit tested conventions instead of re-proving them.
"""

from pinch_backend.auth.models import Session

LOGIN = "/api/v1/auth/login"
SESSIONS = "/api/v1/auth/sessions"
SCHEMA_JSON = "/api/v1/schema/openapi.json"

PASSWORD = "correct horse battery staple"


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup_with_sessions(client, count: int) -> None:
    """One signup plus enough logins to leave ``count`` live sessions."""
    await client.post(
        "/api/v1/auth/signup",
        json={"email": "taylor@example.com", "password": PASSWORD, "display_name": "Taylor"},
        headers=await _csrf(client),
    )
    for _ in range(count - 1):
        await client.post(
            LOGIN,
            json={"email": "taylor@example.com", "password": PASSWORD},
            headers=await _csrf(client),
        )


# --- Cursor pagination (stories 8, 12) -----------------------------------------


async def test_session_list_returns_the_pagination_envelope(client) -> None:
    await _signup_with_sessions(client, 1)
    body = (await client.get(SESSIONS)).json()
    assert set(body) == {"items", "next_cursor"}
    assert len(body["items"]) == 1
    assert body["next_cursor"] is None


async def test_paginating_across_page_boundaries_covers_every_row_once(client) -> None:
    await _signup_with_sessions(client, 5)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        body = (await client.get(SESSIONS, params=params)).json()
        assert len(body["items"]) <= 2
        seen += [item["id"] for item in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert pages == 3  # 2 + 2 + 1
    # Stable uuid7 ordering: every row exactly once, in ascending id order.
    assert seen == sorted(seen)
    assert len(seen) == len(set(seen)) == 5
    assert set(seen) == {str(s.id) for s in await Session.all()}


async def test_a_full_final_page_ends_with_a_null_cursor(client) -> None:
    """next_cursor is "there is more", never "the page was full"."""
    await _signup_with_sessions(client, 2)
    first = (await client.get(SESSIONS, params={"limit": 2})).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"] is None


async def test_the_cursor_survives_deletion_of_the_row_it_points_at(client) -> None:
    """Keyset pagination: a cursor is a position, not a row reference —
    revoking the session you just paged past must not break the walk."""
    await _signup_with_sessions(client, 4)

    first = (await client.get(SESSIONS, params={"limit": 2})).json()
    cursor = first["next_cursor"]
    last_paged_id = first["items"][-1]["id"]
    row = await Session.where(lambda s: s.id == last_paged_id).first()
    await row.delete()

    rest = (await client.get(SESSIONS, params={"limit": 2, "cursor": cursor})).json()
    assert len(rest["items"]) == 2
    assert all(item["id"] > last_paged_id for item in rest["items"])


async def test_limit_is_validated_and_capped(client) -> None:
    await _signup_with_sessions(client, 1)
    for bad in (0, -1, 101, "not-a-number"):
        response = await client.get(SESSIONS, params={"limit": bad})
        assert response.status_code == 400, f"limit={bad} must be rejected"


async def test_an_invalid_cursor_answers_400_in_the_error_envelope(client) -> None:
    await _signup_with_sessions(client, 1)
    response = await client.get(SESSIONS, params={"cursor": "not-a-cursor"})
    assert response.status_code == 400
    body = response.json()
    # The documented envelope: status_code + detail (+ optional extra).
    assert body["status_code"] == 400
    assert body["detail"]
    assert "not-a-cursor" not in body["detail"]


# --- OpenAPI (story 7) ----------------------------------------------------------


async def test_openapi_schema_is_served_under_the_versioned_api(client) -> None:
    response = await client.get(SCHEMA_JSON)
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "Pinch API"
    # Every existing route is described — presence, not a brittle snapshot.
    for path in (
        "/health",
        "/api/v1/auth/signup",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/auth/me",
        "/api/v1/auth/sessions",
        "/api/v1/auth/sessions/{session_id}",
        "/api/v1/auth/pats",
        "/api/v1/auth/pats/{pat_id}",
        "/api/v1/auth/password-reset/request",
    ):
        assert path in schema["paths"], f"{path} missing from the OpenAPI document"


async def test_openapi_documents_the_f3_enabler_surface(client) -> None:
    """F3 enablers (#42): the frontend regenerates its typed client from this
    document, so all three additions must be described in it."""
    schema = (await client.get(SCHEMA_JSON)).json()
    assert "patch" in schema["paths"]["/api/v1/auth/me"]
    assert "get" in schema["paths"]["/api/v1/transactions/unreviewed-count"]
    tx_list = schema["paths"]["/api/v1/transactions"]["get"]
    assert "q" in {p["name"] for p in tx_list.get("parameters", [])}


async def test_operation_ids_are_handler_names(client) -> None:
    """Typed-client method names (frontend enabler): operation ids are the
    handler names — `list_accounts`, never `ApiV1AccountsListAccounts` —
    unique across the whole surface."""
    schema = (await client.get(SCHEMA_JSON)).json()
    ids = [
        meta["operationId"]
        for item in schema["paths"].values()
        for meta in item.values()
        if isinstance(meta, dict) and "operationId" in meta
    ]
    assert len(ids) == len(set(ids)), "operation ids must be unique"
    assert "signup" in ids and "list_accounts" in ids and "create_link_token" in ids
    assert all(oid.replace("_", "").islower() for oid in ids), "snake_case handler names only"


async def test_openapi_documents_the_pagination_convention(client) -> None:
    schema = (await client.get(SCHEMA_JSON)).json()
    sessions_get = schema["paths"]["/api/v1/auth/sessions"]["get"]
    params = {p["name"] for p in sessions_get.get("parameters", [])}
    assert {"cursor", "limit"} <= params
    # The 200 response resolves to the {items, next_cursor} envelope.
    ref = sessions_get["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    component = schema["components"]["schemas"][ref.removeprefix("#/components/schemas/")]
    assert {"items", "next_cursor"} <= set(component["properties"])


async def test_openapi_declares_both_credential_schemes(client) -> None:
    """The auth schemes are part of the served contract (story 7), and the
    bearer scheme is what gives Swagger UI its Authorize button."""
    schema = (await client.get(SCHEMA_JSON)).json()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["bearerToken"]["type"] == "http"
    assert schemes["bearerToken"]["scheme"] == "bearer"
    assert schemes["sessionCookie"]["type"] == "apiKey"
    assert schemes["sessionCookie"]["in"] == "cookie"
    # Either credential satisfies the API-wide requirement.
    assert {"bearerToken": []} in schema["security"]
    assert {"sessionCookie": []} in schema["security"]


async def test_the_docs_ui_is_swagger_plus_the_raw_document(client) -> None:
    """One interactive UI, deliberately: the convention M4 copies is
    "swagger + raw json/yaml", not Litestar's default four-UI zoo."""
    assert (await client.get("/api/v1/schema")).status_code == 200
    assert (await client.get("/api/v1/schema/swagger")).status_code == 200
    assert (await client.get(SCHEMA_JSON)).status_code == 200
    assert (await client.get("/api/v1/schema/openapi.yaml")).status_code == 200
    for gone in ("/api/v1/schema/redoc", "/api/v1/schema/rapidoc", "/api/v1/schema/elements"):
        assert (await client.get(gone)).status_code == 404, f"{gone} should be trimmed"


async def test_the_error_envelope_is_documented_as_the_contract(client) -> None:
    schema = (await client.get(SCHEMA_JSON)).json()
    description = schema["info"]["description"]
    for term in ("status_code", "detail", "extra"):
        assert term in description, f"error envelope field {term} undocumented"
