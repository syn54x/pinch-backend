"""The flywheel at the HTTP seam (M5 CP3, #21): commit -> job -> proposals
with provenance; auto-file; undo retraction; the commit request itself
never classifies. Data flows through the real M4 import seam."""

import pytest

TX = "/api/v1/transactions"
IMPORTS = "/api/v1/imports"
CATEGORIES = "/api/v1/categories"
RULES = "/api/v1/rules"
LOG = "/api/v1/correction-log"
PASSWORD = "correct horse battery staple"
MAPPING = {
    "delimiter": ",",
    "has_header": True,
    "date_column": 0,
    "date_format": "%Y-%m-%d",
    "amount_column": 1,
    "description_columns": [2],
}
CSV_ROWS = [
    ("2026-07-01", "-5.00", "STARBUCKS 123"),
    ("2026-07-02", "-42.00", "MYSTERY CO"),
]


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


async def _commit_csv(client, account_id, *, rows=CSV_ROWS, auto_file=False) -> str:
    body = "date,amount,description\n" + "\n".join(f"{d},{a},{m}" for d, a, m in rows) + "\n"
    up = await client.post(
        IMPORTS,
        files={"file": ("bank.csv", body, "text/csv")},
        data={"account_id": account_id},
        headers=await _csrf(client),
    )
    assert up.status_code == 201, up.text
    import_id = up.json()["id"]
    confirmed = await client.post(
        f"{IMPORTS}/{import_id}/mapping", json=MAPPING, headers=await _csrf(client)
    )
    assert confirmed.status_code == 200, confirmed.text
    committed = await client.post(
        f"{IMPORTS}/{import_id}/commit",
        json={"auto_file": auto_file},
        headers=await _csrf(client),
    )
    assert committed.status_code == 200, committed.text
    return import_id


async def _category(client, name: str) -> str:
    r = await client.post(CATEGORIES, json={"name": name}, headers=await _csrf(client))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _rule(client, *, contains: str, category_id: str) -> None:
    r = await client.post(
        RULES,
        json={
            "condition": {"payee": {"op": "contains", "value": contains}},
            "action_category_id": category_id,
        },
        headers=await _csrf(client),
    )
    assert r.status_code == 201, r.text


async def _transactions(client) -> list[dict]:
    r = await client.get(TX)
    assert r.status_code == 200
    return r.json()["items"]


async def test_commit_enqueues_exactly_one_classification_job(client, job_connector) -> None:
    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(client, account_id)
    jobs = list(job_connector.jobs.values())
    assert len(jobs) == 1
    assert jobs[0]["task_name"] == "classification.classify_ledger"
    assert jobs[0]["args"]["auto_file_import_id"] is None
    assert jobs[0]["lock"] == f"ledger:{jobs[0]['args']['ledger_id']}"


async def test_auto_file_commit_carries_the_import_id(client, job_connector) -> None:
    await _signup(client)
    account_id = await _account(client)
    import_id = await _commit_csv(client, account_id, auto_file=True)
    jobs = list(job_connector.jobs.values())
    assert len(jobs) == 1
    assert jobs[0]["args"]["auto_file_import_id"] == import_id


@pytest.mark.xfail(reason="TransactionOut.proposal lands in Task 8", strict=True)
async def test_commit_defers_and_the_job_writes_proposals(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee Z")
    await _rule(client, contains="starbucks", category_id=coffee)

    await _commit_csv(client, account_id)
    for txn in await _transactions(client):
        assert txn["proposal"] is None  # the commit request never classifies

    await run_jobs()
    by_payee = {t["description_normalized"]: t for t in await _transactions(client)}
    ruled = by_payee["starbucks 123"]["proposal"]
    assert ruled["provenance"] == "rule"
    assert ruled["category"]["name"] == "Coffee Z"
    unknown = by_payee["mystery co"]["proposal"]
    assert unknown["provenance"] == "none"
    assert unknown["category"] is None


@pytest.mark.xfail(reason="correction-log endpoint lands in Task 9", strict=True)
async def test_auto_file_lands_reviewed_and_logged_auto(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee Z")
    await _rule(client, contains="starbucks", category_id=coffee)
    await _commit_csv(client, account_id, auto_file=True)
    await run_jobs()

    by_payee = {t["description_normalized"]: t for t in await _transactions(client)}
    filed = by_payee["starbucks 123"]
    assert filed["reviewed_at"] is not None
    assert filed["category"]["name"] == "Coffee Z"
    assert filed["proposal"] is None  # consumed
    unknown = by_payee["mystery co"]
    assert unknown["reviewed_at"] is not None  # the empty proposal auto-files too
    assert unknown["category"] is None

    entries = (await client.get(LOG)).json()["items"]
    assert entries and all(e["actor"] == "auto" for e in entries)


@pytest.mark.xfail(reason="TransactionOut.proposal lands in Task 8", strict=True)
async def test_keyless_empty_taxonomy_sweeps_clean(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    # Empty the seeded taxonomy: children first, then roots.
    cats = (await client.get(f"{CATEGORIES}?limit=100")).json()["items"]
    for c in [c for c in cats if c["parent_id"]] + [c for c in cats if not c["parent_id"]]:
        r = await client.request(
            "DELETE",
            f"{CATEGORIES}/{c['id']}",
            json={"reassign_to": None},
            headers=await _csrf(client),
        )
        assert r.status_code == 204, r.text

    await _commit_csv(client, account_id)
    await run_jobs()
    for txn in await _transactions(client):
        assert txn["proposal"]["provenance"] == "none"

    # Re-defer: the sweep does not reprocess (empty proposals are done-markers).
    await _commit_csv(client, account_id, rows=[("2026-07-03", "-1.00", "ANOTHER")])
    await run_jobs()
    payees = {t["description_normalized"] for t in await _transactions(client)}
    assert "another" in payees
