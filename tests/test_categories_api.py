"""/api/v1/categories over the public seam (M5 CP1, #19)."""

CATEGORIES = "/api/v1/categories"
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


async def _create(client, name: str, parent_id: str | None = None):
    r = await client.post(
        CATEGORIES,
        json={"name": name, "parent_id": parent_id},
        headers=await _csrf(client),
    )
    return r


async def test_signup_seeds_a_listable_taxonomy(client) -> None:
    await _signup(client)
    r = await client.get(f"{CATEGORIES}?limit=100")
    assert r.status_code == 200
    names = {c["name"] for c in r.json()["items"]}
    assert {"Food & Drink", "Groceries", "Income"} <= names


async def test_depth_three_is_rejected(client) -> None:
    await _signup(client)
    food = (await _create(client, "MyFood")).json()
    sub = (await _create(client, "MySub", parent_id=food["id"])).json()
    r = await _create(client, "TooDeep", parent_id=sub["id"])
    assert r.status_code == 400


async def test_reparent_into_a_cycle_is_rejected(client) -> None:
    await _signup(client)
    a = (await _create(client, "A")).json()
    b = (await _create(client, "B", parent_id=a["id"])).json()
    r = await client.patch(
        f"{CATEGORIES}/{a['id']}",
        json={"parent_id": b["id"], "reparent": True},
        headers=await _csrf(client),
    )
    assert r.status_code == 400


async def test_delete_requires_a_disposition_and_reassigns(client) -> None:
    await _signup(client)
    src = (await _create(client, "Src")).json()
    dst = (await _create(client, "Dst")).json()
    # A leaf with no children deletes with an explicit reassign target.
    r = await client.request(
        "DELETE",
        f"{CATEGORIES}/{src['id']}",
        json={"reassign_to": dst["id"]},
        headers=await _csrf(client),
    )
    assert r.status_code == 204, r.text


async def test_delete_is_blocked_by_children(client) -> None:
    await _signup(client)
    parent = (await _create(client, "Parent")).json()
    await _create(client, "Child", parent_id=parent["id"])
    r = await client.request(
        "DELETE",
        f"{CATEGORIES}/{parent['id']}",
        json={"reassign_to": None},
        headers=await _csrf(client),
    )
    assert r.status_code == 409


async def test_other_ledger_category_is_a_404(client) -> None:
    await _signup(client, "a@example.com")
    mine = (await _create(client, "Mine")).json()
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))
    await _signup(client, "b@example.com")
    r = await client.get(f"{CATEGORIES}/{mine['id']}")
    assert r.status_code == 404
