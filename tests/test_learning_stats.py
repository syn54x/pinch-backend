"""accepted_untouched + /correction-log/stats over the public seam
(F4 Enabler A, #66): the flywheel's accuracy measure, recorded at decision
time and rolled up for the Learning tab."""

from test_flywheel_e2e import _account, _category, _commit_csv, _review, _signup

LOG = "/api/v1/correction-log"
TX = "/api/v1/transactions"


async def _txn_ids(client) -> list[str]:
    r = await client.get(TX)
    return [t["id"] for t in r.json()["items"]]


async def test_accept_as_is_records_untouched_and_a_correction_does_not(client) -> None:
    await _signup(client)
    account = await _account(client)
    coffee = await _category(client, "Coffee")
    await _commit_csv(
        client,
        account,
        rows=[
            ("2026-07-01", "-4.50", "BLUE BOTTLE"),
            ("2026-07-02", "-12.00", "CHIPOTLE"),
        ],
    )
    first, second = await _txn_ids(client)

    r = await _review(client, first)  # accept the proposal exactly as offered
    assert r.status_code == 200, r.text
    r = await _review(client, second, {"category_id": coffee})  # a correction
    assert r.status_code == 200, r.text

    entries = (await client.get(LOG)).json()["items"]
    flags = {e["transaction_id"]: e["accepted_untouched"] for e in entries}
    assert flags[first] is True
    assert flags[second] is False


async def test_backfill_derives_the_flag_for_pre_f4_rows(client, db) -> None:
    """Pre-F4 rows (flag NULL) are self-contained; the startup backfill
    derives the flag once, and void rows stay NULL (not decisions)."""
    import uuid as uuid_mod

    from pinch_backend.db import backfill_accepted_untouched
    from pinch_backend.models import (
        CorrectionActor,
        CorrectionKind,
        CorrectionLogEntry,
    )

    await _signup(client)
    from test_correction_log_api import _ledger_for

    ledger = await _ledger_for()
    accepted = await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=uuid_mod.uuid7(),
        kind=CorrectionKind.DECISION,
        actor=CorrectionActor.USER,
        proposal_category_id=None,
        decision_category_id=None,
    )
    cat = uuid_mod.uuid7()
    corrected = await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=uuid_mod.uuid7(),
        kind=CorrectionKind.DECISION,
        actor=CorrectionActor.USER,
        proposal_category_id=None,
        decision_category_id=cat,
    )
    void = await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=uuid_mod.uuid7(),
        kind=CorrectionKind.VOID,
        actor=CorrectionActor.USER,
        voids=accepted.id,
    )
    assert accepted.accepted_untouched is None

    await backfill_accepted_untouched()

    entries = {e["id"]: e for e in (await client.get(LOG)).json()["items"]}
    assert entries[str(accepted.id)]["accepted_untouched"] is True
    assert entries[str(corrected.id)]["accepted_untouched"] is False
    assert entries[str(void.id)]["accepted_untouched"] is None


async def test_stats_rolls_up_the_flywheel(client, db) -> None:
    """Reviews, corrections, untouched %, month-over-month windows, and
    accepted promoted rules — user-actor, non-voided decisions only."""
    import uuid as uuid_mod
    from datetime import UTC, datetime

    import pytest

    from pinch_backend.models import (
        CorrectionActor,
        CorrectionKind,
        CorrectionLogEntry,
        Rule,
        RuleOrigin,
        RuleStatus,
    )
    from test_correction_log_api import _ledger_for

    await _signup(client)
    ledger = await _ledger_for()

    async def entry(*, untouched: bool, when: datetime, actor=CorrectionActor.USER):
        return await CorrectionLogEntry.create(
            ledger=ledger,
            transaction_id=uuid_mod.uuid7(),
            kind=CorrectionKind.DECISION,
            actor=actor,
            accepted_untouched=untouched,
            created_at=when,
        )

    july = datetime(2026, 7, 10, tzinfo=UTC)
    june = datetime(2026, 6, 10, tzinfo=UTC)
    await entry(untouched=True, when=july)
    await entry(untouched=True, when=july)
    await entry(untouched=False, when=july)
    await entry(untouched=True, when=june)
    await entry(untouched=False, when=june)
    voided = await entry(untouched=True, when=july)
    await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=voided.transaction_id,
        kind=CorrectionKind.VOID,
        actor=CorrectionActor.AUTO,
        voids=voided.id,
        created_at=july,
    )
    await entry(untouched=True, when=july, actor=CorrectionActor.AUTO)

    await Rule.create(
        ledger=ledger, condition={}, origin=RuleOrigin.PROMOTION, status=RuleStatus.ACTIVE
    )
    await Rule.create(
        ledger=ledger, condition={}, origin=RuleOrigin.PROMOTION, status=RuleStatus.PROPOSED
    )
    await Rule.create(ledger=ledger, condition={}, origin=RuleOrigin.USER, status=RuleStatus.ACTIVE)

    r = await client.get(f"{LOG}/stats", params={"as_of": "2026-07-15"})
    assert r.status_code == 200, r.text
    stats = r.json()
    assert stats["reviews_total"] == 5
    assert stats["corrections_total"] == 2
    assert stats["accepted_untouched_pct"] == pytest.approx(3 / 5)
    assert stats["current_month_pct"] == pytest.approx(2 / 3)
    assert stats["previous_month_pct"] == pytest.approx(1 / 2)
    assert stats["promoted_rules_accepted"] == 1
