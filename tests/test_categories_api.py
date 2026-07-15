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
