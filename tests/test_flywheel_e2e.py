"""The M5 thesis, end to end at the HTTP seam (#22): import -> propose ->
correct -> history learns -> third consistent filing -> proposed rule ->
accept -> rule wins precedence. One test, the whole flywheel."""

TX = "/api/v1/transactions"
RULES = "/api/v1/rules"

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


async def _review(client, txn_id: str, body: dict | None = None):
    return await client.post(f"{TX}/{txn_id}/review", json=body or {}, headers=await _csrf(client))


async def _inbox_txn(client) -> dict:
    items = [t for t in await _transactions(client) if t["reviewed_at"] is None]
    assert items, "expected an unreviewed transaction"
    return items[0]


async def test_the_flywheel(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")

    async def arrive(day: str, amount: str) -> dict:
        await _commit_csv(client, account_id, rows=[(f"2026-07-{day}", amount, "STARBUCKS 123")])
        await run_jobs()
        return await _inbox_txn(client)

    # 1. First arrival: every stage abstains — the empty proposal.
    txn = await arrive("01", "-5.00")
    assert txn["proposal"]["provenance"] == "none"
    assert txn["proposal"]["category"] is None
    # 2. The user corrects. One vote; no rule yet.
    r = await _review(client, txn["id"], {"category_id": coffee})
    assert r.json()["result"] == "corrected"
    assert r.json()["proposed_rule"] is None
    # 3. Second arrival: history learned the correction.
    txn = await arrive("02", "-6.00")
    assert txn["proposal"]["provenance"] == "history"
    assert txn["proposal"]["category"]["id"] == coffee
    r = await _review(client, txn["id"])
    assert r.json()["result"] == "accepted"
    assert r.json()["proposed_rule"] is None  # two votes
    # 4. Third consistent filing: the rule is proposed — consent asked.
    txn = await arrive("03", "-7.00")
    r = await _review(client, txn["id"])
    rule = r.json()["proposed_rule"]
    assert rule is not None
    assert rule["status"] == "proposed"
    assert rule["condition"]["payee"] == {"op": "equals", "value": "starbucks 123"}
    assert rule["action_category"]["id"] == coffee
    # 5. Proposed is not law: the next arrival is still history-proposed.
    txn = await arrive("04", "-8.00")
    assert txn["proposal"]["provenance"] == "history"
    r = await _review(client, txn["id"])
    assert r.json()["proposed_rule"] is None  # the covering rule blocks re-mint
    # 6. The user consents: one status flip.
    accepted = await client.patch(
        f"{RULES}/{rule['id']}", json={"status": "active"}, headers=await _csrf(client)
    )
    assert accepted.status_code == 200, accepted.text
    # 7. The rule wins precedence over history.
    txn = await arrive("05", "-9.00")
    assert txn["proposal"]["provenance"] == "rule"
    assert txn["proposal"]["category"]["id"] == coffee
