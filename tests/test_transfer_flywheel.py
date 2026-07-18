"""Pipeline & flywheel for transfers (M6 CP4, #29): the mark-transfer rule
action deferred out of M5, history learning untracked transfers, transfer
promotion, and the milestone acceptance e2e — the flywheel thesis applied
to transfers, not just categories.
"""

TX = "/api/v1/transactions"
TRANSFERS = "/api/v1/transfers"
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


async def _account(client, label: str = "Checking") -> str:
    r = await client.post(
        "/api/v1/accounts",
        json={"kind": "depository", "label": label, "currency": "USD"},
        headers=await _csrf(client),
    )
    return r.json()["id"]


async def _category(client, name: str) -> str:
    r = await client.post("/api/v1/categories", json={"name": name}, headers=await _csrf(client))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _commit_csv(client, account_id: str, rows: list[tuple[str, str, str]]) -> str:
    body = "date,amount,description\n" + "\n".join(f"{d},{a},{m}" for d, a, m in rows) + "\n"
    up = await client.post(
        "/api/v1/imports",
        files={"file": ("bank.csv", body, "text/csv")},
        data={"account_id": account_id},
        headers=await _csrf(client),
    )
    assert up.status_code == 201, up.text
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
    return iid


async def _inbox(client) -> list[dict]:
    return (await client.get(TX, params={"reviewed": "false"})).json()["items"]


async def _review(client, txn_id: str, body: dict | None = None):
    return await client.post(f"{TX}/{txn_id}/review", json=body or {}, headers=await _csrf(client))


async def _rule(client, body: dict):
    return await client.post(RULES, json=body, headers=await _csrf(client))


# ----------------------------------------------------------- rule action


async def test_mark_transfer_rule_api_shapes(client) -> None:
    await _signup(client)
    groceries = await _category(client, "Groceries")

    r = await _rule(
        client,
        {
            "condition": {"payee": {"op": "contains", "value": "VENMO"}},
            "action_mark_transfer": True,
            "action_add_tags": ["cc-payment"],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["action_mark_transfer"] is True
    assert body["action_category"] is None

    # A rule cannot both categorize and mark transfer — contradictory law.
    both = await _rule(
        client,
        {
            "condition": {"payee": {"op": "contains", "value": "X"}},
            "action_mark_transfer": True,
            "action_category_id": groceries,
        },
    )
    assert both.status_code == 400, both.text
    # PATCHing a category onto a mark-transfer rule is the same contradiction.
    patched = await client.patch(
        f"{RULES}/{body['id']}",
        json={"action_category_id": groceries},
        headers=await _csrf(client),
    )
    assert patched.status_code == 400, patched.text
    # mark_transfer alone satisfies "a rule must carry at least one action".
    solo = await _rule(
        client,
        {
            "condition": {"payee": {"op": "equals", "value": "ZELLE OUT"}},
            "action_mark_transfer": True,
        },
    )
    assert solo.status_code == 201, solo.text


async def test_transfer_rule_beats_category_stages_but_tags_ride(client, run_jobs) -> None:
    await _signup(client)
    acct = await _account(client)
    groceries = await _category(client, "Groceries")
    # History seed for the payee — must be SKIPPED once a transfer rule matches.
    await _commit_csv(client, acct, [("2026-07-01", "-99.00", "VENMO CASHOUT")])
    await run_jobs()
    (seed,) = await _inbox(client)
    assert (await _review(client, seed["id"], {"category_id": groceries})).status_code == 200

    assert (
        await _rule(
            client,
            {
                "condition": {"payee": {"op": "contains", "value": "VENMO"}},
                "action_mark_transfer": True,
            },
        )
    ).status_code == 201
    assert (
        await _rule(
            client,
            {
                "condition": {"payee": {"op": "contains", "value": "VENMO"}},
                "action_add_tags": ["cc-payment"],
                "action_rename_to": "Venmo transfer",
            },
        )
    ).status_code == 201

    await _commit_csv(client, acct, [("2026-07-02", "-88.00", "VENMO CASHOUT")])
    await run_jobs()
    (txn,) = await _inbox(client)
    proposal = txn["proposal"]
    assert proposal["proposed_transfer"] is True
    assert proposal["category"] is None  # history's Groceries suppressed
    assert proposal["provenance"] == "rule"
    assert proposal["tags"] == ["cc-payment"]  # tag-union rides
    assert proposal["display_name"] == "Venmo transfer"  # first-rename rides


async def test_zero_amount_never_proposes_a_transfer(client, run_jobs) -> None:
    await _signup(client)
    acct = await _account(client)
    assert (
        await _rule(
            client,
            {
                "condition": {"payee": {"op": "contains", "value": "VOID"}},
                "action_mark_transfer": True,
            },
        )
    ).status_code == 201
    await _commit_csv(client, acct, [("2026-07-01", "0.00", "VOID CHECK")])
    await run_jobs()
    (txn,) = await _inbox(client)
    assert txn["proposal"]["proposed_transfer"] is False  # unlinkable, never proposed

    # And accept-as-is stays transfer-free.
    assert (await _review(client, txn["id"])).status_code == 200
    assert (await client.get(f"{TX}/{txn['id']}")).json()["transfer"] is None


# ----------------------------------------------------------- consume paths


async def test_consuming_proposed_transfer_single_batch_and_since_split(client, run_jobs) -> None:
    await _signup(client)
    acct = await _account(client)
    assert (
        await _rule(
            client,
            {
                "condition": {"payee": {"op": "contains", "value": "VENMO"}},
                "action_mark_transfer": True,
            },
        )
    ).status_code == 201
    await _commit_csv(
        client,
        acct,
        [
            ("2026-07-01", "-10.00", "VENMO CASHOUT"),
            ("2026-07-02", "-20.00", "VENMO CASHOUT"),
            ("2026-07-03", "-30.00", "VENMO CASHOUT"),
        ],
    )
    await run_jobs()
    inbox = await _inbox(client)
    assert all(t["proposal"]["proposed_transfer"] for t in inbox)
    single = next(t for t in inbox if t["amount_minor"] == -1000)
    batched = next(t for t in inbox if t["amount_minor"] == -2000)
    since_split = next(t for t in inbox if t["amount_minor"] == -3000)

    # Single review, empty body: accepting a transfer proposal IS the accept.
    r = await _review(client, single["id"])
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "accepted"
    assert r.json()["transaction"]["transfer"]["kind"] == "untracked"

    # The since-split transaction is accepted WITHOUT a transfer (exclusivity).
    assert (
        await client.put(
            f"{TX}/{since_split['id']}/splits",
            json=[{"amount_minor": -1000}, {"amount_minor": -2000}],
            headers=await _csrf(client),
        )
    ).status_code == 200

    batch = await client.post(
        f"{TX}/review",
        json={"ids": [batched["id"], since_split["id"]]},
        headers=await _csrf(client),
    )
    assert batch.status_code == 200, batch.text
    assert batch.json()["accepted"] == 2

    batched_detail = (await client.get(f"{TX}/{batched['id']}")).json()
    assert batched_detail["transfer"]["kind"] == "untracked"
    split_detail = (await client.get(f"{TX}/{since_split['id']}")).json()
    assert split_detail["transfer"] is None
    assert split_detail["reviewed_at"] is not None

    entries = (await client.get("/api/v1/correction-log", params={"kind": "decision"})).json()[
        "items"
    ]
    by_txn = {e["transaction_id"]: e for e in entries}
    assert by_txn[single["id"]]["decision_transfer"]["kind"] == "untracked"
    assert by_txn[batched["id"]]["decision_transfer"]["kind"] == "untracked"
    assert by_txn[since_split["id"]]["decision_transfer"] is None
    assert by_txn[since_split["id"]]["decision_splits"] is not None


# ----------------------------------------------------------- history


async def test_history_proposes_untracked_after_one_filing(client, run_jobs) -> None:
    await _signup(client)
    acct = await _account(client)
    await _commit_csv(client, acct, [("2026-07-01", "-99.00", "ZELLE TO LANDLORD")])
    await run_jobs()
    (first,) = await _inbox(client)
    assert (
        await _review(client, first["id"], {"transfer": {"untracked": True}})
    ).status_code == 200

    await _commit_csv(client, acct, [("2026-07-08", "-99.00", "ZELLE TO LANDLORD")])
    await run_jobs()
    (second,) = await _inbox(client)
    proposal = second["proposal"]
    assert proposal["proposed_transfer"] is True
    assert proposal["provenance"] == "history"
    assert proposal["category"] is None


async def test_linked_transfers_are_not_history_signals(client, run_jobs) -> None:
    await _signup(client)
    checking = await _account(client, "Checking")
    savings = await _account(client, "Savings")
    await _commit_csv(client, checking, [("2026-07-01", "-250.00", "TRANSFER TO SAVINGS")])
    await _commit_csv(client, savings, [("2026-07-02", "250.00", "TRANSFER FROM CHECKING")])
    await run_jobs()
    inbox = await _inbox(client)
    outflow = next(t for t in inbox if t["amount_minor"] < 0)
    inflow = next(t for t in inbox if t["amount_minor"] > 0)
    r = await _review(client, outflow["id"], {"transfer": {"counterpart": inflow["id"]}})
    assert r.status_code == 200, r.text

    # The same payee arrives again: the counterpart EXISTS somewhere — an
    # "untracked" proposal would be wrong, so history stays silent.
    await _commit_csv(client, checking, [("2026-07-08", "-250.00", "TRANSFER TO SAVINGS")])
    await run_jobs()
    (again,) = await _inbox(client)
    assert again["proposal"]["proposed_transfer"] is False
    assert again["proposal"]["provenance"] == "none"


# ----------------------------------------------------------- promotion


async def test_promotion_mints_mark_transfer_rule_at_three(client, run_jobs) -> None:
    await _signup(client)
    acct = await _account(client)
    responses = []
    for day, amount in (("01", "-10.00"), ("02", "-20.00"), ("03", "-30.00")):
        await _commit_csv(client, acct, [(f"2026-07-{day}", amount, "ZELLE TO LANDLORD")])
        await run_jobs()
        (txn,) = await _inbox(client)
        r = await _review(client, txn["id"], {"transfer": {"untracked": True}})
        assert r.status_code == 200, r.text
        responses.append(r.json())

    assert responses[0]["proposed_rule"] is None
    assert responses[1]["proposed_rule"] is None
    minted = responses[2]["proposed_rule"]
    assert minted is not None  # the consent moment rides the response
    assert minted["status"] == "proposed"
    assert minted["action_mark_transfer"] is True
    assert minted["action_category"] is None
    assert minted["condition"]["payee"] == {"op": "equals", "value": "zelle to landlord"}

    # Dismissal is a tombstone: filing #4 must not re-propose.
    dismissed = await client.patch(
        f"{RULES}/{minted['id']}", json={"status": "dismissed"}, headers=await _csrf(client)
    )
    assert dismissed.status_code == 200, dismissed.text
    await _commit_csv(client, acct, [("2026-07-04", "-40.00", "ZELLE TO LANDLORD")])
    await run_jobs()
    (fourth,) = await _inbox(client)
    r4 = await _review(client, fourth["id"], {"transfer": {"untracked": True}})
    assert r4.status_code == 200
    assert r4.json()["proposed_rule"] is None


async def test_mixed_treatment_mints_nothing_in_either_direction(client, run_jobs) -> None:
    await _signup(client)
    acct = await _account(client)
    groceries = await _category(client, "Groceries")

    # Direction one: a category filing kills transfer promotion.
    for day, body in (
        ("01", {"transfer": {"untracked": True}}),
        ("02", {"transfer": {"untracked": True}}),
        ("03", {"category_id": groceries}),  # the deviation
        ("04", {"transfer": {"untracked": True}}),
    ):
        await _commit_csv(client, acct, [(f"2026-07-{day}", "-15.00", "VENMO CASHOUT")])
        await run_jobs()
        txn = next(t for t in await _inbox(client))
        r = await _review(client, txn["id"], body)
        assert r.status_code == 200, r.text
        assert r.json()["proposed_rule"] is None

    # Direction two: transfer filings kill category promotion.
    for day, body in (
        ("11", {"category_id": groceries}),
        ("12", {"category_id": groceries}),
        ("13", {"transfer": {"untracked": True}}),  # the deviation
        ("14", {"category_id": groceries}),
    ):
        await _commit_csv(client, acct, [(f"2026-07-{day}", "-25.00", "COSTCO WHOLESALE")])
        await run_jobs()
        txn = next(t for t in await _inbox(client))
        r = await _review(client, txn["id"], body)
        assert r.status_code == 200, r.text
        assert r.json()["proposed_rule"] is None

    proposed = (await client.get(RULES, params={"status": "proposed"})).json()["items"]
    assert proposed == []


# ----------------------------------------------------------- acceptance e2e


async def test_transfer_flywheel_e2e(client, run_jobs) -> None:
    """The milestone acceptance path (#24): import checking + savings → link
    one pair manually → mark a recurring payee untracked at review → next
    import, history proposes it → third filing → promotion proposes a
    mark-transfer rule → accept → rule wins precedence → is_transfer=false
    lists clean spending."""
    await _signup(client)
    checking = await _account(client, "Checking")
    savings = await _account(client, "Savings")

    # Month one: a savings sweep pair and the first VENMO cashout.
    await _commit_csv(
        client,
        checking,
        [
            ("2026-06-01", "-500.00", "TRANSFER TO SAVINGS"),
            ("2026-06-02", "-42.00", "VENMO CASHOUT"),
            ("2026-06-03", "-70.00", "COSTCO WHOLESALE"),
        ],
    )
    await _commit_csv(client, savings, [("2026-06-01", "500.00", "TRANSFER FROM CHECKING")])
    await run_jobs()
    inbox = await _inbox(client)

    # Link the sweep pair manually, one motion from the inbox.
    sweep_out = next(t for t in inbox if t["amount_minor"] == -50000)
    sweep_in = next(t for t in inbox if t["amount_minor"] == 50000)
    r = await _review(client, sweep_out["id"], {"transfer": {"counterpart": sweep_in["id"]}})
    assert r.status_code == 200, r.text

    # Mark the cashout untracked; file the Costco run honestly.
    groceries = await _category(client, "Groceries")
    venmo1 = next(t for t in inbox if t["amount_minor"] == -4200)
    assert (
        await _review(client, venmo1["id"], {"transfer": {"untracked": True}})
    ).status_code == 200
    costco = next(t for t in inbox if t["amount_minor"] == -7000)
    assert (await _review(client, costco["id"], {"category_id": groceries})).status_code == 200

    # Month two: history proposes the cashout as an untracked transfer.
    await _commit_csv(client, checking, [("2026-07-02", "-43.00", "VENMO CASHOUT")])
    await run_jobs()
    (venmo2,) = await _inbox(client)
    assert venmo2["proposal"]["proposed_transfer"] is True
    assert venmo2["proposal"]["provenance"] == "history"
    second = await _review(client, venmo2["id"])  # accept the proposal as-is
    assert second.status_code == 200 and second.json()["result"] == "accepted"

    # Month three: the third consistent filing mints a mark-transfer rule.
    await _commit_csv(client, checking, [("2026-08-02", "-44.00", "VENMO CASHOUT")])
    await run_jobs()
    (venmo3,) = await _inbox(client)
    third = await _review(client, venmo3["id"])
    assert third.status_code == 200, third.text
    minted = third.json()["proposed_rule"]
    assert minted is not None and minted["action_mark_transfer"] is True

    # Consent: accept the proposed rule; it becomes law and wins precedence.
    accepted = await client.patch(
        f"{RULES}/{minted['id']}", json={"status": "active"}, headers=await _csrf(client)
    )
    assert accepted.status_code == 200, accepted.text
    await _commit_csv(client, checking, [("2026-09-02", "-45.00", "VENMO CASHOUT")])
    await run_jobs()
    (venmo4,) = await _inbox(client)
    assert venmo4["proposal"]["proposed_transfer"] is True
    assert venmo4["proposal"]["provenance"] == "rule"
    assert (await _review(client, venmo4["id"])).status_code == 200

    # Clean spending: transfers excluded, the Costco run remains.
    clean = (await client.get(TX, params={"is_transfer": "false"})).json()["items"]
    assert [t["description_raw"] for t in clean] == ["COSTCO WHOLESALE"]
    members = (await client.get(TX, params={"is_transfer": "true"})).json()["items"]
    assert len(members) == 6  # the pair + four cashouts
