"""M11 CP3 seam: probe-then-decide reconciliation (issue #81; ADR 0008;
provider-aware since M13 CP4, issue #91; ADR 0009).

The safety net that isn't polling: fakes script the probe answers and the
verdicts are proven behaviorally — URL drift re-registers and syncs
(Plaid only: MX registration is dashboard-side and unprobeable), a missed
doorbell syncs loudly, a quiet connection costs one free probe and
nothing else. Broken connections are never probed; the 24h cadence is
bookkept on last_reconciled_at.
"""

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet

from pinch_backend import providers
from pinch_backend.models import Connection, ConnectionStatus, utcnow
from test_investments_api import FakeInvestmentsProvider
from test_mx_sync_api import FakeMXSyncProvider

CONNECTIONS = "/api/v1/connections"
OUR_URL = "https://pinch.example/webhooks/plaid"

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


class FakeReconcileProvider(FakeInvestmentsProvider):
    """The investments fake grown the reconciler's two calls: a scripted
    /item/get answer and a captured /item/webhook/update — assertions
    happen at this seam, exactly like every provider call."""

    def __init__(self) -> None:
        super().__init__()
        self.item_state = providers.ItemState(webhook=OUR_URL)
        self.item_state_failure: providers.ProviderError | None = None
        self.probes = 0
        self.webhook_updates: list[str] = []

    async def get_item_state(self) -> providers.ItemState:
        self.probes += 1
        if self.item_state_failure is not None:
            raise self.item_state_failure
        return self.item_state

    async def update_webhook(self, url: str) -> None:
        self.webhook_updates.append(url)


@pytest.fixture
def fake_provider(monkeypatch):
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "plaid_client_id", "test-client-id")
    monkeypatch.setattr(settings, "plaid_secret", "test-secret")
    monkeypatch.setattr(settings, "secret_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "plaid_webhook_url", OUR_URL)
    fake = FakeReconcileProvider()
    monkeypatch.setattr(providers, "get_provider", fake.materialize)
    return fake


@pytest.fixture
async def connection(client, db, fake_provider, job_connector) -> Connection:
    """A connected Item, stale enough to be due: last synced 2 days ago,
    never reconciled, job queue drained of the connect flow's enqueues."""
    await _signup(client)
    response = await client.post(
        CONNECTIONS, json={"provider": "plaid", "token": "public-abc"}, headers=await _csrf(client)
    )
    assert response.status_code == 201, response.text
    job_connector.reset()
    connection_id = response.json()["id"]
    row = await Connection.where(lambda c, cid=connection_id: c.id == cid).first()
    row.last_synced_at = utcnow() - timedelta(days=2)
    await row.save()
    return row


def _sync_jobs(job_connector) -> list[dict]:
    return [j for j in job_connector.jobs.values() if j["task_name"] == "sync.sync_connection"]


async def _fresh(connection: Connection) -> Connection:
    return await Connection.where(lambda c, cid=connection.id: c.id == cid).first()


# --- The three verdicts ----------------------------------------------------------


async def test_url_drift_reregisters_and_syncs(connection, fake_provider, job_connector) -> None:
    """Verdict (a): an empty item.webhook — the pre-M11 retrofit shape —
    re-registers through /item/webhook/update and enqueues a sync."""
    from pinch_backend.reconcile import reconcile_pass

    fake_provider.item_state = providers.ItemState(webhook="")

    count = await reconcile_pass()

    assert count == 1
    assert fake_provider.webhook_updates == [OUR_URL]
    jobs = _sync_jobs(job_connector)
    assert len(jobs) == 1 and jobs[0]["args"] == {"connection_id": str(connection.id)}
    assert (await _fresh(connection)).last_reconciled_at is not None


async def test_foreign_url_counts_as_drift(connection, fake_provider, job_connector) -> None:
    """A rotated tunnel or changed deploy domain is the same verdict."""
    from pinch_backend.reconcile import reconcile_pass

    fake_provider.item_state = providers.ItemState(webhook="https://old-tunnel.example/hook")

    await reconcile_pass()

    assert fake_provider.webhook_updates == [OUR_URL]
    assert len(_sync_jobs(job_connector)) == 1


async def test_plaid_updated_after_us_syncs_and_warns(
    connection, fake_provider, job_connector
) -> None:
    """Verdict (b): Plaid's status postdating our last sync means a
    doorbell went missing — enqueue the normal sync (loudly logged)."""
    from pinch_backend.reconcile import reconcile_pass

    fake_provider.item_state = providers.ItemState(
        webhook=OUR_URL, transactions_updated_at=utcnow() - timedelta(hours=1)
    )

    await reconcile_pass()

    assert fake_provider.webhook_updates == []  # registration was fine
    assert len(_sync_jobs(job_connector)) == 1
    assert (await _fresh(connection)).last_reconciled_at is not None


async def test_the_missed_verdict_is_what_warns(connection, fake_provider) -> None:
    """The loud warning rides the webhook_missed verdict (PRD #77 story
    14) — pinned at the verdict seam."""
    from pinch_backend.reconcile import reconcile_connection

    fake_provider.item_state = providers.ItemState(
        webhook=OUR_URL, transactions_updated_at=utcnow() - timedelta(hours=1)
    )
    assert await reconcile_connection(connection) == "webhook_missed"


async def test_investments_price_churn_alone_is_quiet(
    connection, fake_provider, job_connector
) -> None:
    """The stated boundary: HOLDINGS extraction moves near-daily on price
    churn and nothing on our side stamps an investments sync, so the
    missed-webhook verdict reads transactions only — an investments-only
    timestamp must not cry wolf every market day."""
    from pinch_backend.reconcile import reconcile_pass

    fake_provider.item_state = providers.ItemState(
        webhook=OUR_URL,
        transactions_updated_at=utcnow() - timedelta(days=3),
        investments_updated_at=utcnow() - timedelta(hours=1),
    )

    await reconcile_pass()

    assert _sync_jobs(job_connector) == []
    assert (await _fresh(connection)).last_reconciled_at is not None


async def test_quiet_connection_stamps_and_calls_nothing_further(
    connection, fake_provider, job_connector
) -> None:
    """Verdict (c): registered, nothing new — one free probe, a stamp,
    zero syncs. The quiet account's entire daily cost."""
    from pinch_backend.reconcile import reconcile_pass

    fake_provider.item_state = providers.ItemState(
        webhook=OUR_URL, transactions_updated_at=utcnow() - timedelta(days=3)
    )

    await reconcile_pass()

    assert fake_provider.probes == 1
    assert fake_provider.webhook_updates == []
    assert _sync_jobs(job_connector) == []
    assert (await _fresh(connection)).last_reconciled_at is not None


# --- Selection: who gets probed --------------------------------------------------


async def test_broken_connections_are_never_probed(
    connection, fake_provider, job_connector
) -> None:
    """reauth_required and error have their own repair paths (now prompt
    via ITEM webhooks) — the reconciler leaves them alone."""
    from pinch_backend.reconcile import reconcile_pass

    for status in (ConnectionStatus.REAUTH_REQUIRED, ConnectionStatus.ERROR):
        row = await _fresh(connection)
        row.status = status
        await row.save()
        assert await reconcile_pass() == 0
    assert fake_provider.probes == 0


async def test_a_reconciled_connection_is_not_reexamined_within_24_hours(
    connection, fake_provider, job_connector
) -> None:
    from pinch_backend.reconcile import reconcile_pass

    await reconcile_pass()
    assert fake_provider.probes == 1
    await reconcile_pass()
    assert fake_provider.probes == 1  # within the cadence: not due again


async def test_a_recently_synced_connection_is_not_probed(
    connection, fake_provider, job_connector
) -> None:
    """last_synced_at counts as examination too: a connection that just
    synced has nothing for the probe to discover."""
    from pinch_backend.reconcile import reconcile_pass

    row = await _fresh(connection)
    row.last_synced_at = utcnow()
    await row.save()

    assert await reconcile_pass() == 0
    assert fake_provider.probes == 0


async def test_a_failed_probe_does_not_stamp(connection, fake_provider, job_connector) -> None:
    """A probe failure must not buy the connection 24 quiet hours — the
    next pass retries it."""
    from pinch_backend.reconcile import reconcile_pass

    fake_provider.item_state_failure = providers.ProviderError(
        code="INSTITUTION_DOWN", message="try later"
    )
    await reconcile_pass()
    assert (await _fresh(connection)).last_reconciled_at is None

    fake_provider.item_state_failure = None
    await reconcile_pass()
    assert fake_provider.probes == 2
    assert (await _fresh(connection)).last_reconciled_at is not None


# --- The periodic task and the manual pass ---------------------------------------


def test_the_periodic_task_registers_with_the_settings_interval() -> None:
    """The repo's first periodic task: registered under its stable name,
    cron derived from PINCH_RECONCILE_INTERVAL_HOURS (24h default →
    daily; production's 1 → hourly)."""
    from pinch_backend.jobs import job_app, reconcile_cron

    assert "sync.reconcile_connections" in job_app.tasks
    registered = {key[0]: pt.cron for key, pt in job_app.periodic_registry.periodic_tasks.items()}
    assert registered["sync.reconcile_connections"] == reconcile_cron(24)
    assert reconcile_cron(24) == "0 0 * * *"
    assert reconcile_cron(1) == "0 * * * *"
    assert reconcile_cron(6) == "0 */6 * * *"


async def test_the_job_runs_the_identical_pass(
    connection, fake_provider, job_connector, run_jobs
) -> None:
    """The worker-side entry point drives the same reconcile_pass the CLI
    wraps — proven by a verdict landing through it."""
    from pinch_backend.jobs import reconcile_connections

    fake_provider.item_state = providers.ItemState(webhook="")
    await reconcile_connections(timestamp=0)

    assert fake_provider.webhook_updates == [OUR_URL]
    assert (await _fresh(connection)).last_reconciled_at is not None


# --- The MX probe (M13 CP4, issue #91; ADR 0009) ---------------------------------


class FakeMXReconcileProvider(FakeMXSyncProvider):
    """The MX sync fake grown a scriptable probe answer — assertions
    happen at this seam, exactly like the Plaid reconcile fake's."""

    def __init__(self) -> None:
        super().__init__()
        self.item_state = providers.ItemState(webhook="", member_status="CONNECTED")
        self.item_state_failure: providers.ProviderError | None = None
        self.probes = 0

    async def get_item_state(self) -> providers.ItemState:
        self.probes += 1
        if self.item_state_failure is not None:
            raise self.item_state_failure
        return self.item_state


@pytest.fixture
def fake_mx(monkeypatch):
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "mx_client_id", "test-mx-client-id")
    monkeypatch.setattr(settings, "mx_api_key", "test-mx-api-key")
    monkeypatch.setattr(settings, "mx_webhook_secret", "wh-secret")
    fake = FakeMXReconcileProvider()
    monkeypatch.setattr(providers, "get_provider", fake.materialize)
    return fake


@pytest.fixture
async def mx_connection(client, db, fake_mx, job_connector) -> Connection:
    """A connected MX member, stale enough to be due: last synced 2 days
    ago, never reconciled, queue drained of the connect flow's enqueues."""
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
    connection_id = response.json()["id"]
    row = await Connection.where(lambda c, cid=connection_id: c.id == cid).first()
    row.last_synced_at = utcnow() - timedelta(days=2)
    await row.save()
    return row


async def test_mx_aggregating_after_us_is_a_missed_doorbell(
    mx_connection, fake_mx, job_connector
) -> None:
    """MX's stamp postdating our last sync means a ring went missing:
    enqueue the normal sync (loudly logged), stamp the examination. The
    probe binds the enrollment's user guid and the member guid — the MX
    credential shape (ADR 0009), no secret anywhere."""
    from pinch_backend.reconcile import reconcile_pass

    fake_mx.item_state = providers.ItemState(
        webhook="",
        member_status="CONNECTED",
        transactions_updated_at=utcnow() - timedelta(hours=1),
    )

    count = await reconcile_pass()

    assert count == 1
    jobs = _sync_jobs(job_connector)
    assert len(jobs) == 1 and jobs[0]["args"] == {"connection_id": str(mx_connection.id)}
    assert (await _fresh(mx_connection)).last_reconciled_at is not None
    assert fake_mx.materialized[-1]["user_guid"] == "USR-1"
    assert fake_mx.materialized[-1]["member_guid"] == "MBR-9"


async def test_the_mx_missed_verdict_is_what_warns(mx_connection, fake_mx) -> None:
    """The loud warning rides the webhook_missed verdict, same seam as
    Plaid's — and it is the whole registration story for MX: a dead
    dashboard URL has no drift verdict to catch it (ADR 0009)."""
    from pinch_backend.reconcile import reconcile_connection

    fake_mx.item_state = providers.ItemState(
        webhook="",
        member_status="CONNECTED",
        transactions_updated_at=utcnow() - timedelta(hours=1),
    )
    assert await reconcile_connection(mx_connection) == "webhook_missed"


async def test_a_dead_dashboard_url_surfaces_as_daily_missed_doorbells(
    mx_connection, fake_mx, job_connector
) -> None:
    """The acceptance story (#91): nobody registered (or a registration
    died), MX aggregates nightly anyway — so every daily pass finds a
    fresh stamp, warns webhook.missed, and syncs. Loud every day, silent
    never."""
    from pinch_backend.reconcile import reconcile_pass

    for day in (2, 1):  # two consecutive daily passes
        fake_mx.item_state = providers.ItemState(
            webhook="",
            member_status="CONNECTED",
            transactions_updated_at=utcnow() - timedelta(hours=1),
        )
        assert await reconcile_pass() == 1
        # The pass syncs (advancing last_synced_at in real life); the next
        # nightly MX aggregation re-stales us — replayed here by aging the
        # stamps a day past the cadence.
        row = await _fresh(mx_connection)
        row.last_synced_at = utcnow() - timedelta(days=day)
        row.last_reconciled_at = utcnow() - timedelta(hours=25)
        await row.save()
    assert len(_sync_jobs(job_connector)) == 2  # one recovery sync per pass


async def test_a_quiet_mx_member_stamps_and_calls_nothing_further(
    mx_connection, fake_mx, job_connector
) -> None:
    """A stamp older than our last sync is the quiet verdict: one free
    probe, an examination stamp, zero syncs."""
    from pinch_backend.reconcile import reconcile_pass

    fake_mx.item_state = providers.ItemState(
        webhook="",
        member_status="CONNECTED",
        transactions_updated_at=utcnow() - timedelta(days=3),
    )

    await reconcile_pass()

    assert fake_mx.probes == 1
    assert _sync_jobs(job_connector) == []
    assert (await _fresh(mx_connection)).last_reconciled_at is not None


async def test_an_mx_side_broken_status_is_a_missed_doorbell(
    mx_connection, fake_mx, job_connector
) -> None:
    """MX says CHALLENGED while we still say active: the Connection
    Status doorbell went missing. The reconciler writes no health (the
    no-new-states law) — it enqueues the sync whose own status probe
    reads MX and records through the engine's existing transitions."""
    from pinch_backend.reconcile import reconcile_connection

    fake_mx.item_state = providers.ItemState(
        webhook="",
        member_status="CHALLENGED",
        transactions_updated_at=utcnow() - timedelta(days=3),  # stale: broken members stop
    )

    assert await reconcile_connection(mx_connection) == "webhook_missed"
    assert len(_sync_jobs(job_connector)) == 1
    row = await _fresh(mx_connection)
    assert row.status == ConnectionStatus.ACTIVE  # the probe never wrote health
    assert row.last_reconciled_at is not None


async def test_a_failed_mx_probe_does_not_stamp(mx_connection, fake_mx, job_connector) -> None:
    """Same contract as Plaid's: a probe failure must not buy 24 quiet
    hours."""
    from pinch_backend.reconcile import reconcile_pass

    fake_mx.item_state_failure = providers.ProviderError(code="HTTP_502", message="MX down")
    await reconcile_pass()
    assert (await _fresh(mx_connection)).last_reconciled_at is None

    fake_mx.item_state_failure = None
    await reconcile_pass()
    assert fake_mx.probes == 2
    assert (await _fresh(mx_connection)).last_reconciled_at is not None


async def test_broken_mx_connections_are_never_probed(mx_connection, fake_mx) -> None:
    """The existing rule holds per-provider: reauth_required and error
    have their own repair paths."""
    from pinch_backend.reconcile import reconcile_pass

    for status in (ConnectionStatus.REAUTH_REQUIRED, ConnectionStatus.ERROR):
        row = await _fresh(mx_connection)
        row.status = status
        await row.save()
        assert await reconcile_pass() == 0
    assert fake_mx.probes == 0


async def test_an_unconfigured_provider_is_not_probed(mx_connection, fake_mx, monkeypatch) -> None:
    """get_provider must never materialize without credentials: an MX row
    on an instance whose MX config went away simply waits (partial
    configuration is a valid state, PRD #86 story 16)."""
    from pinch_backend.reconcile import reconcile_pass
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "mx_client_id", "")

    assert await reconcile_pass() == 0
    assert fake_mx.probes == 0


async def test_the_periodic_task_runs_for_an_mx_only_instance(
    mx_connection, fake_mx, job_connector
) -> None:
    """The worker gate is any-provider (M13 CP4): Plaid-unconfigured must
    not silence MX's safety net."""
    from pinch_backend.jobs import reconcile_connections
    from pinch_backend.settings import settings

    assert not settings.plaid_configured  # the suite's keyless baseline
    fake_mx.item_state = providers.ItemState(
        webhook="",
        member_status="CONNECTED",
        transactions_updated_at=utcnow() - timedelta(hours=1),
    )

    await reconcile_connections(timestamp=0)

    assert len(_sync_jobs(job_connector)) == 1
    assert (await _fresh(mx_connection)).last_reconciled_at is not None
