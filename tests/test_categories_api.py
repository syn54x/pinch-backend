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


async def test_reparent_to_a_new_valid_parent_succeeds(client) -> None:
    await _signup(client)
    a = (await _create(client, "A")).json()
    b = (await _create(client, "B")).json()
    leaf = (await _create(client, "Leaf", parent_id=a["id"])).json()
    r = await client.patch(
        f"{CATEGORIES}/{leaf['id']}",
        json={"parent_id": b["id"], "reparent": True},
        headers=await _csrf(client),
    )
    assert r.status_code == 200, r.text
    assert r.json()["parent_id"] == b["id"]


async def test_reparent_to_top_level_succeeds(client) -> None:
    await _signup(client)
    a = (await _create(client, "A")).json()
    leaf = (await _create(client, "Leaf", parent_id=a["id"])).json()
    r = await client.patch(
        f"{CATEGORIES}/{leaf['id']}",
        json={"parent_id": None, "reparent": True},
        headers=await _csrf(client),
    )
    assert r.status_code == 200, r.text
    assert r.json()["parent_id"] is None


async def test_reparent_a_subtree_past_the_cap_is_rejected(client) -> None:
    await _signup(client)
    a = (await _create(client, "A")).json()
    b = (await _create(client, "B")).json()
    # A has a child, so moving A under B would push the grandchild to depth 3.
    await _create(client, "AChild", parent_id=a["id"])
    r = await client.patch(
        f"{CATEGORIES}/{a['id']}",
        json={"parent_id": b["id"], "reparent": True},
        headers=await _csrf(client),
    )
    assert r.status_code == 400


async def test_delete_is_blocked_by_targeting_rules(client) -> None:
    await _signup(client)
    cat = (await _create(client, "RuleTarget")).json()
    rule = await client.post(
        "/api/v1/rules",
        json={
            "condition": {"payee": {"op": "contains", "value": "x"}},
            "action_category_id": cat["id"],
        },
        headers=await _csrf(client),
    )
    assert rule.status_code == 201, rule.text
    r = await client.request(
        "DELETE",
        f"{CATEGORIES}/{cat['id']}",
        json={"reassign_to": None},
        headers=await _csrf(client),
    )
    assert r.status_code == 409
    assert rule.json()["id"] in r.json()["extra"]["rules"]


async def test_delete_succeeds_after_rule_retargeted(client) -> None:
    await _signup(client)
    cat = (await _create(client, "RuleTarget2")).json()
    other = (await _create(client, "Elsewhere")).json()
    rule = (
        await client.post(
            "/api/v1/rules",
            json={
                "condition": {"payee": {"op": "contains", "value": "y"}},
                "action_category_id": cat["id"],
            },
            headers=await _csrf(client),
        )
    ).json()
    await client.patch(
        f"/api/v1/rules/{rule['id']}",
        json={"action_category_id": other["id"]},
        headers=await _csrf(client),
    )
    r = await client.request(
        "DELETE",
        f"{CATEGORIES}/{cat['id']}",
        json={"reassign_to": None},
        headers=await _csrf(client),
    )
    assert r.status_code == 204, r.text


async def _seed_proposal_targeting(category_id: str):
    """A transaction + pending proposal aimed at ``category_id``. Model-layer
    on purpose: the pending proposal is pipeline-owned state and the surface
    under test is DELETE /categories."""
    import uuid as _uuid
    from datetime import date as _date

    from pinch_backend.models import (
        Account,
        AccountKind,
        Category,
        Ledger,
        Proposal,
        ProposalProvenance,
        Transaction,
    )

    ledger = (await Ledger.all())[0]
    account = await Account.create(ledger=ledger, kind=AccountKind.DEPOSITORY, label="Chk")
    txn = await Transaction.create(
        ledger=ledger,
        account=account,
        date=_date(2026, 7, 1),
        amount_minor=-100,
        currency="USD",
        description_raw="X",
        description_normalized="x",
        fingerprint=f"fp-{_uuid.uuid4().hex[:8]}",
    )
    target = await Category.get(_uuid.UUID(category_id))
    await Proposal.create(
        ledger=ledger,
        transaction=txn,
        category=target,
        provenance=ProposalProvenance.RULE,
        provenance_detail={"rule_ids": ["r"]},
    )
    return txn


async def test_delete_repoints_pending_proposals(client) -> None:
    from pinch_backend.models import Proposal, ProposalProvenance

    await _signup(client)
    a = (
        await client.post(
            "/api/v1/categories", json={"name": "Doomed Q"}, headers=await _csrf(client)
        )
    ).json()
    b = (
        await client.post(
            "/api/v1/categories", json={"name": "Target Q"}, headers=await _csrf(client)
        )
    ).json()
    txn = await _seed_proposal_targeting(a["id"])

    resp = await client.request(
        "DELETE",
        f"/api/v1/categories/{a['id']}",
        json={"reassign_to": b["id"]},
        headers=await _csrf(client),
    )
    assert resp.status_code == 204
    p = await Proposal.where(lambda p, tid=txn.id: p.transaction_id == tid).first()
    assert str(p.category_id) == b["id"]
    assert p.provenance is ProposalProvenance.RULE  # re-point keeps provenance


async def _seed_transaction_in(category_id: str | None):
    """A transaction directly categorized under ``category_id`` (or
    uncategorized when None), for delete-reassignment tests. Model-layer: an
    existing user-categorized transaction, not the pipeline's proposal state."""
    import uuid as _uuid
    from datetime import date as _date

    from pinch_backend.models import Account, AccountKind, Category, Ledger, Transaction

    ledger = (await Ledger.all())[0]
    account = await Account.create(ledger=ledger, kind=AccountKind.DEPOSITORY, label="Chk")
    category = await Category.get(_uuid.UUID(category_id)) if category_id else None
    txn = await Transaction.create(
        ledger=ledger,
        account=account,
        category=category,
        date=_date(2026, 7, 1),
        amount_minor=-100,
        currency="USD",
        description_raw="X",
        description_normalized="x",
        fingerprint=f"fp-{_uuid.uuid4().hex[:8]}",
    )
    return txn


async def test_delete_reassign_to_self_is_rejected(client) -> None:
    """Finding 1 regression: reassigning a category's transactions to itself
    must not be treated as a no-op that lets the cascade delete fire —
    every FK defaults to ferro's CASCADE, so category.delete() would take the
    transaction (and its tags/proposals) down with it."""
    from pinch_backend.models import Transaction

    await _signup(client)
    cat = (await _create(client, "SelfTarget")).json()
    txn = await _seed_transaction_in(cat["id"])

    r = await client.request(
        "DELETE",
        f"{CATEGORIES}/{cat['id']}",
        json={"reassign_to": cat["id"]},
        headers=await _csrf(client),
    )
    assert r.status_code == 409, r.text

    still_there = await client.get(f"{CATEGORIES}/{cat['id']}")
    assert still_there.status_code == 200

    reloaded = await Transaction.get(txn.id)
    assert str(reloaded.category_id) == cat["id"]  # ty: ignore[unresolved-attribute]


async def test_delete_reassigns_transactions_to_target(client) -> None:
    """Finding 5: the delete's core effect — reassignment of live
    transactions — was untested; the prior test used a category with zero
    transactions."""
    from pinch_backend.models import Transaction

    await _signup(client)
    src = (await _create(client, "SrcWithTxns")).json()
    dst = (await _create(client, "DstWithTxns")).json()
    t1 = await _seed_transaction_in(src["id"])
    t2 = await _seed_transaction_in(src["id"])

    r = await client.request(
        "DELETE",
        f"{CATEGORIES}/{src['id']}",
        json={"reassign_to": dst["id"]},
        headers=await _csrf(client),
    )
    assert r.status_code == 204, r.text

    for txn in (t1, t2):
        reloaded = await Transaction.get(txn.id)
        assert str(reloaded.category_id) == dst["id"]  # ty: ignore[unresolved-attribute]


async def test_delete_with_null_disposition_uncategorizes_transactions(client) -> None:
    """Finding 5: the null-disposition path of delete's core effect."""
    from pinch_backend.models import Transaction

    await _signup(client)
    src = (await _create(client, "SrcToUncategorize")).json()
    t1 = await _seed_transaction_in(src["id"])
    t2 = await _seed_transaction_in(src["id"])

    r = await client.request(
        "DELETE",
        f"{CATEGORIES}/{src['id']}",
        json={"reassign_to": None},
        headers=await _csrf(client),
    )
    assert r.status_code == 204, r.text

    for txn in (t1, t2):
        reloaded = await Transaction.get(txn.id)
        assert reloaded.category_id is None  # ty: ignore[unresolved-attribute]


async def test_create_duplicate_sibling_name_is_rejected(client) -> None:
    """Finding 5: _assert_sibling_name_free's create-time path was untested
    via the API."""
    await _signup(client)
    await _create(client, "Coffee")
    r = await _create(client, "Coffee")
    assert r.status_code == 400, r.text


async def test_create_duplicate_sibling_name_case_and_whitespace_variant_is_rejected(
    client,
) -> None:
    """Finding 6: sibling comparison must trim + casefold, matching the Tag
    precedent, so "Coffee" and " coffee " collide."""
    await _signup(client)
    await _create(client, "Coffee")
    r = await _create(client, " coffee ")
    assert r.status_code == 400, r.text


async def test_rename_onto_sibling_name_case_variant_is_rejected(client) -> None:
    """Finding 5/6: _assert_sibling_name_free's rename-time path, with a
    case-variant collision."""
    await _signup(client)
    await _create(client, "Foo")
    b = (await _create(client, "Bar")).json()
    r = await client.patch(
        f"{CATEGORIES}/{b['id']}",
        json={"name": "foo"},
        headers=await _csrf(client),
    )
    assert r.status_code == 400, r.text


async def test_update_parent_id_without_reparent_flag_is_rejected(client) -> None:
    """Finding 15: parent_id without reparent: true used to be a silent
    200 no-op; it must now be rejected outright."""
    await _signup(client)
    a = (await _create(client, "A")).json()
    b = (await _create(client, "B")).json()
    r = await client.patch(
        f"{CATEGORIES}/{b['id']}",
        json={"parent_id": a["id"]},
        headers=await _csrf(client),
    )
    assert r.status_code == 400, r.text


async def test_delete_with_null_disposition_empties_proposals(client) -> None:
    from pinch_backend.models import Proposal, ProposalProvenance

    await _signup(client)
    a = (
        await client.post(
            "/api/v1/categories", json={"name": "Doomed R"}, headers=await _csrf(client)
        )
    ).json()
    txn = await _seed_proposal_targeting(a["id"])

    resp = await client.request(
        "DELETE",
        f"/api/v1/categories/{a['id']}",
        json={"reassign_to": None},
        headers=await _csrf(client),
    )
    assert resp.status_code == 204
    p = await Proposal.where(lambda p, tid=txn.id: p.transaction_id == tid).first()
    assert p.category_id is None
    assert p.provenance is ProposalProvenance.NONE
    assert p.provenance_detail is None
