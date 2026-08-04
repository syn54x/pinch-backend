"""M13 CP4 seam: the MX doorbell receiver at the HTTP boundary (issue
#91; PRD #86; ADR 0009).

The front door is the per-instance secret URL segment — MX signs nothing,
so there is no cryptography to fake: tests POST exactly what MX would.
The doorbell-only law is proven behaviorally — a rung doorbell enqueues
exactly the job a clicked Refresh would, payload contents (statuses,
counts, inline transaction objects) change nothing, and every rejection
is an undifferentiated 401 that enqueues nothing.
"""

import json

import pytest

from pinch_backend import providers
from pinch_backend.models import Connection, ConnectionProvider
from test_mx_sync_api import FakeMXSyncProvider

SECRET = "wh-secret-fixture"
WEBHOOK = f"/webhooks/mx/{SECRET}"
CONNECTIONS = "/api/v1/connections"

PASSWORD = "correct horse battery staple"


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email: str = "taylor@example.com") -> None:
    response = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "display_name": "Taylor"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text


def _doorbell(webhook_type: str, member_guid: str | None = "MBR-9", **extra) -> bytes:
    """An MX-shaped ring (docs-derived: docs.mx.com/resources/webhooks) —
    ``type`` names the family, ``member_guid`` names who rang."""
    payload: dict = {"type": webhook_type, "user_guid": "USR-1", **extra}
    if member_guid is not None:
        payload["member_guid"] = member_guid
    return json.dumps(payload).encode()


async def _ring(client, body: bytes, path: str = WEBHOOK):
    """POST like MX does: raw JSON body, no session, no CSRF header, no
    signature — the secret path segment is the whole authentication."""
    return await client.post(path, content=body, headers={"content-type": "application/json"})


@pytest.fixture
def mx_provider(monkeypatch):
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "mx_client_id", "test-mx-client-id")
    monkeypatch.setattr(settings, "mx_api_key", "test-mx-api-key")
    monkeypatch.setattr(settings, "mx_webhook_secret", SECRET)
    fake = FakeMXSyncProvider()
    monkeypatch.setattr(providers, "get_provider", fake.materialize)
    return fake


@pytest.fixture
async def connection(client, db, mx_provider, job_connector) -> dict:
    """A connected MX member with provider_item_id ``MBR-9``, the job
    queue drained of the connect flow's own enqueues."""
    await _signup(client)
    session = await client.post(
        f"{CONNECTIONS}/connect-session", json={"provider": "mx"}, headers=await _csrf(client)
    )
    assert session.status_code == 201, session.text  # ensures the enrollment
    response = await client.post(
        CONNECTIONS, json={"provider": "mx", "token": "MBR-9"}, headers=await _csrf(client)
    )
    assert response.status_code == 201, response.text
    job_connector.reset()
    return response.json()


def _queued(job_connector) -> list[dict]:
    return list(job_connector.jobs.values())


# --- Rung doorbells dispatch ------------------------------------------------------


async def test_an_aggregation_doorbell_enqueues_the_connection_sync(
    client, connection, job_connector
) -> None:
    """The tracer: MX finishes a nightly aggregation and rings; Pinch runs
    the same sync a clicked Refresh would — same job, same per-connection
    lock — and 200s. The payload's counts are pointers we never read."""
    body = _doorbell(
        "AGGREGATION",
        action="member_data_updated",
        transactions_created_count=8,
        transactions_updated_count=2,
    )

    response = await _ring(client, body)

    assert response.status_code == 200, response.text
    jobs = _queued(job_connector)
    assert [j["task_name"] for j in jobs] == ["sync.sync_connection"]
    assert jobs[0]["args"] == {"connection_id": connection["id"]}
    assert jobs[0]["lock"] == f"sync:{connection['id']}"


async def test_initial_data_ready_rings_the_same_doorbell(
    client, connection, job_connector
) -> None:
    """The first-aggregation pointer is the aggregation-completed family
    too: one sync job, nothing MX-special past the door."""
    response = await _ring(client, _doorbell("INITIAL_DATA_READY", action="initial_data_ready"))

    assert response.status_code == 200, response.text
    assert [j["task_name"] for j in _queued(job_connector)] == ["sync.sync_connection"]


async def test_a_connection_status_change_dispatches_a_sync_and_writes_no_status(
    client, connection, job_connector
) -> None:
    """The no-new-states law against an unsigned payload: the ring says
    DENIED, but connection health only ever changes when the sync path's
    own status probe reads MX — the payload's claim writes nothing, the
    enqueued sync is how we learn."""
    body = _doorbell(
        "CONNECTION_STATUS",
        action="CHANGED",
        connection_status="DENIED",
        connection_status_message="The credentials entered do not match",
    )

    response = await _ring(client, body)

    assert response.status_code == 200, response.text
    assert [j["task_name"] for j in _queued(job_connector)] == ["sync.sync_connection"]
    view = (await client.get(f"{CONNECTIONS}/{connection['id']}")).json()
    assert view["status"] == "active"  # the payload never wrote health
    assert view["error_detail"] is None


async def test_a_ring_carries_no_session_and_no_cookies(client, connection, job_connector) -> None:
    """MX is not a browser: the sessionless, cookie-less POST — exactly
    what production receives — authenticates and dispatches."""
    client.cookies.clear()

    response = await _ring(client, _doorbell("AGGREGATION", action="member_data_updated"))

    assert response.status_code == 200, response.text
    assert [j["task_name"] for j in _queued(job_connector)] == ["sync.sync_connection"]


async def test_duplicate_rings_both_enqueue_and_the_lock_serializes(
    client, connection, job_connector
) -> None:
    """Duplicate delivery is the norm, priced in: each ring enqueues; the
    per-connection lock is what serializes them (ADR-0006)."""
    body = _doorbell("AGGREGATION", action="member_data_updated")
    assert (await _ring(client, body)).status_code == 200
    assert (await _ring(client, body)).status_code == 200

    jobs = _queued(job_connector)
    assert len(jobs) == 2
    assert {j["lock"] for j in jobs} == {f"sync:{connection['id']}"}


# --- The front door: undifferentiated 401s ----------------------------------------


async def test_a_wrong_secret_answers_401(client, connection, job_connector) -> None:
    response = await _ring(client, _doorbell("AGGREGATION"), path="/webhooks/mx/wrong-secret")

    assert response.status_code == 401
    assert _queued(job_connector) == []


async def test_a_missing_secret_answers_401(client, connection, job_connector) -> None:
    """The bare path is the same undifferentiated 401 as a wrong secret —
    never a different kind of door."""
    response = await _ring(client, _doorbell("AGGREGATION"), path="/webhooks/mx")

    assert response.status_code == 401
    assert _queued(job_connector) == []


async def test_an_mx_unconfigured_instance_answers_401(client, db, job_connector) -> None:
    """No bypass in any configuration: without MX there is no secret to
    compare against, so every ring is a 401 — never an unverified
    dispatch, and never a hint about why."""
    response = await _ring(client, _doorbell("AGGREGATION"))

    assert response.status_code == 401
    assert _queued(job_connector) == []


# --- Authenticated but not actionable: log-and-200 --------------------------------


async def test_a_data_carrying_family_answers_200_and_enqueues_nothing(
    client, connection, job_connector
) -> None:
    """MX's Transactions webhook carries full transaction objects inline —
    the hardest temptation ADR 0009 rejects: unsigned delivery must never
    be load-bearing for data, so the family is acknowledged and ignored
    (deletions included: the sync path's window diff re-derives them)."""
    body = _doorbell(
        "TRANSACTIONS",
        action="deleted",
        transaction={"guid": "TRN-1", "amount": 12.5, "description": "GHOST"},
    )

    response = await _ring(client, body)

    assert response.status_code == 200
    assert _queued(job_connector) == []


async def test_an_unknown_type_answers_200_and_enqueues_nothing(
    client, connection, job_connector
) -> None:
    """The family map is docs-derived (no live capture without dashboard
    registration), so tolerance is the contract: anything unrecognized is
    acknowledged — a retry would not go better."""
    response = await _ring(client, _doorbell("SOME_FUTURE_FAMILY", action="whatever"))

    assert response.status_code == 200
    assert _queued(job_connector) == []


async def test_an_unparseable_body_answers_200(client, connection, job_connector) -> None:
    for body in (b"not json at all", json.dumps(["a", "list"]).encode()):
        response = await _ring(client, body)
        assert response.status_code == 200, body
    assert _queued(job_connector) == []


async def test_a_missing_member_guid_answers_200_and_enqueues_nothing(
    client, connection, job_connector
) -> None:
    response = await _ring(client, _doorbell("AGGREGATION", member_guid=None))

    assert response.status_code == 200
    assert _queued(job_connector) == []


async def test_an_unmatched_member_answers_200_and_enqueues_nothing(
    client, connection, job_connector
) -> None:
    """A ring for a member we no longer hold: acknowledged, never
    confirmed — the response tells a prober nothing about which guids
    exist."""
    response = await _ring(client, _doorbell("AGGREGATION", member_guid="MBR-ghost"))

    assert response.status_code == 200
    assert _queued(job_connector) == []


async def test_the_lookup_is_scoped_to_mx_connections(client, connection, job_connector) -> None:
    """(provider, provider_item_id) is the scope (M13 CP1): a Plaid
    connection carrying the same id string must never answer MX's door."""
    row = await Connection.where(lambda c, cid=connection["id"]: c.id == cid).first()
    assert row is not None
    row.provider = ConnectionProvider.PLAID
    await row.save()

    response = await _ring(client, _doorbell("AGGREGATION", member_guid="MBR-9"))

    assert response.status_code == 200
    assert _queued(job_connector) == []
