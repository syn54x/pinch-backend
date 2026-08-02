"""/api/v1/tags over the public seam (M5 CP1, #19)."""

TAGS = "/api/v1/tags"
PASSWORD = "correct horse battery staple"


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


async def test_create_and_list_tag(client) -> None:
    await _signup(client)
    r = await client.post(TAGS, json={"name": "Vacation-2026"}, headers=await _csrf(client))
    assert r.status_code == 201, r.text
    listing = await client.get(TAGS)
    assert "Vacation-2026" in {t["name"] for t in listing.json()["items"]}


async def test_whitespace_only_name_is_rejected(client) -> None:
    await _signup(client)
    r = await client.post(TAGS, json={"name": " "}, headers=await _csrf(client))
    assert r.status_code == 400


async def test_casefold_collision_is_rejected(client) -> None:
    await _signup(client)
    await client.post(TAGS, json={"name": "Vacation"}, headers=await _csrf(client))
    r = await client.post(TAGS, json={"name": "vacation"}, headers=await _csrf(client))
    assert r.status_code == 409


async def test_delete_removes_the_tag(client) -> None:
    await _signup(client)
    created = await client.post(TAGS, json={"name": "temp"}, headers=await _csrf(client))
    tag_id = created.json()["id"]
    r = await client.request("DELETE", f"{TAGS}/{tag_id}", headers=await _csrf(client))
    assert r.status_code == 204
    listing = await client.get(TAGS)
    assert tag_id not in {t["id"] for t in listing.json()["items"]}


async def _account(client) -> str:
    r = await client.post(
        "/api/v1/accounts",
        json={"kind": "depository", "label": "Checking", "currency": "USD"},
        headers=await _csrf(client),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _txn(client, account_id: str, amount_minor: int, tags: list[str]) -> str:
    r = await client.post(
        "/api/v1/transactions",
        json={
            "account_id": account_id,
            "date": "2026-07-15",
            "amount_minor": amount_minor,
            "description": f"seed {amount_minor}",
            "tags": tags,
        },
        headers=await _csrf(client),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_list_carries_per_tag_aggregates(client) -> None:
    """Each tag totals its transactions: count, net, and the unsettled slice
    (F4 Enabler A, #66 — the expense-report numbers)."""
    from pinch_backend.models import Transaction

    await _signup(client)
    account = await _account(client)
    await _txn(client, account, -21480, ["trip"])
    pending_id = await _txn(client, account, -6210, ["trip"])
    await _txn(client, account, 5000, ["trip"])
    await _txn(client, account, -999, ["other"])
    await client.post(TAGS, json={"name": "unused"}, headers=await _csrf(client))

    row = await Transaction.get(pending_id)
    row.pending = True
    await row.save()

    resp = await client.get(TAGS)
    assert resp.status_code == 200, resp.text
    listing = resp.json()["items"]
    by_name = {t["name"]: t for t in listing}
    assert by_name["trip"]["transaction_count"] == 3
    assert by_name["trip"]["net_minor"] == -22690
    assert by_name["trip"]["pending_minor"] == -6210
    assert by_name["other"]["transaction_count"] == 1
    assert by_name["other"]["net_minor"] == -999
    assert by_name["other"]["pending_minor"] == 0
    assert by_name["unused"]["transaction_count"] == 0
    assert by_name["unused"]["net_minor"] == 0
    assert by_name["unused"]["pending_minor"] == 0


async def test_rename_updates_everywhere_and_keeps_links(client) -> None:
    """Rename fixes the label everywhere at once: id stable, links intact,
    casefold collisions refused (F4 Enabler A, #66)."""
    await _signup(client)
    account = await _account(client)
    await _txn(client, account, -5000, ["vacaton"])
    await client.post(TAGS, json={"name": "taken"}, headers=await _csrf(client))

    listing = (await client.get(TAGS)).json()["items"]
    tag = next(t for t in listing if t["name"] == "vacaton")

    r = await client.patch(
        f"{TAGS}/{tag['id']}", json={"name": "Vacation"}, headers=await _csrf(client)
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Vacation"
    assert r.json()["id"] == tag["id"]
    assert r.json()["transaction_count"] == 1

    txns = (await client.get("/api/v1/transactions?tag=Vacation")).json()["items"]
    assert len(txns) == 1

    r = await client.patch(
        f"{TAGS}/{tag['id']}", json={"name": "TAKEN"}, headers=await _csrf(client)
    )
    assert r.status_code == 409

    r = await client.patch(f"{TAGS}/{tag['id']}", json={"name": "  "}, headers=await _csrf(client))
    assert r.status_code == 400

    r = await client.patch(
        f"{TAGS}/{tag['id']}", json={"name": "VACATION"}, headers=await _csrf(client)
    )
    assert r.status_code == 200, "re-casing yourself is not a collision"
    assert r.json()["name"] == "VACATION"
