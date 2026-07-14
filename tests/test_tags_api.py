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
