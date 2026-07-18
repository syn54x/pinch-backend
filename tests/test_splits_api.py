"""PUT/DELETE /api/v1/transactions/{id}/splits (M6 CP1, #26).

Split lines divide a transaction across categories — one atomic document,
validates-all-first, the parent persisting untouched as the anchor with its
own category vacated (exactly one layer holds categories). Data arrives
through the manual-entry seam; assertions stay at the HTTP seam.
"""

TX = "/api/v1/transactions"
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


async def _account(client, label: str = "Checking") -> str:
    r = await client.post(
        "/api/v1/accounts",
        json={"kind": "depository", "label": label, "currency": "USD"},
        headers=await _csrf(client),
    )
    return r.json()["id"]


async def _category(client, name: str, parent_id: str | None = None) -> str:
    body: dict = {"name": name}
    if parent_id is not None:
        body["parent_id"] = parent_id
    r = await client.post("/api/v1/categories", json=body, headers=await _csrf(client))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _txn(client, account_id: str, amount_minor: int = -7000, **extra) -> dict:
    r = await client.post(
        TX,
        json={
            "account_id": account_id,
            "date": "2026-07-10",
            "amount_minor": amount_minor,
            "description": "COSTCO WHOLESALE",
        }
        | extra,
        headers=await _csrf(client),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _put_splits(client, txn_id: str, lines: list[dict]):
    return await client.put(f"{TX}/{txn_id}/splits", json=lines, headers=await _csrf(client))


async def test_put_splits_vacates_parent_and_inlines_lines(client) -> None:
    await _signup(client)
    acct = await _account(client)
    groceries = await _category(client, "Groceries")
    txn = await _txn(client, acct, amount_minor=-7000, category_id=groceries)

    r = await _put_splits(
        client,
        txn["id"],
        [
            {"amount_minor": -3000, "category_id": groceries},
            {"amount_minor": -4000, "memo": "tires"},
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["category"] is None  # parent category vacated
    assert body["splits"] == [
        {"amount_minor": -3000, "category": {"id": groceries, "name": "Groceries"}, "memo": None},
        {"amount_minor": -4000, "category": None, "memo": "tires"},
    ]
    # The anchor persists: same transaction row, source data untouched.
    detail = (await client.get(f"{TX}/{txn['id']}")).json()
    assert detail["amount_minor"] == -7000
    assert detail["description_raw"] == "COSTCO WHOLESALE"
    assert detail["splits"] is not None and len(detail["splits"]) == 2


async def test_unsplit_leaves_parent_uncategorized_and_review_untouched(client) -> None:
    await _signup(client)
    acct = await _account(client)
    groceries = await _category(client, "Groceries")
    # category at birth => reviewed at birth (M5 CP4)
    txn = await _txn(client, acct, category_id=groceries)
    assert (await client.get(f"{TX}/{txn['id']}")).json()["reviewed_at"] is not None

    r = await _put_splits(
        client,
        txn["id"],
        [{"amount_minor": -3000, "category_id": groceries}, {"amount_minor": -4000}],
    )
    assert r.status_code == 200, r.text
    assert r.json()["reviewed_at"] is not None  # split never touches review state

    r2 = await client.delete(f"{TX}/{txn['id']}/splits", headers=await _csrf(client))
    assert r2.status_code == 204, r2.text
    detail = (await client.get(f"{TX}/{txn['id']}")).json()
    assert detail["splits"] is None
    assert detail["category"] is None  # stays uncategorized, not restored
    assert detail["reviewed_at"] is not None  # still reviewed


async def test_re_put_replaces_wholesale(client) -> None:
    await _signup(client)
    acct = await _account(client)
    groceries = await _category(client, "Groceries")
    snacks = await _category(client, "Snacks")
    txn = await _txn(client, acct)

    first = await _put_splits(
        client,
        txn["id"],
        [{"amount_minor": -3000, "category_id": groceries}, {"amount_minor": -4000}],
    )
    assert first.status_code == 200
    second = await _put_splits(
        client,
        txn["id"],
        [
            {"amount_minor": -1000, "category_id": snacks},
            {"amount_minor": -2000},
            {"amount_minor": -4000, "category_id": groceries},
        ],
    )
    assert second.status_code == 200, second.text
    splits = second.json()["splits"]
    assert [s["amount_minor"] for s in splits] == [-1000, -2000, -4000]
    assert splits[0]["category"]["name"] == "Snacks"


# ---------------------------------------------------------------- rejections


async def test_document_rejections_are_400_and_persist_nothing(client) -> None:
    await _signup(client)
    acct = await _account(client)
    groceries = await _category(client, "Groceries")
    txn = await _txn(client, acct, amount_minor=-7000, category_id=groceries)

    bad_documents = [
        [{"amount_minor": -7000}],  # fewer than two lines
        [{"amount_minor": -7000}, {"amount_minor": 0}],  # zero line
        [{"amount_minor": -8000}, {"amount_minor": 1000}],  # counter-signed line
        [{"amount_minor": -3000}, {"amount_minor": -3000}],  # sum mismatch
    ]
    for doc in bad_documents:
        r = await _put_splits(client, txn["id"], doc)
        assert r.status_code == 400, (doc, r.text)

    detail = (await client.get(f"{TX}/{txn['id']}")).json()
    assert detail["splits"] is None  # nothing persisted
    assert detail["category"]["id"] == groceries  # category not vacated


async def test_rejected_re_put_leaves_the_existing_document_intact(client) -> None:
    await _signup(client)
    acct = await _account(client)
    groceries = await _category(client, "Groceries")
    txn = await _txn(client, acct)
    ok = await _put_splits(
        client,
        txn["id"],
        [{"amount_minor": -3000, "category_id": groceries}, {"amount_minor": -4000}],
    )
    assert ok.status_code == 200
    bad = await _put_splits(client, txn["id"], [{"amount_minor": -1}, {"amount_minor": -2}])
    assert bad.status_code == 400
    splits = (await client.get(f"{TX}/{txn['id']}")).json()["splits"]
    assert [s["amount_minor"] for s in splits] == [-3000, -4000]


async def test_unknown_or_foreign_line_category_is_404(client) -> None:
    await _signup(client)
    acct = await _account(client)
    txn = await _txn(client, acct)
    r = await _put_splits(
        client,
        txn["id"],
        [
            {"amount_minor": -3000, "category_id": "019f0000-0000-7000-8000-000000000000"},
            {"amount_minor": -4000},
        ],
    )
    assert r.status_code == 404
    assert (await client.get(f"{TX}/{txn['id']}")).json()["splits"] is None


async def test_delete_on_an_unsplit_transaction_is_404(client) -> None:
    await _signup(client)
    acct = await _account(client)
    txn = await _txn(client, acct)
    r = await client.delete(f"{TX}/{txn['id']}/splits", headers=await _csrf(client))
    assert r.status_code == 404


# ---------------------------------------------------------------- list filter


async def test_category_filter_finds_splits_by_leaf_and_ancestor_subtree(client) -> None:
    await _signup(client)
    acct = await _account(client)
    food = await _category(client, "Food")
    produce = await _category(client, "Produce", parent_id=food)
    snacks = await _category(client, "Snacks", parent_id=food)
    other = await _category(client, "Other")

    split = await _txn(client, acct, amount_minor=-7000)
    await _put_splits(
        client,
        split["id"],
        [
            {"amount_minor": -3000, "category_id": produce},
            {"amount_minor": -4000, "category_id": snacks},
        ],
    )
    unsplit_match = await _txn(client, acct, amount_minor=-500, category_id=food)
    await _txn(client, acct, amount_minor=-900, category_id=other)  # must not match

    by_leaf = (await client.get(TX, params={"category_id": produce})).json()["items"]
    assert [t["id"] for t in by_leaf] == [split["id"]]

    by_ancestor = (await client.get(TX, params={"category_id": food})).json()["items"]
    assert {t["id"] for t in by_ancestor} == {split["id"], unsplit_match["id"]}
    assert len(by_ancestor) == 2  # the two-line split matches once, root-shaped


# ------------------------------------------------- dispositions & cascades


async def test_category_delete_disposition_extends_to_lines(client) -> None:
    await _signup(client)
    acct = await _account(client)
    groceries = await _category(client, "Groceries")
    food = await _category(client, "Food")
    doomed_null = await _category(client, "DoomedNull")

    txn = await _txn(client, acct, amount_minor=-7000)
    await _put_splits(
        client,
        txn["id"],
        [
            {"amount_minor": -3000, "category_id": groceries},
            {"amount_minor": -4000, "category_id": doomed_null},
        ],
    )
    # reassign disposition: groceries -> food
    r = await client.request(
        "DELETE",
        f"/api/v1/categories/{groceries}",
        json={"reassign_to": food},
        headers=await _csrf(client),
    )
    assert r.status_code == 204, r.text
    # null disposition: doomed_null -> uncategorized line
    r2 = await client.request(
        "DELETE",
        f"/api/v1/categories/{doomed_null}",
        json={"reassign_to": None},
        headers=await _csrf(client),
    )
    assert r2.status_code == 204, r2.text
    splits = (await client.get(f"{TX}/{txn['id']}")).json()["splits"]
    assert splits[0]["category"]["id"] == food
    assert splits[1]["category"] is None


async def test_lines_cascade_with_their_transaction_on_import_undo(client) -> None:
    """The import-undo path (M4) deletes transactions; their lines must die
    with them at the DB (CP0-verified CASCADE) — checked below the HTTP seam
    because the transaction itself is gone from the API."""
    from pinch_backend.models import SplitLine

    await _signup(client)
    acct = await _account(client)
    body = "date,amount,description\n2026-07-01,-70.00,COSTCO WHOLESALE\n"
    up = await client.post(
        "/api/v1/imports",
        files={"file": ("bank.csv", body, "text/csv")},
        data={"account_id": acct},
        headers=await _csrf(client),
    )
    iid = up.json()["id"]
    await client.post(
        f"/api/v1/imports/{iid}/mapping",
        json=up.json()["suggested_mapping"],
        headers=await _csrf(client),
    )
    commit = await client.post(
        f"/api/v1/imports/{iid}/commit", json={}, headers=await _csrf(client)
    )
    assert commit.status_code == 200, commit.text
    txn = (await client.get(TX)).json()["items"][0]
    await _put_splits(client, txn["id"], [{"amount_minor": -3000}, {"amount_minor": -4000}])
    assert await SplitLine.where(lambda ln: ln.id != None).count() == 2  # noqa: E711

    undo = await client.delete(f"/api/v1/imports/{iid}", headers=await _csrf(client))
    assert undo.status_code == 204, undo.text
    assert (await client.get(f"{TX}/{txn['id']}")).status_code == 404
    assert await SplitLine.where(lambda ln: ln.id != None).count() == 0  # noqa: E711


# ------------------------------------------------- review & proposal seams


async def test_split_never_reviews_never_logs_and_proposals_survive(client, run_jobs) -> None:
    await _signup(client)
    acct = await _account(client)
    groceries = await _category(client, "Groceries")
    # Arrive through the import seam so the sweep attaches a proposal.
    body = "date,amount,description\n2026-07-01,-70.00,COSTCO WHOLESALE\n"
    up = await client.post(
        "/api/v1/imports",
        files={"file": ("bank.csv", body, "text/csv")},
        data={"account_id": acct},
        headers=await _csrf(client),
    )
    iid = up.json()["id"]
    await client.post(
        f"/api/v1/imports/{iid}/mapping",
        json=up.json()["suggested_mapping"],
        headers=await _csrf(client),
    )
    await client.post(f"/api/v1/imports/{iid}/commit", json={}, headers=await _csrf(client))
    await run_jobs()
    txn = (await client.get(TX)).json()["items"][0]
    assert txn["reviewed_at"] is None
    assert txn["proposal"] is not None

    r = await _put_splits(
        client,
        txn["id"],
        [{"amount_minor": -3000, "category_id": groceries}, {"amount_minor": -4000}],
    )
    assert r.status_code == 200
    after = r.json()
    assert after["reviewed_at"] is None  # edits edit, review reviews
    assert after["proposal"] is not None  # the pending proposal survives

    await client.delete(f"{TX}/{txn['id']}/splits", headers=await _csrf(client))
    final = (await client.get(f"{TX}/{txn['id']}")).json()
    assert final["reviewed_at"] is None
    assert final["proposal"] is not None

    log_entries = (await client.get("/api/v1/correction-log")).json()["items"]
    assert log_entries == []  # neither split nor unsplit wrote an entry


# ---------------------------------------------------------------- tenancy


async def test_cross_ledger_splits_are_404_and_read_scope_403(client) -> None:
    await _signup(client)
    acct = await _account(client)
    groceries = await _category(client, "Groceries")
    txn = await _txn(client, acct)
    lines = [{"amount_minor": -3000, "category_id": groceries}, {"amount_minor": -4000}]

    pat = await client.post(
        "/api/v1/auth/pats",
        json={"name": "reader", "scopes": ["read"]},
        headers=await _csrf(client),
    )
    read_token = pat.json()["token"]

    # A second ledger cannot see (or write) the first's transaction.
    client.cookies.clear()
    await _signup(client, email="other@example.com")
    r = await _put_splits(client, txn["id"], lines)
    assert r.status_code == 404
    r2 = await client.delete(f"{TX}/{txn['id']}/splits", headers=await _csrf(client))
    assert r2.status_code == 404

    # A read-scoped PAT is refused on the write verb (scope guard).
    client.cookies.clear()
    r3 = await client.put(
        f"{TX}/{txn['id']}/splits",
        json=lines,
        headers={"Authorization": f"Bearer {read_token}"},
    )
    assert r3.status_code == 403
