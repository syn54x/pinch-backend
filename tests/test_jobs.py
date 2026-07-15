"""Procrastinate wiring (M5 CP3, #21): the app, the task, the conninfo
translation. Job effects on real data are asserted at the HTTP seam in
test_classification_api.py."""

import uuid
from datetime import date

from pinch_backend.jobs import _psycopg_conninfo, classify_ledger, job_app
from pinch_backend.models import (
    Account,
    AccountKind,
    Ledger,
    Proposal,
    Transaction,
    provision_user,
)


def test_conninfo_translation_strips_ferro_params() -> None:
    assert _psycopg_conninfo("postgres://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert (
        _psycopg_conninfo("postgres://u:p@h:5432/db?ferro_search_path=s&sslmode=require")
        == "postgresql://u:p@h:5432/db?sslmode=require"
    )


async def test_deferred_job_sweeps_the_ledger(db, job_connector, run_jobs) -> None:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]
    account = await Account.create(ledger=ledger, kind=AccountKind.DEPOSITORY, label="Checking")
    await Transaction.create(
        ledger=ledger,
        account=account,
        date=date(2026, 7, 1),
        amount_minor=-500,
        currency="USD",
        description_raw="X",
        description_normalized="x",
        fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
    )

    await classify_ledger.configure(lock=f"ledger:{ledger.id}").defer_async(
        ledger_id=str(ledger.id)
    )
    assert len(job_connector.jobs) == 1
    assert await Proposal.where(lambda p: p.ledger_id == ledger.id).count() == 0

    await run_jobs()
    assert await Proposal.where(lambda p: p.ledger_id == ledger.id).count() == 1


def test_task_is_registered_under_its_stable_name() -> None:
    assert "classification.classify_ledger" in job_app.tasks
