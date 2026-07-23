"""M8 CP5 seam: the ledger stats endpoint (issue #51).

One poll target for onboarding step 3 and the Dashboard's trust split:
classification progress, the unreviewed provenance breakdown, the live
recurring-found count, and sync recency.
"""

STATS = "/api/v1/ledgers/current/stats"

PASSWORD = "correct horse battery staple"


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


async def _account(client) -> str:
    response = await client.post(
        "/api/v1/accounts",
        json={"kind": "depository", "label": "Checking", "currency": "USD"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _txn(client, account_id: str, date_: str, amount_minor: int, description: str) -> str:
    response = await client.post(
        "/api/v1/transactions",
        json={
            "account_id": account_id,
            "date": date_,
            "amount_minor": amount_minor,
            "description": description,
        },
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _stats(client) -> dict:
    response = await client.get(STATS)
    assert response.status_code == 200, response.text
    return response.json()


async def test_classification_progress_and_provenance_split(client, run_jobs):
    """A rule-matched, a history-matched, and an abstained transaction:
    the wizard's processed/total plus the Dashboard's trust breakdown."""
    await _signup(client)
    account = await _account(client)
    category = await client.post(
        "/api/v1/categories", json={"name": "Groceries"}, headers=await _csrf(client)
    )
    rule = await client.post(
        "/api/v1/rules",
        json={
            "condition": {"payee": {"op": "contains", "value": "costco"}},
            "action_category_id": category.json()["id"],
        },
        headers=await _csrf(client),
    )
    assert rule.status_code == 201, rule.text

    # History precedent: a reviewed CAFE transaction with the category.
    precedent = await _txn(client, account, "2026-07-01", -1_000, "CAFE")
    review = await client.post(
        f"/api/v1/transactions/{precedent}/review",
        json={"category_id": category.json()["id"]},
        headers=await _csrf(client),
    )
    assert review.status_code == 200, review.text

    await _txn(client, account, "2026-07-10", -2_000, "COSTCO")  # rule
    await _txn(client, account, "2026-07-11", -3_000, "CAFE")  # history
    await _txn(client, account, "2026-07-12", -4_000, "MYSTERY")  # none
    await run_jobs()

    body = await _stats(client)
    assert body["transactions_total"] == 4
    assert body["unreviewed"] == 3
    assert body["classified"] == 4  # 1 reviewed + 3 carrying proposals
    split = body["unreviewed_by_provenance"]
    assert split["rule"] == 1
    assert split["history"] == 1
    assert split["none"] == 1
    assert split["ai"] == 0


async def test_recurring_found_counts_active_series(client, run_jobs):
    await _signup(client)
    account = await _account(client)
    for month in ["2026-04", "2026-05", "2026-06"]:
        await _txn(client, account, f"{month}-12", -999, "SPOTIFY")
    await run_jobs()

    body = await _stats(client)
    assert body["recurring_found"] == 1


async def test_empty_ledger_and_no_sync(client):
    await _signup(client)
    body = await _stats(client)
    assert body["transactions_total"] == 0
    assert body["classified"] == 0
    assert body["unreviewed"] == 0
    assert body["recurring_found"] == 0
    assert body["last_synced_at"] is None


async def test_requires_authentication(client):
    response = await client.get(STATS)
    assert response.status_code == 401
