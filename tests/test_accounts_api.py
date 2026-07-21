"""M4 CP1 seam: manual accounts and balance entries over the public API
(issue #14 — the first domain endpoints).

Everything here is the M3 conventions meeting real domain data: Page[T]
lists, the error envelope, tenancy 404s, and the write-scope guard's first
real success path (retiring the test-only write probe's exclusivity).
"""

import uuid

from pinch_backend.models import BalanceEntry

ACCOUNTS = "/api/v1/accounts"
SCHEMA_JSON = "/api/v1/schema/openapi.json"

PASSWORD = "correct horse battery staple"

ACCOUNT_FIELDS = {
    "id",
    "mask",
    "kind",
    "label",
    "currency",
    "manual",
    "archived",
    "balance",
    "created_at",
}
BALANCE_ENTRY_FIELDS = {"id", "amount_minor", "currency", "as_of", "source", "created_at"}


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email: str = "taylor@example.com") -> None:
    response = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "display_name": "Taylor"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text


async def _logout(client) -> None:
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))


async def _mint_pat(client, scopes: list[str]) -> str:
    response = await client.post(
        "/api/v1/auth/pats",
        json={"name": f"{'-'.join(scopes)}-pat", "scopes": scopes},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_account(client, *, kind: str = "depository", label: str = "Checking", **extra):
    payload = {"kind": kind, "label": label, "currency": "USD"} | extra
    response = await client.post(ACCOUNTS, json=payload, headers=await _csrf(client))
    assert response.status_code == 201, response.text
    return response.json()


async def _enter_balance(client, account_id: str, amount_minor: int, **extra):
    response = await client.post(
        f"{ACCOUNTS}/{account_id}/balance-entries",
        json={"amount_minor": amount_minor} | extra,
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- Create and shape (story 1) ---------------------------------------------------


async def test_create_returns_the_allowlisted_account_shape(client) -> None:
    await _signup(client)
    account = await _create_account(client, kind="credit", label="Sapphire", currency="USD")

    assert set(account) == ACCOUNT_FIELDS
    assert account["kind"] == "credit"
    assert account["label"] == "Sapphire"
    assert account["currency"] == "USD"
    assert account["manual"] is True  # no connection: manual by construction
    assert account["archived"] is False
    assert account["balance"] is None  # no entries yet — never a fake zero


async def test_create_validates_kind_label_and_currency(client) -> None:
    await _signup(client)
    headers = await _csrf(client)
    for bad in (
        {"kind": "checking", "label": "x", "currency": "USD"},  # not an AccountKind
        {"kind": "depository", "label": "", "currency": "USD"},
        {"kind": "depository", "label": "x", "currency": "usd"},
        {"kind": "depository", "label": "x", "currency": "US"},
        {"kind": "depository", "label": "x"},  # currency is explicit, always (I-1)
        {"label": "x", "currency": "USD"},
    ):
        response = await client.post(ACCOUNTS, json=bad, headers=headers)
        assert response.status_code == 400, f"{bad} must be rejected"


# --- List and pagination (stories 1, 12) -------------------------------------------


async def test_accounts_list_pages_on_the_m3_convention(client) -> None:
    await _signup(client)
    for n in range(3):
        await _create_account(client, label=f"Account {n}")

    first = (await client.get(ACCOUNTS, params={"limit": 2})).json()
    assert set(first) == {"items", "next_cursor"}
    assert len(first["items"]) == 2
    rest = (await client.get(ACCOUNTS, params={"limit": 2, "cursor": first["next_cursor"]})).json()
    assert len(rest["items"]) == 1
    assert rest["next_cursor"] is None

    ids = [item["id"] for item in first["items"] + rest["items"]]
    assert ids == sorted(ids)  # uuid7 creation order
    assert (await client.get(ACCOUNTS, params={"cursor": "junk"})).status_code == 400


async def test_get_returns_the_account_and_unknown_ids_answer_404(client) -> None:
    await _signup(client)
    account = await _create_account(client)

    fetched = (await client.get(f"{ACCOUNTS}/{account['id']}")).json()
    assert fetched == account

    assert (await client.get(f"{ACCOUNTS}/{uuid.uuid7()}")).status_code == 404


# --- Update label and archive (story 3) --------------------------------------------


async def test_update_label_changes_the_label_and_nothing_else(client) -> None:
    await _signup(client)
    account = await _create_account(client, label="Chekcing")

    response = await client.patch(
        f"{ACCOUNTS}/{account['id']}",
        json={"label": "Checking"},
        headers=await _csrf(client),
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["label"] == "Checking"
    assert updated | {"label": account["label"]} == account

    bad = await client.patch(
        f"{ACCOUNTS}/{account['id']}", json={"label": ""}, headers=await _csrf(client)
    )
    assert bad.status_code == 400


async def test_archive_is_an_idempotent_flag_flip(client) -> None:
    await _signup(client)
    account = await _create_account(client)

    first = await client.post(f"{ACCOUNTS}/{account['id']}/archive", headers=await _csrf(client))
    assert first.status_code == 200
    assert first.json()["archived"] is True

    again = await client.post(f"{ACCOUNTS}/{account['id']}/archive", headers=await _csrf(client))
    assert again.status_code == 200
    assert again.json()["archived"] is True

    # Archived accounts keep their history and stay listed (story 3).
    listed = (await client.get(ACCOUNTS)).json()["items"]
    assert [item["archived"] for item in listed] == [True]


async def test_account_delete_does_not_exist(client) -> None:
    await _signup(client)
    account = await _create_account(client)
    response = await client.delete(f"{ACCOUNTS}/{account['id']}", headers=await _csrf(client))
    assert response.status_code == 405


# --- Balance entries (story 2) ------------------------------------------------------


async def test_a_hand_entered_balance_becomes_the_current_balance(client) -> None:
    await _signup(client)
    account = await _create_account(client)

    entry = await _enter_balance(client, account["id"], 125_00, as_of="2026-07-01T00:00:00Z")
    assert set(entry) == BALANCE_ENTRY_FIELDS
    assert entry["amount_minor"] == 125_00
    assert entry["currency"] == "USD"  # from the account, never the client
    assert entry["source"] == "manual"

    balance = (await client.get(f"{ACCOUNTS}/{account['id']}")).json()["balance"]
    assert balance == {"amount_minor": 125_00, "currency": "USD", "as_of": entry["as_of"]}

    later = await _enter_balance(client, account["id"], 90_00, as_of="2026-07-10T00:00:00Z")
    balance = (await client.get(f"{ACCOUNTS}/{account['id']}")).json()["balance"]
    assert balance["amount_minor"] == 90_00
    assert balance["as_of"] == later["as_of"]


async def test_a_backdated_entry_extends_history_without_moving_the_balance(client) -> None:
    """The current balance is the latest entry by as_of — a backfilled
    older observation is history, not a correction of today."""
    await _signup(client)
    account = await _create_account(client)

    await _enter_balance(client, account["id"], 500_00, as_of="2026-07-10T00:00:00Z")
    await _enter_balance(client, account["id"], 100_00, as_of="2026-01-01T00:00:00Z")

    balance = (await client.get(f"{ACCOUNTS}/{account['id']}")).json()["balance"]
    assert balance["amount_minor"] == 500_00

    history = (await client.get(f"{ACCOUNTS}/{account['id']}/balance-entries")).json()
    assert len(history["items"]) == 2


async def test_balance_history_pages_on_the_m3_convention(client) -> None:
    await _signup(client)
    account = await _create_account(client)
    for month in (1, 2, 3):
        await _enter_balance(
            client, account["id"], month * 100, as_of=f"2026-0{month}-01T00:00:00Z"
        )

    url = f"{ACCOUNTS}/{account['id']}/balance-entries"
    first = (await client.get(url, params={"limit": 2})).json()
    assert set(first) == {"items", "next_cursor"}
    assert len(first["items"]) == 2
    rest = (await client.get(url, params={"limit": 2, "cursor": first["next_cursor"]})).json()
    assert len(rest["items"]) == 1
    assert rest["next_cursor"] is None
    assert (await client.get(url, params={"cursor": "junk"})).status_code == 400


async def test_amounts_are_integers_never_floats(client) -> None:
    """Money is integer minor units (CONTEXT.md); a fractional amount is
    invalid, never rounded (I-1)."""
    await _signup(client)
    account = await _create_account(client)
    headers = await _csrf(client)
    # JSON booleans are absent deliberately: Litestar's decode layer coerces
    # true -> 1 before pydantic runs, and the app-wide strict-mode fix breaks
    # every string-borne type (enums, datetimes) — a framework boundary
    # accepted after scratch-testing, not an oversight.
    for bad in (100.5, "100.5", "1e2", None):
        response = await client.post(
            f"{ACCOUNTS}/{account['id']}/balance-entries",
            json={"amount_minor": bad},
            headers=headers,
        )
        assert response.status_code == 400, f"amount_minor={bad!r} must be rejected"


# --- Tenancy (AGENTS I-2; the non-confirmation discipline) ---------------------------


async def test_another_users_accounts_answer_404_never_403(client) -> None:
    await _signup(client)
    account = await _create_account(client)
    await _enter_balance(client, account["id"], 100_00)
    await _logout(client)

    await _signup(client, email="other@example.com")
    assert (await client.get(ACCOUNTS)).json()["items"] == []

    headers = await _csrf(client)
    a_id = account["id"]
    assert (await client.get(f"{ACCOUNTS}/{a_id}")).status_code == 404
    assert (
        await client.patch(f"{ACCOUNTS}/{a_id}", json={"label": "mine now"}, headers=headers)
    ).status_code == 404
    assert (await client.post(f"{ACCOUNTS}/{a_id}/archive", headers=headers)).status_code == 404
    assert (await client.get(f"{ACCOUNTS}/{a_id}/balance-entries")).status_code == 404
    assert (
        await client.post(
            f"{ACCOUNTS}/{a_id}/balance-entries", json={"amount_minor": 1}, headers=headers
        )
    ).status_code == 404

    # And nothing was touched: the victim's data is exactly as they left it.
    assert len(await BalanceEntry.all()) == 1


# --- The scope guard's first real success path (story 12; retires the probe) ---------


async def test_read_pats_are_refused_and_write_pats_succeed_on_real_writes(client) -> None:
    await _signup(client)
    read_token = await _mint_pat(client, ["read"])
    write_token = await _mint_pat(client, ["write"])
    client.cookies.clear()

    payload = {"kind": "asset", "label": "House", "currency": "USD"}
    refused = await client.post(ACCOUNTS, json=payload, headers=_bearer(read_token))
    assert refused.status_code == 403

    created = await client.post(ACCOUNTS, json=payload, headers=_bearer(write_token))
    assert created.status_code == 201
    account_id = created.json()["id"]

    entry = await client.post(
        f"{ACCOUNTS}/{account_id}/balance-entries",
        json={"amount_minor": 350_000_00},
        headers=_bearer(write_token),
    )
    assert entry.status_code == 201
    refused_entry = await client.post(
        f"{ACCOUNTS}/{account_id}/balance-entries",
        json={"amount_minor": 1},
        headers=_bearer(read_token),
    )
    assert refused_entry.status_code == 403

    # Read scope still reads everything.
    listed = await client.get(ACCOUNTS, headers=_bearer(read_token))
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1


async def test_either_credential_sees_the_same_accounts(client) -> None:
    """Cookie and bearer resolve to the same ledger (story 12): an account
    created by one credential is visible to the other."""
    await _signup(client)
    token = await _mint_pat(client, ["write"])
    via_cookie = await _create_account(client, label="From cookie")

    client.cookies.clear()
    listed = (await client.get(ACCOUNTS, headers=_bearer(token))).json()["items"]
    assert [item["id"] for item in listed] == [via_cookie["id"]]


# --- OpenAPI (story 12) ----------------------------------------------------------------


async def test_openapi_describes_the_account_routes(client) -> None:
    schema = (await client.get(SCHEMA_JSON)).json()
    for path in (
        "/api/v1/accounts",
        "/api/v1/accounts/{account_id}",
        "/api/v1/accounts/{account_id}/archive",
        "/api/v1/accounts/{account_id}/balance-entries",
    ):
        assert path in schema["paths"], f"{path} missing from the OpenAPI document"
