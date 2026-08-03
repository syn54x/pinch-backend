"""/api/v1/rules over the public seam (M5 CP2, #20)."""

from pinch_backend.models import Ledger, Rule, RuleStatus

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
    assert body["origin"] == "user"
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


async def test_create_rejects_whitespace_only_payee_value(client) -> None:
    await _signup(client)
    r = await _create_rule(client, condition={"payee": {"op": "contains", "value": " "}})
    assert r.status_code == 400


async def test_create_rejects_unknown_top_level_condition_key(client) -> None:
    await _signup(client)
    r = await _create_rule(
        client,
        condition={
            "day_of_week": {"op": "equals", "value": 1},
            "payee": {"op": "equals", "value": "x"},
        },
    )
    assert r.status_code == 400


async def test_create_rejects_unknown_nested_condition_key(client) -> None:
    await _signup(client)
    r = await _create_rule(
        client,
        condition={"payee": {"op": "equals", "value": "x", "case_sensitive": True}},
    )
    assert r.status_code == 400


async def test_create_rejects_blank_currency_instead_of_defaulting(client) -> None:
    await _signup(client)
    r = await _create_rule(
        client,
        condition={"amount": {"op": "equals", "value": 999, "direction": "out", "currency": ""}},
    )
    assert r.status_code == 400


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
    assert "payee" not in body["condition"]
    # Clearing the only action is rejected: a rule must do something.
    r2 = await client.patch(
        f"{RULES}/{rule['id']}", json={"action_add_tags": []}, headers=await _csrf(client)
    )
    assert r2.status_code == 400


async def test_patch_null_add_tags_clears_them(client) -> None:
    await _signup(client)
    cat = await _category(client, "Groceries4")
    rule = (await _create_rule(client, action_category_id=cat["id"])).json()
    assert rule["action_add_tags"] == ["bulk"]
    r = await client.patch(
        f"{RULES}/{rule['id']}", json={"action_add_tags": None}, headers=await _csrf(client)
    )
    assert r.status_code == 200, r.text
    assert r.json()["action_add_tags"] == []


async def test_patch_null_add_tags_400s_when_it_was_the_only_action(client) -> None:
    await _signup(client)
    rule = (await _create_rule(client)).json()
    r = await client.patch(
        f"{RULES}/{rule['id']}", json={"action_add_tags": None}, headers=await _csrf(client)
    )
    assert r.status_code == 400


async def test_patch_null_status_is_a_400(client) -> None:
    await _signup(client)
    rule = (await _create_rule(client)).json()
    r = await client.patch(
        f"{RULES}/{rule['id']}", json={"status": None}, headers=await _csrf(client)
    )
    assert r.status_code == 400


async def test_patch_rejects_fabricated_proposed_status(client) -> None:
    await _signup(client)
    rule = (await _create_rule(client)).json()
    r = await client.patch(
        f"{RULES}/{rule['id']}", json={"status": "proposed"}, headers=await _csrf(client)
    )
    assert r.status_code == 400


async def test_patch_proposed_to_active_still_works(client) -> None:
    await _signup(client)
    ledger = (await Ledger.all())[0]
    rule = await Rule.create(
        ledger=ledger,
        status=RuleStatus.PROPOSED,
        condition={"payee": {"op": "equals", "value": "starbucks"}},
        action_add_tags=["coffee"],
    )
    r = await client.patch(
        f"{RULES}/{rule.id}", json={"status": "active"}, headers=await _csrf(client)
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"


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


# --- Preview (story 9: rules built with evidence, not hope) -------------------

IMPORTS = "/api/v1/imports"
ACCOUNTS = "/api/v1/accounts"


async def _import_rows(client, rows: list[tuple[str, str, str]]) -> None:
    account = await client.post(
        ACCOUNTS,
        json={"kind": "depository", "label": "Chk", "currency": "USD"},
        headers=await _csrf(client),
    )
    body = "date,amount,description\n" + "\n".join(f"{d},{a},{desc}" for d, a, desc in rows) + "\n"
    up = await client.post(
        IMPORTS,
        files={"file": ("bank.csv", body, "text/csv")},
        data={"account_id": account.json()["id"]},
        headers=await _csrf(client),
    )
    iid = up.json()["id"]
    await client.post(
        f"{IMPORTS}/{iid}/mapping", json=up.json()["suggested_mapping"], headers=await _csrf(client)
    )
    commit = await client.post(f"{IMPORTS}/{iid}/commit", json={}, headers=await _csrf(client))
    assert commit.status_code == 200, commit.text


async def test_preview_samples_matches_before_any_rule_exists(client) -> None:
    await _signup(client)
    await _import_rows(
        client,
        [("2026-01-10", "-9.50", "COSTCO WHSE #1"), ("2026-01-11", "-40.00", "SHELL OIL")],
    )
    r = await client.post(
        f"{RULES}/preview",
        json={"payee": {"op": "contains", "value": "Costco"}},
        headers=await _csrf(client),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["truncated"] is False
    assert [i["description_raw"] for i in body["items"]] == ["COSTCO WHSE #1"]
    assert "tags" in body["items"][0]  # full TransactionOut shape


async def test_preview_caps_at_50_and_flags_truncation(client) -> None:
    await _signup(client)
    await _import_rows(
        client,
        [("2026-01-10", f"-{i + 1}.00", f"COSTCO RUN {i}") for i in range(51)],
    )
    r = await client.post(
        f"{RULES}/preview",
        json={"payee": {"op": "contains", "value": "costco"}},
        headers=await _csrf(client),
    )
    body = r.json()
    assert len(body["items"]) == 50
    assert body["truncated"] is True


async def test_preview_at_exactly_the_cap_is_not_truncated(client) -> None:
    await _signup(client)
    await _import_rows(
        client,
        [("2026-01-10", f"-{i + 1}.00", f"COSTCO RUN {i}") for i in range(50)],
    )
    r = await client.post(
        f"{RULES}/preview",
        json={"payee": {"op": "contains", "value": "costco"}},
        headers=await _csrf(client),
    )
    body = r.json()
    assert len(body["items"]) == 50
    assert body["truncated"] is False


async def test_preview_fills_currency_and_rejects_garbage(client) -> None:
    await _signup(client)
    ok = await client.post(
        f"{RULES}/preview",
        json={"amount": {"op": "equals", "value": 950, "direction": "out"}},
        headers=await _csrf(client),
    )
    assert ok.status_code == 200  # currency filled from primary (USD)
    bad = await client.post(f"{RULES}/preview", json={}, headers=await _csrf(client))
    assert bad.status_code == 400


async def test_list_annotates_each_rule_with_its_matched_count(client, run_jobs) -> None:
    """'matched 12' (F4 Enabler A, #66): how many reviewed decisions this
    rule contributed to — counted from the log's provenance snapshots, so
    it survives anything but the log itself."""
    from test_flywheel_e2e import _account, _commit_csv, _review, _transactions

    await _signup(client)
    cat = await _category(client, "Groceries9")
    r = await _create_rule(client, action_category_id=cat["id"])
    rule_id = r.json()["id"]

    account = await _account(client)
    await _commit_csv(
        client,
        account,
        rows=[
            ("2026-07-01", "-214.90", "COSTCO #482"),
            ("2026-07-02", "-62.10", "COSTCO GAS"),
            ("2026-07-03", "-4.50", "BLUE BOTTLE"),
        ],
    )
    await run_jobs()
    for txn in await _transactions(client):
        assert (await _review(client, txn["id"])).status_code == 200

    listing = (await client.get(RULES)).json()["items"]
    by_id = {item["id"]: item for item in listing}
    assert by_id[rule_id]["matched_count"] == 2

    single = (await client.get(f"{RULES}/{rule_id}")).json()
    assert single["matched_count"] == 2


# --- F4 Enabler B (#67): retro-apply -----------------------------------------


async def _seed_costco_ledger(client, run_jobs):
    """Two reviewed + two unreviewed COSTCO matches, one split parent and
    one transfer member among them, plus a non-match. Returns (category_id,
    txn ids by role)."""
    from test_flywheel_e2e import _account, _commit_csv, _review, _transactions

    cat = await _category(client, "Groceries B")
    account = await _account(client)
    await _commit_csv(
        client,
        account,
        rows=[
            ("2026-07-01", "-214.90", "COSTCO #482"),  # reviewed, plain
            ("2026-07-02", "-62.10", "COSTCO GAS"),  # reviewed, plain
            ("2026-07-03", "-88.00", "COSTCO SPLIT"),  # reviewed, then split
            ("2026-07-04", "-40.00", "COSTCO XFER"),  # reviewed, transfer member
            ("2026-07-05", "-19.75", "COSTCO RUN"),  # unreviewed
            ("2026-07-06", "-33.50", "COSTCO TRIP"),  # unreviewed
            ("2026-07-07", "-4.50", "BLUE BOTTLE"),  # non-match
        ],
    )
    await run_jobs()
    txns = {t["description_raw"]: t for t in await _transactions(client)}
    for name in ("COSTCO #482", "COSTCO GAS", "COSTCO SPLIT", "COSTCO XFER"):
        assert (await _review(client, txns[name]["id"])).status_code == 200
    split_id = txns["COSTCO SPLIT"]["id"]
    r = await client.put(
        f"/api/v1/transactions/{split_id}/splits",
        json=[
            {"amount_minor": -4400, "category_id": None, "memo": "half"},
            {"amount_minor": -4400, "category_id": None, "memo": "other half"},
        ],
        headers=await _csrf(client),
    )
    assert r.status_code == 200, r.text
    xfer_id = txns["COSTCO XFER"]["id"]
    r = await client.post(
        "/api/v1/transfers",
        json={"transaction_ids": [xfer_id]},
        headers=await _csrf(client),
    )
    assert r.status_code in (200, 201), r.text
    return cat["id"], txns


async def test_preview_breaks_matches_down_by_review_state(client, run_jobs) -> None:
    """The consent counts (F4 Enabler B, #67): unreviewed / reviewed /
    skipped — splits and transfer members keep their structure."""
    await _signup(client)
    await _seed_costco_ledger(client, run_jobs)

    r = await client.post(
        f"{RULES}/preview",
        json={"payee": {"op": "contains", "value": "costco"}},
        headers=await _csrf(client),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unreviewed_count"] == 2
    assert body["reviewed_count"] == 2
    assert body["skipped_count"] == 2


async def test_apply_unreviewed_refreshes_the_backlog_and_leaves_reviewed_alone(
    client, run_jobs
) -> None:
    """The default tier (F4 Enabler B, #67): matching unreviewed
    transactions are re-proposed under the rule; reviewed history stays as
    the user filed it. Rules still never write user data."""
    from test_flywheel_e2e import _transactions

    await _signup(client)
    cat_id, _txns = await _seed_costco_ledger(client, run_jobs)

    r = await _create_rule(client, action_category_id=cat_id, apply="unreviewed")
    assert r.status_code == 201, r.text
    applied = r.json()["applied"]
    assert applied["tier"] == "unreviewed"
    assert applied["refreshed_unreviewed"] == 2
    assert applied["recategorized_reviewed"] == 0
    assert applied["skipped"] == 2
    assert applied["batch_id"] is None

    await run_jobs()
    after = {t["description_raw"]: t for t in await _transactions(client)}
    for name in ("COSTCO RUN", "COSTCO TRIP"):
        proposal = after[name]["proposal"]
        assert proposal is not None
        assert proposal["provenance"] == "rule"
        assert proposal["category"]["id"] == cat_id
    for name in ("COSTCO #482", "COSTCO GAS"):
        assert after[name]["category"] is None
        assert after[name]["reviewed_at"] is not None


async def test_apply_full_recategorizes_reviewed_in_place_as_one_logged_batch(
    client, run_jobs
) -> None:
    """The full tier (F4 Enabler B, #67): a user-consented bulk edit —
    reviewed matches recategorized in place, still reviewed, nothing back
    to the Inbox; one batch of per-transaction decision entries; splits
    and transfer members keep their structure."""
    from test_flywheel_e2e import _transactions

    await _signup(client)
    cat_id, _txns = await _seed_costco_ledger(client, run_jobs)

    r = await _create_rule(
        client, action_category_id=cat_id, action_rename_to="Costco", apply="full"
    )
    assert r.status_code == 201, r.text
    applied = r.json()["applied"]
    assert applied["tier"] == "full"
    assert applied["refreshed_unreviewed"] == 2
    assert applied["recategorized_reviewed"] == 2
    assert applied["skipped"] == 2
    assert applied["batch_id"] is not None

    after = {t["description_raw"]: t for t in await _transactions(client)}
    for name in ("COSTCO #482", "COSTCO GAS"):
        txn = after[name]
        assert txn["category"]["id"] == cat_id
        assert txn["reviewed_at"] is not None, "stays reviewed — nothing returns to the Inbox"
        assert txn["display_name"] == "Costco"
        assert "bulk" in {t["name"] for t in txn["tags"]}
    assert after["COSTCO SPLIT"]["category"] is None, "split parent keeps its structure"
    assert after["COSTCO XFER"]["category"] is None, "transfer member keeps its structure"

    unreviewed_count = (await client.get("/api/v1/transactions/unreviewed-count")).json()
    entries = (await client.get("/api/v1/correction-log")).json()["items"]
    batch = [e for e in entries if e["batch_id"] == applied["batch_id"]]
    assert len(batch) == 2
    for e in batch:
        assert e["kind"] == "decision"
        assert e["actor"] == "user"
        assert e["accepted_untouched"] is False
        assert e["decision_category_id"] == cat_id
        assert e["decision_category_name"] == "Groceries B"
        assert e["decision_display_name"] == "Costco"
        assert "bulk" in e["decision_tags"]
    singles = [e for e in entries if e["batch_id"] is None and e["kind"] == "decision"]
    assert len(singles) == 4, "the original reviews stay unbatched"
    assert unreviewed_count["count"] == 3, "unreviewed backlog unchanged (2 costco + blue bottle)"


async def test_transfer_marking_rules_are_forward_only(client) -> None:
    """A rule sees one transaction and cannot re-interpret settled history
    (F4 Enabler B, #67): no retro tiers for mark-transfer rules."""
    await _signup(client)
    for tier in ("unreviewed", "full"):
        r = await _create_rule(client, action_add_tags=[], action_mark_transfer=True, apply=tier)
        assert r.status_code == 400, r.text
    r = await _create_rule(client, action_add_tags=[], action_mark_transfer=True, apply="forward")
    assert r.status_code == 201, r.text
    assert r.json()["applied"] is None


async def _mint_proposed_rule(client, category_id: str):
    """Seed a promotion-minted PROPOSED rule the API way: three consistent
    filings of one payee (promotion is synchronous with review)."""
    from test_flywheel_e2e import _account, _review

    account = await _account(client)
    last_response = None
    for index in range(3):
        r = await client.post(
            "/api/v1/transactions",
            json={
                "account_id": account,
                "date": f"2026-07-0{index + 1}",
                "amount_minor": -450 - index,
                "description": "BLUE BOTTLE",
            },
            headers=await _csrf(client),
        )
        assert r.status_code == 201, r.text
        last_response = await _review(client, r.json()["id"], {"category_id": category_id})
        assert last_response.status_code == 200, last_response.text
    proposed = last_response.json()["proposed_rule"]
    assert proposed is not None, "third consistent filing should mint a proposed rule"
    return account, proposed


async def test_accepting_a_proposed_rule_may_retro_apply(client, run_jobs) -> None:
    """Acceptance IS the creation consent (CONTEXT.md: Retro-apply): the
    proposed->active PATCH may carry a tier; origin stays promotion."""
    await _signup(client)
    cat = await _category(client, "Coffee Shops")
    account, rule = await _mint_proposed_rule(client, cat["id"])

    # An unreviewed backlog of the same payee, swept under old proposals.
    for day in ("04", "05"):
        r = await client.post(
            "/api/v1/transactions",
            json={
                "account_id": account,
                "date": f"2026-07-{day}",
                "amount_minor": -800,
                "description": "BLUE BOTTLE",
            },
            headers=await _csrf(client),
        )
        assert r.status_code == 201, r.text
    await run_jobs()

    r = await client.patch(
        f"{RULES}/{rule['id']}",
        json={"status": "active", "apply": "unreviewed"},
        headers=await _csrf(client),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["origin"] == "promotion"
    assert body["applied"]["tier"] == "unreviewed"
    assert body["applied"]["refreshed_unreviewed"] == 2
    assert body["applied"]["recategorized_reviewed"] == 0
    assert body["applied"]["batch_id"] is None

    await run_jobs()
    txns = (await client.get("/api/v1/transactions?reviewed=false")).json()["items"]
    assert len(txns) == 2
    for txn in txns:
        assert txn["proposal"]["provenance"] == "rule"
        assert txn["proposal"]["category"]["id"] == cat["id"]


async def test_apply_is_refused_outside_the_acceptance_transition(client) -> None:
    """The creation-time law holds: apply rides proposed->active only."""
    await _signup(client)
    cat = await _category(client, "Groceries X")
    r = await _create_rule(client, action_category_id=cat["id"])
    active_rule = r.json()

    # An already-active rule never re-offers retro-apply…
    r = await client.patch(
        f"{RULES}/{active_rule['id']}",
        json={"apply": "unreviewed"},
        headers=await _csrf(client),
    )
    assert r.status_code == 400, r.text

    # …not even alongside other edits.
    r = await client.patch(
        f"{RULES}/{active_rule['id']}",
        json={"status": "disabled", "apply": "full"},
        headers=await _csrf(client),
    )
    assert r.status_code == 400, r.text


async def test_patch_with_condition_and_category_keeps_both(client) -> None:
    """The identity-map law (queries before mutations): resolving the
    category must not clobber the just-set condition."""
    await _signup(client)
    cat = await _category(client, "Groceries Y")
    other = await _category(client, "Dining Y")
    rule = (await _create_rule(client, action_category_id=cat["id"])).json()

    r = await client.patch(
        f"{RULES}/{rule['id']}",
        json={
            "condition": {"payee": {"op": "contains", "value": "wholefoods"}},
            "action_category_id": other["id"],
        },
        headers=await _csrf(client),
    )
    assert r.status_code == 200, r.text
    assert r.json()["condition"]["payee"]["value"] == "wholefoods"
    assert r.json()["action_category"]["id"] == other["id"]
