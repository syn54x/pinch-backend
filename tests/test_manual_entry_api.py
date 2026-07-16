"""POST /api/v1/transactions — manual entry (M5 CP4, #22): manual accounts
only; without category an ordinary incoming transaction (sweep, inbox);
with category/tags reviewed at birth (empty-proposal log entry, actor=user).
Fingerprint via the M4 recipe so later CSV overlaps flag."""

TX = "/api/v1/transactions"
LOG = "/api/v1/correction-log"
RULES = "/api/v1/rules"
PATS = "/api/v1/auth/pats"

PASSWORD = "correct horse battery staple"
MAPPING = {
    "delimiter": ",",
    "has_header": True,
    "date_column": 0,
    "date_format": "%Y-%m-%d",
    "amount_column": 1,
    "description_columns": [2],
}


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email: str = "taylor@example.com") -> None:
    r = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "display_name": "Taylor"},
        headers=await _csrf(client),
    )
    assert r.status_code == 201, r.text


async def _account(client) -> str:
    r = await client.post(
        "/api/v1/accounts",
        json={"kind": "depository", "label": "Checking", "currency": "USD"},
        headers=await _csrf(client),
    )
    return r.json()["id"]


async def _commit_csv(client, account_id, *, rows, auto_file=False) -> str:
    body = "date,amount,description\n" + "\n".join(f"{d},{a},{m}" for d, a, m in rows) + "\n"
    up = await client.post(
        "/api/v1/imports",
        files={"file": ("bank.csv", body, "text/csv")},
        data={"account_id": account_id},
        headers=await _csrf(client),
    )
    assert up.status_code == 201, up.text
    import_id = up.json()["id"]
    confirmed = await client.post(
        f"/api/v1/imports/{import_id}/mapping", json=MAPPING, headers=await _csrf(client)
    )
    assert confirmed.status_code == 200, confirmed.text
    committed = await client.post(
        f"/api/v1/imports/{import_id}/commit",
        json={"auto_file": auto_file},
        headers=await _csrf(client),
    )
    assert committed.status_code == 200, committed.text
    return import_id


async def _category(client, name: str) -> str:
    r = await client.post("/api/v1/categories", json={"name": name}, headers=await _csrf(client))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _transactions(client) -> list[dict]:
    r = await client.get(TX)
    assert r.status_code == 200
    return r.json()["items"]


async def _mint(
    client, name: str = "ci-script", scopes: list[str] | None = None
) -> tuple[dict, str]:
    response = await client.post(
        PATS,
        json={"name": name, "scopes": scopes or ["read", "write"]},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body, body["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _manual(client, account_id: str, body: dict | None = None):
    payload = {
        "account_id": account_id,
        "date": "2026-07-10",
        "amount_minor": -1250,
        "description": "Farmers Market",
    } | (body or {})
    return await client.post(TX, json=payload, headers=await _csrf(client))


async def test_uncategorized_manual_entry_is_ordinary_incoming(client, run_jobs, job_connector):
    await _signup(client)
    account_id = await _account(client)
    before = len(job_connector.jobs)
    r = await _manual(client, account_id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["reviewed_at"] is None
    assert body["currency"] == "USD"  # the account's, never the payload's
    assert len(job_connector.jobs) == before + 1  # manual creation enqueues
    await run_jobs()
    txn = (await client.get(f"{TX}/{body['id']}")).json()
    assert txn["proposal"] is not None  # the sweep classified it
    assert (await client.get(LOG, params={"transaction_id": body["id"]})).json()["items"] == []


async def test_categorized_manual_entry_is_reviewed_at_birth(client, job_connector):
    await _signup(client)
    account_id = await _account(client)
    groceries = await _category(client, "Groceries")
    before = len(job_connector.jobs)
    r = await _manual(client, account_id, {"category_id": groceries, "tags": ["market"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["reviewed_at"] is not None
    assert body["category"]["id"] == groceries
    assert [t["name"] for t in body["tags"]] == ["market"]
    assert body["proposal"] is None
    assert len(job_connector.jobs) == before  # born reviewed: no sweep needed
    entries = (await client.get(LOG, params={"transaction_id": body["id"]})).json()["items"]
    assert len(entries) == 1
    assert entries[0]["actor"] == "user"
    assert entries[0]["proposal_provenance"] == "none"  # the pipeline never ran
    assert entries[0]["decision_category_id"] == groceries


async def test_tags_alone_review_at_birth_but_annotations_do_not(client):
    await _signup(client)
    account_id = await _account(client)
    tagged = await _manual(client, account_id, {"tags": ["cash"]})
    assert tagged.json()["reviewed_at"] is not None
    annotated = await _manual(
        client,
        account_id,
        {"date": "2026-07-11", "display_name": "Market", "notes": "cash run"},
    )
    body = annotated.json()
    assert body["reviewed_at"] is None  # annotations are not decisions
    assert body["display_name"] == "Market"
    assert body["notes"] == "cash run"


async def test_connected_account_answers_409(client, db):
    import uuid as _uuid

    from pinch_backend.models import Account, Connection, Ledger

    await _signup(client)
    account_id = await _account(client)
    account = await Account.get(_uuid.UUID(account_id))
    ledger = await Ledger.get(account.ledger_id)  # ty: ignore[unresolved-attribute]
    connection = await Connection.create(ledger=ledger, provider_item_id="item-1")
    connected = await Account.create(
        ledger=ledger, kind=account.kind, label="Linked", currency="USD", connection=connection
    )
    r = await _manual(client, str(connected.id))
    assert r.status_code == 409


async def test_amount_beyond_the_int4_column_is_a_400_not_a_500(client):
    """amount_minor is bounded to the integer column width (PR review
    finding 11): an out-of-range amount is a client error, never a database
    error surfacing as a 500."""
    await _signup(client)
    account_id = await _account(client)
    r = await _manual(client, account_id, {"amount_minor": 2**40})
    assert r.status_code == 400
    r2 = await _manual(client, account_id, {"amount_minor": -(2**40)})
    assert r2.status_code == 400


async def test_manual_entry_rejects_unknown_keys(client):
    """extra="forbid" (PR review finding 16): the payload's currency is
    never accepted (the account's always wins), so sending one is a 400."""
    await _signup(client)
    account_id = await _account(client)
    r = await _manual(client, account_id, {"currency": "EUR"})
    assert r.status_code == 400


async def test_account_tenancy_and_category_404(client):
    import uuid as _uuid

    await _signup(client)
    account_id = await _account(client)
    assert (await _manual(client, str(_uuid.uuid7()))).status_code == 404
    r = await _manual(client, account_id, {"category_id": str(_uuid.uuid7())})
    assert r.status_code == 404


async def test_later_csv_overlap_flags_against_the_hand_entered_row(client, run_jobs):
    await _signup(client)
    account_id = await _account(client)
    r = await _manual(client, account_id)  # 2026-07-10, -1250, Farmers Market
    assert r.status_code == 201
    # The same movement arrives in a CSV: the fingerprint collides, the row
    # is flagged, and default commit skips it (M4 semantics).
    await _commit_csv(client, account_id, rows=[("2026-07-10", "-12.50", "Farmers Market")])
    await run_jobs()
    matching = [t for t in await _transactions(client) if t["description_raw"] == "Farmers Market"]
    assert len(matching) == 1  # the duplicate was skipped at commit


async def test_three_manual_filings_promote(client):
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    for day in ("01", "02", "03"):
        r = await _manual(
            client,
            account_id,
            {"date": f"2026-07-{day}", "description": "Blue Bottle", "category_id": coffee},
        )
        assert r.status_code == 201
    proposed = (await client.get(RULES, params={"status": "proposed"})).json()["items"]
    assert len(proposed) == 1
    assert proposed[0]["condition"]["payee"] == {"op": "equals", "value": "blue bottle"}


async def test_read_scope_pat_is_refused_by_manual_entry_with_403(client):
    await _signup(client)
    account_id = await _account(client)
    _, token = await _mint(client, scopes=["read"])
    client.cookies.clear()
    r = await client.post(
        TX,
        json={
            "account_id": account_id,
            "date": "2026-07-10",
            "amount_minor": -1250,
            "description": "Farmers Market",
        },
        headers=_bearer(token),
    )
    assert r.status_code == 403
