"""/api/v1/rules over the public seam (M5 CP2, #20)."""

RULES = "/api/v1/rules"
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


async def _category(client, name: str) -> dict:
    r = await client.post("/api/v1/categories", json={"name": name}, headers=await _csrf(client))
    return r.json()


async def _create_rule(client, **over):
    payload = {
        "condition": {"payee": {"op": "contains", "value": "costco"}},
        "action_add_tags": ["bulk"],
    } | over
    return await client.post(RULES, json=payload, headers=await _csrf(client))


async def test_create_defaults_to_active_and_round_trips(client) -> None:
    await _signup(client)
    cat = await _category(client, "Groceries3")
    r = await _create_rule(client, action_category_id=cat["id"], action_rename_to="Costco")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["condition"]["payee"]["value"] == "costco"
    assert body["action_category"] == {"id": cat["id"], "name": "Groceries3"}
    assert body["action_add_tags"] == ["bulk"]
    assert body["action_rename_to"] == "Costco"


async def test_create_fills_amount_currency_from_primary(client) -> None:
    await _signup(client)
    r = await _create_rule(
        client,
        condition={"amount": {"op": "equals", "value": 999, "direction": "out"}},
    )
    assert r.status_code == 201, r.text
    assert r.json()["condition"]["amount"]["currency"] == "USD"


async def test_create_requires_at_least_one_action(client) -> None:
    await _signup(client)
    r = await _create_rule(client, action_add_tags=[])
    assert r.status_code == 400


async def test_create_rejects_empty_or_versionless_garbage_condition(client) -> None:
    await _signup(client)
    assert (await _create_rule(client, condition={})).status_code == 400
    assert (
        await _create_rule(
            client, condition={"version": 2, "payee": {"op": "equals", "value": "x"}}
        )
    ).status_code == 400


async def test_foreign_action_category_is_a_404(client) -> None:
    await _signup(client, "a@example.com")
    cat = await _category(client, "Mine2")
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))
    await _signup(client, "b@example.com")
    r = await _create_rule(client, action_category_id=cat["id"])
    assert r.status_code == 404


async def test_list_filters_by_status_and_pages(client) -> None:
    await _signup(client)
    await _create_rule(client)
    r = await client.patch(
        f"{RULES}/{(await _create_rule(client)).json()['id']}",
        json={"status": "disabled"},
        headers=await _csrf(client),
    )
    assert r.status_code == 200, r.text
    active = await client.get(f"{RULES}?status=active")
    assert {i["status"] for i in active.json()["items"]} == {"active"}
    everything = await client.get(RULES)
    assert {"items", "next_cursor"} <= everything.json().keys()
    assert len(everything.json()["items"]) == 2


async def test_patch_replaces_condition_whole_and_enforces_actions(client) -> None:
    await _signup(client)
    rule = (await _create_rule(client)).json()
    r = await client.patch(
        f"{RULES}/{rule['id']}",
        json={"condition": {"day_of_month": {"op": "equals", "value": 30}}},
        headers=await _csrf(client),
    )
    body = r.json()
    assert "payee" not in {k: v for k, v in body["condition"].items() if v is not None}
    # Clearing the only action is rejected: a rule must do something.
    r2 = await client.patch(
        f"{RULES}/{rule['id']}", json={"action_add_tags": []}, headers=await _csrf(client)
    )
    assert r2.status_code == 400


async def test_delete_then_404(client) -> None:
    await _signup(client)
    rule = (await _create_rule(client)).json()
    r = await client.request("DELETE", f"{RULES}/{rule['id']}", headers=await _csrf(client))
    assert r.status_code == 204
    assert (await client.get(f"{RULES}/{rule['id']}")).status_code == 404


async def test_tenancy_and_scope(client) -> None:
    await _signup(client, "a@example.com")
    rule = (await _create_rule(client)).json()
    pat = await client.post(
        "/api/v1/auth/pats",
        json={"name": "ro", "scopes": ["read"]},
        headers=await _csrf(client),
    )
    token = pat.json()["token"]
    ro = await client.post(
        RULES,
        json={"condition": {"payee": {"op": "equals", "value": "x"}}, "action_add_tags": ["t"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ro.status_code == 403
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))
    await _signup(client, "b@example.com")
    assert (await client.get(f"{RULES}/{rule['id']}")).status_code == 404
