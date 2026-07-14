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


async def _one_txn(client) -> str:
    await _signup(client)
    acct = await _account(client)
    await _import(client, acct, [("2026-01-01", "-5.00", "COFFEE SHOP")])
    return (await client.get(TX)).json()["items"][0]["id"]


async def test_patch_assigns_category_tags_and_reviews(client) -> None:
    txn_id = await _one_txn(client)
    cat = (
        await client.post(
            "/api/v1/categories", json={"name": "Coffee"}, headers=await _csrf(client)
        )
    ).json()
    r = await client.patch(
        f"{TX}/{txn_id}",
        json={
            "category_id": cat["id"],
            "tags": ["morning", "Morning"],  # casefold-deduped to one
            "display_name": "Blue Bottle",
            "notes": "oat latte",
            "reviewed": True,
        },
        headers=await _csrf(client),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["category"]["id"] == cat["id"]
    assert [t["name"] for t in body["tags"]] == ["morning"]
    assert body["display_name"] == "Blue Bottle"
    assert body["notes"] == "oat latte"
    assert body["reviewed_at"] is not None


async def test_patch_can_clear_category_and_unreview(client) -> None:
    txn_id = await _one_txn(client)
    cat = (
        await client.post(
            "/api/v1/categories", json={"name": "Coffee"}, headers=await _csrf(client)
        )
    ).json()
    await client.patch(
        f"{TX}/{txn_id}",
        json={"category_id": cat["id"], "reviewed": True},
        headers=await _csrf(client),
    )
    r = await client.patch(
        f"{TX}/{txn_id}",
        json={"category_id": None, "reviewed": False},
        headers=await _csrf(client),
    )
    body = r.json()
    assert body["category"] is None
    assert body["reviewed_at"] is None


async def test_patch_leaves_unmentioned_fields_untouched(client) -> None:
    txn_id = await _one_txn(client)
    await client.patch(f"{TX}/{txn_id}", json={"notes": "keep me"}, headers=await _csrf(client))
    r = await client.patch(
        f"{TX}/{txn_id}", json={"display_name": "Renamed"}, headers=await _csrf(client)
    )
    assert r.json()["notes"] == "keep me"  # not wiped by the second patch


async def test_read_scoped_pat_cannot_patch(client) -> None:
    txn_id = await _one_txn(client)
    pat = await client.post(
        "/api/v1/auth/pats",
        json={"name": "ro", "scopes": ["read"]},
        headers=await _csrf(client),
    )
    token = pat.json()["token"]
    r = await client.patch(
        f"{TX}/{txn_id}",
        json={"notes": "nope"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


# --- Filter round-trip tests (added per Task 9 review): the category/tag/
# reviewed filters can only be exercised once PATCH exists to assign those
# values, so their integration coverage lands here, at the real seam. ---


async def _setup_txns(client, rows):
    """Sign up, import `rows`, and return a {description: transaction_id} map."""
    await _signup(client)
    acct = await _account(client)
    await _import(client, acct, rows)
    items = (await client.get(f"{TX}?limit=100")).json()["items"]
    return {i["description_raw"]: i["id"] for i in items}


async def test_category_id_filter_is_subtree_inclusive(client) -> None:
    ids = await _setup_txns(client, [("2026-01-01", "-5.00", "DINNER")])
    food = (
        await client.post("/api/v1/categories", json={"name": "Food2"}, headers=await _csrf(client))
    ).json()
    rest = (
        await client.post(
            "/api/v1/categories",
            json={"name": "Restaurants2", "parent_id": food["id"]},
            headers=await _csrf(client),
        )
    ).json()
    await client.patch(
        f"{TX}/{ids['DINNER']}", json={"category_id": rest["id"]}, headers=await _csrf(client)
    )
    # Filtering by the PARENT returns the child-categorized transaction.
    r = await client.get(f"{TX}?category_id={food['id']}")
    assert [i["description_raw"] for i in r.json()["items"]] == ["DINNER"]


async def test_tag_filter_is_and_composition(client) -> None:
    ids = await _setup_txns(
        client,
        [
            ("2026-01-02", "-1.00", "BOTH"),
            ("2026-01-01", "-2.00", "ONE"),
        ],
    )
    await client.patch(
        f"{TX}/{ids['BOTH']}", json={"tags": ["x", "y"]}, headers=await _csrf(client)
    )
    await client.patch(f"{TX}/{ids['ONE']}", json={"tags": ["x"]}, headers=await _csrf(client))
    r = await client.get(f"{TX}?tag=x&tag=y")  # AND: only the row with both
    assert [i["description_raw"] for i in r.json()["items"]] == ["BOTH"]
    r2 = await client.get(f"{TX}?tag=x")  # single tag: both rows
    assert {i["description_raw"] for i in r2.json()["items"]} == {"BOTH", "ONE"}
    r3 = await client.get(f"{TX}?tag=x&tag=nope")  # unknown tag: empty, not ignored
    assert r3.json()["items"] == []


async def test_reviewed_filter_splits_inbox_from_done(client) -> None:
    ids = await _setup_txns(
        client,
        [
            ("2026-01-02", "-1.00", "DONE"),
            ("2026-01-01", "-2.00", "TODO"),
        ],
    )
    await client.patch(f"{TX}/{ids['DONE']}", json={"reviewed": True}, headers=await _csrf(client))
    r = await client.get(f"{TX}?reviewed=true")
    assert [i["description_raw"] for i in r.json()["items"]] == ["DONE"]
    r2 = await client.get(f"{TX}?reviewed=false")
    assert [i["description_raw"] for i in r2.json()["items"]] == ["TODO"]


async def test_uncategorized_filter_excludes_categorized_rows(client) -> None:
    ids = await _setup_txns(
        client,
        [
            ("2026-01-02", "-1.00", "HASCAT"),
            ("2026-01-01", "-2.00", "NOCAT"),
        ],
    )
    cat = (
        await client.post("/api/v1/categories", json={"name": "Misc2"}, headers=await _csrf(client))
    ).json()
    await client.patch(
        f"{TX}/{ids['HASCAT']}", json={"category_id": cat["id"]}, headers=await _csrf(client)
    )
    r = await client.get(f"{TX}?uncategorized=true")
    assert [i["description_raw"] for i in r.json()["items"]] == ["NOCAT"]
