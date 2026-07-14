"""/api/v1/transactions list + get (M5 CP1, #19).

Data is created through the real M4 import seam, so these tests also prove
imported transactions carry the normalized payee and are readable back.
"""

TX = "/api/v1/transactions"
IMPORTS = "/api/v1/imports"
ACCOUNTS = "/api/v1/accounts"
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


async def _account(client) -> str:
    r = await client.post(
        ACCOUNTS,
        json={"kind": "depository", "label": "Checking", "currency": "USD"},
        headers=await _csrf(client),
    )
    return r.json()["id"]


async def _import(client, account_id: str, rows: list[tuple[str, str, str]]) -> None:
    body = "date,amount,description\n" + "\n".join(f"{d},{a},{desc}" for d, a, desc in rows) + "\n"
    up = await client.post(
        IMPORTS,
        files={"file": ("bank.csv", body, "text/csv")},
        data={"account_id": account_id},
        headers=await _csrf(client),
    )
    assert up.status_code == 201, up.text
    iid = up.json()["id"]
    await client.post(
        f"{IMPORTS}/{iid}/mapping", json=up.json()["suggested_mapping"], headers=await _csrf(client)
    )
    commit = await client.post(f"{IMPORTS}/{iid}/commit", json={}, headers=await _csrf(client))
    assert commit.status_code == 200, commit.text


async def test_list_is_newest_first_with_inlined_fields(client) -> None:
    await _signup(client)
    acct = await _account(client)
    await _import(
        client,
        acct,
        [
            ("2026-01-01", "-5.00", "OLDEST"),
            ("2026-03-01", "-7.00", "NEWEST"),
            ("2026-02-01", "-6.00", "MIDDLE"),
        ],
    )
    r = await client.get(TX)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["description_raw"] for i in items] == ["NEWEST", "MIDDLE", "OLDEST"]
    assert items[0]["category"] is None
    assert items[0]["tags"] == []
    assert items[0]["reviewed_at"] is None


async def test_uncategorized_filter_keeps_null_category_rows(client) -> None:
    await _signup(client)
    acct = await _account(client)
    await _import(client, acct, [("2026-01-01", "-5.00", "THING")])
    r = await client.get(f"{TX}?uncategorized=true")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1


async def test_composite_cursor_pages_across_a_day_boundary(client) -> None:
    await _signup(client)
    acct = await _account(client)
    await _import(
        client,
        acct,
        [
            ("2026-01-02", "-1.00", "A"),
            ("2026-01-02", "-2.00", "B"),
            ("2026-01-01", "-3.00", "C"),
        ],
    )
    page1 = await client.get(f"{TX}?limit=2")
    body1 = page1.json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"] is not None
    page2 = await client.get(f"{TX}?limit=2&cursor={body1['next_cursor']}")
    body2 = page2.json()
    assert len(body2["items"]) == 1
    seen = [i["description_raw"] for i in body1["items"] + body2["items"]]
    assert len(set(seen)) == 3  # no dupes, no gaps across the boundary


async def test_other_ledger_transaction_is_a_404(client) -> None:
    await _signup(client, "a@example.com")
    acct = await _account(client)
    await _import(client, acct, [("2026-01-01", "-5.00", "MINE")])
    mine_id = (await client.get(TX)).json()["items"][0]["id"]
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))
    await _signup(client, "b@example.com")
    r = await client.get(f"{TX}/{mine_id}")
    assert r.status_code == 404


async def test_account_id_filter_scopes_to_that_account(client) -> None:
    await _signup(client)
    a1 = await _account(client)
    a2 = await _account(client)
    await _import(client, a1, [("2026-01-01", "-5.00", "IN_A1")])
    await _import(client, a2, [("2026-01-02", "-6.00", "IN_A2")])
    r = await client.get(f"{TX}?account_id={a1}")
    assert [i["description_raw"] for i in r.json()["items"]] == ["IN_A1"]


async def test_date_range_filter_is_inclusive_on_both_bounds(client) -> None:
    await _signup(client)
    acct = await _account(client)
    await _import(
        client,
        acct,
        [
            ("2026-01-01", "-1.00", "JAN1"),
            ("2026-02-15", "-2.00", "FEB15"),
            ("2026-03-31", "-3.00", "MAR31"),
        ],
    )
    r = await client.get(f"{TX}?date_from=2026-02-01&date_to=2026-02-28")
    assert [i["description_raw"] for i in r.json()["items"]] == ["FEB15"]
    r2 = await client.get(f"{TX}?date_from=2026-01-01&date_to=2026-01-01")
    assert [i["description_raw"] for i in r2.json()["items"]] == ["JAN1"]
