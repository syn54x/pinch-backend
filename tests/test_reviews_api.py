"""POST /transactions/{id}/review (M5 CP4, #22): the body carries the FINAL
user data; the server diffs against the proposal to record accepted-vs-
corrected; empty body accepts as-is. Wraps CP3's consume."""

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


async def _review(client, txn_id: str, body: dict | None = None):
    return await client.post(f"{TX}/{txn_id}/review", json=body or {}, headers=await _csrf(client))


async def _inbox_txn(client) -> dict:
    items = [t for t in await _transactions(client) if t["reviewed_at"] is None]
    assert items, "expected an unreviewed transaction"
    return items[0]


async def test_empty_body_accepts_the_proposal_as_is(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    # History seed: review the first arrival with a correction...
    await _commit_csv(client, account_id, rows=[("2026-07-01", "-5.00", "STARBUCKS 123")])
    await run_jobs()
    first = await _inbox_txn(client)
    r = await _review(client, first["id"], {"category_id": coffee})
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "corrected"  # empty proposal vs a category
    # ...so the second arrival is history-proposed and accepting is a no-diff.
    await _commit_csv(client, account_id, rows=[("2026-07-02", "-6.00", "STARBUCKS 123")])
    await run_jobs()
    second = await _inbox_txn(client)
    assert second["proposal"]["provenance"] == "history"
    r2 = await _review(client, second["id"])
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["result"] == "accepted"
    assert body["transaction"]["reviewed_at"] is not None
    assert body["transaction"]["category"]["id"] == coffee
    assert body["transaction"]["proposal"] is None  # consumed
    assert body["proposed_rule"] is None  # only two votes


async def test_field_present_merge_body_tags_keep_proposal_category(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    await _commit_csv(client, account_id, rows=[("2026-07-01", "-5.00", "STARBUCKS 123")])
    await run_jobs()
    await _review(client, (await _inbox_txn(client))["id"], {"category_id": coffee})
    await _commit_csv(client, account_id, rows=[("2026-07-02", "-6.00", "STARBUCKS 123")])
    await run_jobs()
    txn = await _inbox_txn(client)
    r = await _review(client, txn["id"], {"tags": ["morning"]})
    body = r.json()
    assert body["result"] == "corrected"  # tags diverge from the (tagless) proposal
    assert body["transaction"]["category"]["id"] == coffee  # merged from proposal
    assert [t["name"] for t in body["transaction"]["tags"]] == ["morning"]


async def test_tags_are_casefold_deduped_and_logged_as_applied(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(client, account_id)
    await run_jobs()
    txn = await _inbox_txn(client)
    r = await _review(client, txn["id"], {"tags": ["Coffee", "coffee ", "MORNING"]})
    assert r.status_code == 200, r.text
    assert [t["name"] for t in r.json()["transaction"]["tags"]] == ["Coffee", "MORNING"]
    entries = (await client.get(LOG, params={"transaction_id": txn["id"]})).json()["items"]
    assert entries[0]["decision_tags"] == ["Coffee", "MORNING"]


async def test_review_before_the_sweep_snapshots_provenance_none(client) -> None:
    """A missing proposal is legal (PRD): the pipeline never ran."""
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    await _commit_csv(client, account_id, rows=[("2026-07-01", "-5.00", "STARBUCKS 123")])
    # No run_jobs: the proposal does not exist yet.
    txn = await _inbox_txn(client)
    assert txn["proposal"] is None
    r = await _review(client, txn["id"], {"category_id": coffee})
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "corrected"
    entries = (await client.get(LOG, params={"transaction_id": txn["id"]})).json()["items"]
    assert entries[0]["proposal_provenance"] == "none"
    assert entries[0]["decision_category_id"] == coffee


async def test_display_name_body_vs_proposal(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(client, account_id)
    await run_jobs()
    txn = await _inbox_txn(client)
    r = await _review(client, txn["id"], {"display_name": "Starbucks"})
    body = r.json()
    assert body["result"] == "corrected"
    assert body["transaction"]["display_name"] == "Starbucks"


async def test_already_reviewed_answers_409(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(client, account_id)
    await run_jobs()
    txn = await _inbox_txn(client)
    assert (await _review(client, txn["id"])).status_code == 200
    assert (await _review(client, txn["id"])).status_code == 409


async def test_unknown_category_404s_and_reviews_nothing(client, run_jobs) -> None:
    import uuid as _uuid

    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(client, account_id)
    await run_jobs()
    txn = await _inbox_txn(client)
    r = await _review(client, txn["id"], {"category_id": str(_uuid.uuid7())})
    assert r.status_code == 404
    assert (await client.get(f"{TX}/{txn['id']}")).json()["reviewed_at"] is None


async def test_tenancy_404(client, run_jobs) -> None:
    await _signup(client, email="a@example.com")
    account_id = await _account(client)
    await _commit_csv(client, account_id)
    await run_jobs()
    txn = await _inbox_txn(client)
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))
    await _signup(client, email="b@example.com")
    assert (await _review(client, txn["id"])).status_code == 404


async def test_third_consistent_review_proposes_a_rule(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    rule = None
    for i, day in enumerate(("01", "02", "03")):
        await _commit_csv(client, account_id, rows=[(f"2026-07-{day}", "-5.00", "STARBUCKS 123")])
        await run_jobs()
        txn = await _inbox_txn(client)
        r = await _review(client, txn["id"], {"category_id": coffee})
        assert r.status_code == 200, r.text
        rule = r.json()["proposed_rule"]
        if i < 2:
            assert rule is None
    assert rule is not None
    assert rule["status"] == "proposed"
    assert rule["condition"]["payee"] == {"op": "equals", "value": "starbucks 123"}
    assert rule["action_category"]["id"] == coffee
    listed = (await client.get(RULES, params={"status": "proposed"})).json()["items"]
    assert [x["id"] for x in listed] == [rule["id"]]


async def test_read_scope_pat_is_refused_by_review_with_403(client) -> None:
    import uuid as _uuid

    await _signup(client)
    _, token = await _mint(client, scopes=["read"])
    client.cookies.clear()
    r = await client.post(
        f"{TX}/{_uuid.uuid7()}/review",
        json={},
        headers=_bearer(token),
    )
    assert r.status_code == 403


async def _batch(client, ids: list[str]):
    return await client.post(f"{TX}/review", json={"ids": ids}, headers=await _csrf(client))


async def test_batch_counts_are_honest_and_idempotent(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(
        client,
        account_id,
        rows=[
            ("2026-07-01", "-5.00", "STARBUCKS 123"),
            ("2026-07-02", "-42.00", "MYSTERY CO"),
            ("2026-07-03", "-7.00", "PEETS"),
        ],
    )
    await run_jobs()
    ids = [t["id"] for t in await _transactions(client)]
    await _review(client, ids[0])  # one already reviewed
    r = await _batch(client, ids)
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 2, "skipped": 1, "proposed_rules": []}
    again = await _batch(client, ids)
    assert again.json() == {"accepted": 0, "skipped": 3, "proposed_rules": []}
    assert all(t["reviewed_at"] is not None for t in await _transactions(client))


async def test_batch_unknown_id_404s_and_consumes_nothing(client, run_jobs) -> None:
    import uuid as _uuid

    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(client, account_id)
    await run_jobs()
    ids = [t["id"] for t in await _transactions(client)]
    ghost = str(_uuid.uuid7())
    r = await _batch(client, [*ids, ghost])
    assert r.status_code == 404
    assert ghost in str(r.json())
    assert all(t["reviewed_at"] is None for t in await _transactions(client))


async def test_batch_duplicate_ids_count_once(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(client, account_id, rows=[("2026-07-01", "-5.00", "STARBUCKS 123")])
    await run_jobs()
    (txn,) = await _transactions(client)
    r = await _batch(client, [txn["id"], txn["id"]])
    assert r.json()["accepted"] == 1
    assert r.json()["skipped"] == 0


async def test_batch_cap_1000(client) -> None:
    import uuid as _uuid

    await _signup(client)
    r = await _batch(client, [str(_uuid.uuid7()) for _ in range(1001)])
    assert r.status_code == 400


async def test_batch_accepting_a_third_history_proposal_promotes(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    await _commit_csv(client, account_id, rows=[("2026-07-01", "-5.00", "STARBUCKS 123")])
    await run_jobs()
    await _review(client, (await _inbox_txn(client))["id"], {"category_id": coffee})
    await _commit_csv(
        client,
        account_id,
        rows=[("2026-07-02", "-6.00", "STARBUCKS 123"), ("2026-07-03", "-7.00", "STARBUCKS 123")],
    )
    await run_jobs()
    pending = [t["id"] for t in await _transactions(client) if t["reviewed_at"] is None]
    r = await _batch(client, pending)
    body = r.json()
    assert body["accepted"] == 2
    assert len(body["proposed_rules"]) == 1  # one check per distinct payee
    assert body["proposed_rules"][0]["condition"]["payee"]["value"] == "starbucks 123"
