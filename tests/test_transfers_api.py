"""/api/v1/transfers + the transaction list's is_transfer filter (M6 CP2, #27).

A Transfer is a first-class link row with structurally directional sides:
both present = linked pair, one = untracked counterparty. Membership vacates
category (being a transfer IS the classification) and derives spending
exclusion — one EXISTS, no flag to drift.
"""

TX = "/api/v1/transactions"
TRANSFERS = "/api/v1/transfers"
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


async def _account(client, label: str = "Checking", currency: str = "USD") -> str:
    r = await client.post(
        "/api/v1/accounts",
        json={"kind": "depository", "label": label, "currency": currency},
        headers=await _csrf(client),
    )
    return r.json()["id"]


async def _category(client, name: str) -> str:
    r = await client.post("/api/v1/categories", json={"name": name}, headers=await _csrf(client))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _txn(client, account_id: str, amount_minor: int, **extra) -> dict:
    r = await client.post(
        TX,
        json={
            "account_id": account_id,
            "date": "2026-07-10",
            "amount_minor": amount_minor,
            "description": "TRANSFER 4242",
        }
        | extra,
        headers=await _csrf(client),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _link(client, *txn_ids: str):
    return await client.post(
        TRANSFERS, json={"transaction_ids": list(txn_ids)}, headers=await _csrf(client)
    )


async def test_linked_pair_places_by_sign_and_vacates_both(client) -> None:
    await _signup(client)
    checking = await _account(client, "Checking")
    savings = await _account(client, "Savings")
    groceries = await _category(client, "Groceries")
    out_txn = await _txn(client, checking, -25000, category_id=groceries)
    in_txn = await _txn(client, savings, 25000, category_id=groceries)

    # id order must not matter — placement is dictated by sign.
    r = await _link(client, in_txn["id"], out_txn["id"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "linked"
    assert body["outflow_transaction_id"] == out_txn["id"]
    assert body["inflow_transaction_id"] == in_txn["id"]

    for txn_id in (out_txn["id"], in_txn["id"]):
        detail = (await client.get(f"{TX}/{txn_id}")).json()
        assert detail["category"] is None  # vacated, both sides
        assert detail["transfer"]["id"] == body["id"]
    out_detail = (await client.get(f"{TX}/{out_txn['id']}")).json()
    assert out_detail["transfer"]["kind"] == "linked"
    assert out_detail["transfer"]["counterpart_transaction_id"] == in_txn["id"]
    assert out_detail["transfer"]["counterpart_account_id"] == savings


async def test_untracked_single_side(client) -> None:
    await _signup(client)
    checking = await _account(client)
    txn = await _txn(client, checking, -9900)
    r = await _link(client, txn["id"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "untracked"
    assert body["outflow_transaction_id"] == txn["id"]
    assert body["inflow_transaction_id"] is None

    detail = (await client.get(f"{TX}/{txn['id']}")).json()
    assert detail["transfer"]["kind"] == "untracked"
    assert detail["transfer"]["counterpart_transaction_id"] is None
    assert detail["transfer"]["counterpart_account_id"] is None


async def test_dissolve_leaves_members_reviewed_and_uncategorized(client) -> None:
    await _signup(client)
    checking = await _account(client, "Checking")
    savings = await _account(client, "Savings")
    groceries = await _category(client, "Groceries")
    # category at birth => reviewed at birth (M5 CP4)
    out_txn = await _txn(client, checking, -25000, category_id=groceries)
    in_txn = await _txn(client, savings, 25000, category_id=groceries)
    transfer = (await _link(client, out_txn["id"], in_txn["id"])).json()

    r = await client.delete(f"{TRANSFERS}/{transfer['id']}", headers=await _csrf(client))
    assert r.status_code == 204, r.text
    for txn_id in (out_txn["id"], in_txn["id"]):
        detail = (await client.get(f"{TX}/{txn_id}")).json()
        assert detail["transfer"] is None
        assert detail["category"] is None  # dissolution does not restore
        assert detail["reviewed_at"] is not None  # members stay reviewed


async def test_list_transfers_paginates_with_either_side_account_filter(client) -> None:
    await _signup(client)
    checking = await _account(client, "Checking")
    savings = await _account(client, "Savings")
    brokerage = await _account(client, "Brokerage")

    pair_out = await _txn(client, checking, -10000)
    pair_in = await _txn(client, savings, 10000)
    linked = (await _link(client, pair_out["id"], pair_in["id"])).json()
    solo_out = await _txn(client, brokerage, -500)
    untracked = (await _link(client, solo_out["id"])).json()

    everything = (await client.get(TRANSFERS)).json()
    assert {t["id"] for t in everything["items"]} == {linked["id"], untracked["id"]}
    assert everything["next_cursor"] is None

    # account_id matches either side: savings only appears as the inflow.
    by_savings = (await client.get(TRANSFERS, params={"account_id": savings})).json()["items"]
    assert [t["id"] for t in by_savings] == [linked["id"]]
    by_brokerage = (await client.get(TRANSFERS, params={"account_id": brokerage})).json()["items"]
    assert [t["id"] for t in by_brokerage] == [untracked["id"]]

    paged = (await client.get(TRANSFERS, params={"limit": 1})).json()
    assert len(paged["items"]) == 1
    assert paged["next_cursor"] is not None


# ---------------------------------------------------------------- rejections


async def test_pair_shape_rejections_are_422_and_persist_nothing(client) -> None:
    await _signup(client)
    checking = await _account(client, "Checking")
    savings = await _account(client, "Savings")
    eur = await _account(client, "Euro", currency="EUR")

    neg_a = await _txn(client, checking, -5000)
    neg_b = await _txn(client, savings, -5000)
    pos_small = await _txn(client, savings, 4000)
    pos_eur = await _txn(client, eur, 5000)
    neg_same_acct = await _txn(client, checking, 5000)
    zero = await _txn(client, savings, 0)

    cases = [
        (neg_a, neg_b),  # same-sign pair
        (neg_a, pos_small),  # unequal magnitudes
        (neg_a, pos_eur),  # mixed currency
        (neg_a, neg_same_acct),  # same account
        (neg_a, zero),  # zero amount in a pair
        (zero,),  # zero amount untracked
        (neg_a, neg_a),  # the same transaction twice
    ]
    for case in cases:
        r = await _link(client, *(t["id"] for t in case))
        assert r.status_code == 422, (case, r.text)

    assert (await client.get(TRANSFERS)).json()["items"] == []  # nothing persisted


async def test_occupied_transaction_answers_409(client) -> None:
    await _signup(client)
    checking = await _account(client, "Checking")
    savings = await _account(client, "Savings")
    out_txn = await _txn(client, checking, -5000)
    in_txn = await _txn(client, savings, 5000)
    assert (await _link(client, out_txn["id"])).status_code == 201

    r = await _link(client, out_txn["id"], in_txn["id"])
    assert r.status_code == 409, r.text
    # The counterpart side is enforced too: occupy in_txn, then try again.
    assert (await _link(client, in_txn["id"])).status_code == 201
    other_neg = await _txn(client, checking, -5000)
    assert (await _link(client, other_neg["id"], in_txn["id"])).status_code == 409


async def test_cross_ledger_ids_404_without_confirming_existence(client) -> None:
    await _signup(client)
    checking = await _account(client)
    foreign_txn = await _txn(client, checking, -5000)

    client.cookies.clear()
    await _signup(client, email="other@example.com")
    own_acct = await _account(client)
    own_txn = await _txn(client, own_acct, 5000)

    r = await _link(client, foreign_txn["id"])
    assert r.status_code == 404
    assert r.json()["detail"] == "No such transaction"  # same as a bogus id
    r2 = await _link(client, own_txn["id"], foreign_txn["id"])
    assert r2.status_code == 404
    assert (await client.get(TRANSFERS)).json()["items"] == []

    # Dissolving a foreign transfer is likewise a 404.
    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login",
        json={"email": "taylor@example.com", "password": PASSWORD},
        headers=await _csrf(client),
    )
    mine = (await _link(client, foreign_txn["id"])).json()
    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login",
        json={"email": "other@example.com", "password": PASSWORD},
        headers=await _csrf(client),
    )
    r3 = await client.delete(f"{TRANSFERS}/{mine['id']}", headers=await _csrf(client))
    assert r3.status_code == 404


# ---------------------------------------------------------------- list filter


async def test_is_transfer_filter_derives_from_membership(client) -> None:
    await _signup(client)
    checking = await _account(client, "Checking")
    savings = await _account(client, "Savings")

    pair_out = await _txn(client, checking, -10000)
    pair_in = await _txn(client, savings, 10000)
    await _link(client, pair_out["id"], pair_in["id"])
    solo = await _txn(client, checking, -300)
    await _link(client, solo["id"])
    plain = await _txn(client, checking, -4200)

    members = (await client.get(TX, params={"is_transfer": "true"})).json()["items"]
    assert {t["id"] for t in members} == {pair_out["id"], pair_in["id"], solo["id"]}
    clean = (await client.get(TX, params={"is_transfer": "false"})).json()["items"]
    assert [t["id"] for t in clean] == [plain["id"]]


# ------------------------------------------------- review & proposal seams


async def test_transfer_never_reviews_never_logs_and_proposals_survive(client, run_jobs) -> None:
    await _signup(client)
    checking = await _account(client)
    body = "date,amount,description\n2026-07-01,-99.00,VENMO CASHOUT\n"
    up = await client.post(
        "/api/v1/imports",
        files={"file": ("bank.csv", body, "text/csv")},
        data={"account_id": checking},
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

    transfer = (await _link(client, txn["id"])).json()
    after = (await client.get(f"{TX}/{txn['id']}")).json()
    assert after["reviewed_at"] is None  # edits edit, review reviews
    assert after["proposal"] is not None  # the pending proposal survives

    await client.delete(f"{TRANSFERS}/{transfer['id']}", headers=await _csrf(client))
    final = (await client.get(f"{TX}/{txn['id']}")).json()
    assert final["reviewed_at"] is None
    assert final["proposal"] is not None

    log_entries = (await client.get("/api/v1/correction-log")).json()["items"]
    assert log_entries == []  # neither create nor dissolve wrote an entry


# ---------------------------------------------------------------- scope


async def test_read_scope_pat_is_refused_on_transfer_writes(client) -> None:
    await _signup(client)
    checking = await _account(client)
    txn = await _txn(client, checking, -5000)
    pat = await client.post(
        "/api/v1/auth/pats",
        json={"name": "reader", "scopes": ["read"]},
        headers=await _csrf(client),
    )
    read_token = pat.json()["token"]
    client.cookies.clear()
    r = await client.post(
        TRANSFERS,
        json={"transaction_ids": [txn["id"]]},
        headers={"Authorization": f"Bearer {read_token}"},
    )
    assert r.status_code == 403
