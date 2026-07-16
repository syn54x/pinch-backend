"""The correction-log read surface (M5 CP3, #21): parity — eval export is
a consumer of this endpoint."""

import uuid

from pinch_backend.models import (
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Ledger,
    User,
)

PASSWORD = "correct horse battery staple"


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email="taylor@example.com") -> None:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "display_name": "Taylor"},
        headers=await _csrf(client),
    )
    assert resp.status_code == 201, resp.text


async def _ledger_for(email="taylor@example.com") -> Ledger:
    user = await User.where(lambda u, e=email: u.email == e).first()
    member = (await user.memberships.all())[0]
    return await Ledger.get(member.ledger_id)


async def _seed_entries(email="taylor@example.com") -> Ledger:
    ledger = await _ledger_for(email)
    txn_id = uuid.uuid7()
    decision = await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=txn_id,
        kind=CorrectionKind.DECISION,
        actor=CorrectionActor.USER,
        input_payee="starbucks",
        decision_tags=["treat"],
    )
    await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=txn_id,
        kind=CorrectionKind.VOID,
        actor=CorrectionActor.USER,
        voids=decision.id,
        void_reason="import undone",
    )
    await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=uuid.uuid7(),
        kind=CorrectionKind.DECISION,
        actor=CorrectionActor.AUTO,
    )
    return ledger


async def test_list_pages_and_filters(client, db) -> None:
    await _signup(client)
    await _seed_entries()

    everything = (await client.get("/api/v1/correction-log")).json()
    assert len(everything["items"]) == 3
    assert everything["next_cursor"] is None

    voids = (await client.get("/api/v1/correction-log?kind=void")).json()["items"]
    assert len(voids) == 1
    assert voids[0]["void_reason"] == "import undone"
    assert voids[0]["voids"] is not None

    autos = (await client.get("/api/v1/correction-log?actor=auto")).json()["items"]
    assert len(autos) == 1

    tid = everything["items"][0]["transaction_id"]
    scoped = (await client.get(f"/api/v1/correction-log?transaction_id={tid}")).json()["items"]
    assert all(e["transaction_id"] == tid for e in scoped)


async def test_log_is_ledger_scoped(client, db) -> None:
    await _signup(client)
    await _seed_entries()
    # A second user sees an empty log, not ours.
    from litestar.testing import AsyncTestClient

    from pinch_backend.api.app import create_app

    async with AsyncTestClient(
        create_app(manage_database=False), base_url="https://testserver.local"
    ) as other:
        await _signup(other, email="other@example.com")
        items = (await other.get("/api/v1/correction-log")).json()["items"]
        assert items == []


async def test_multi_page_cursor_walk(client, db) -> None:
    """CP3 debt: the id-keyset cursor walks this endpoint page by page."""
    await _signup(client)
    ledger = await _ledger_for()
    for _ in range(3):
        await CorrectionLogEntry.create(
            ledger=ledger,
            transaction_id=uuid.uuid7(),
            kind=CorrectionKind.DECISION,
            actor=CorrectionActor.USER,
        )
    first = (await client.get("/api/v1/correction-log", params={"limit": 2})).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"] is not None
    second = (
        await client.get(
            "/api/v1/correction-log", params={"limit": 2, "cursor": first["next_cursor"]}
        )
    ).json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    seen = {e["id"] for e in first["items"]} | {e["id"] for e in second["items"]}
    assert len(seen) == 3  # no overlap, nothing dropped


async def test_combined_filters(client, db) -> None:
    """CP3 debt: transaction_id + actor + kind compose (AND semantics)."""
    await _signup(client)
    ledger = await _ledger_for()
    txn_id = uuid.uuid7()
    other_id = uuid.uuid7()
    await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=txn_id,
        kind=CorrectionKind.DECISION,
        actor=CorrectionActor.USER,
    )
    await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=other_id,
        kind=CorrectionKind.DECISION,
        actor=CorrectionActor.USER,
    )
    params = {"transaction_id": str(txn_id), "actor": "user", "kind": "decision"}
    items = (await client.get("/api/v1/correction-log", params=params)).json()["items"]
    assert [e["transaction_id"] for e in items] == [str(txn_id)]
    # Same transaction_id + kind, but actor no longer matches: AND, not OR.
    none = (await client.get("/api/v1/correction-log", params=params | {"actor": "auto"})).json()[
        "items"
    ]
    assert none == []
