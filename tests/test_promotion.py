"""Promotion (M5 CP4, #22): >=3 consistent user-actor non-voided log
decisions filing payee X as category Y, no rule in ANY state covering X ->
mint `payee equals` in status=proposed. Latest decision per transaction
wins, derived here, never stored. Auto decisions are never evidence."""

import uuid

import pytest

from pinch_backend.classification.promotion import maybe_propose_rule
from pinch_backend.models import (
    Category,
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Ledger,
    Rule,
    RuleStatus,
)

PAYEE = "starbucks 123"


async def _ledger() -> tuple[Ledger, Category]:
    ledger = await Ledger.create(name="promo")
    coffee = await Category.create(ledger=ledger, name="Coffee")
    return ledger, coffee


async def _decision(
    ledger: Ledger,
    category: Category | None,
    *,
    payee: str = PAYEE,
    txn_id: uuid.UUID | None = None,
    actor: CorrectionActor = CorrectionActor.USER,
) -> CorrectionLogEntry:
    return await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=txn_id or uuid.uuid7(),
        kind=CorrectionKind.DECISION,
        actor=actor,
        input_payee=payee,
        decision_category_id=category.id if category else None,
        decision_category_name=category.name if category else None,
    )


async def test_three_consistent_user_decisions_mint_a_proposed_rule(db) -> None:
    ledger, coffee = await _ledger()
    for _ in range(3):
        await _decision(ledger, coffee)
    rule = await maybe_propose_rule(ledger, PAYEE, coffee.id)
    assert rule is not None
    assert rule.status == RuleStatus.PROPOSED
    assert rule.condition["payee"] == {"op": "equals", "value": PAYEE}
    assert rule.action_category_id == coffee.id  # ty: ignore[unresolved-attribute]
    assert rule.action_add_tags == []
    assert rule.action_rename_to is None


async def test_two_decisions_do_not_mint(db) -> None:
    ledger, coffee = await _ledger()
    for _ in range(2):
        await _decision(ledger, coffee)
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_one_deviation_kills(db) -> None:
    ledger, coffee = await _ledger()
    dining = await Category.create(ledger=ledger, name="Dining")
    for _ in range(3):
        await _decision(ledger, coffee)
    await _decision(ledger, dining)
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_an_uncategorized_latest_decision_kills(db) -> None:
    ledger, coffee = await _ledger()
    for _ in range(3):
        await _decision(ledger, coffee)
    await _decision(ledger, None)  # the user decided "uncategorized"
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_auto_decisions_are_never_evidence(db) -> None:
    """The pollution guard, by name (PRD M5)."""
    ledger, coffee = await _ledger()
    for _ in range(3):
        await _decision(ledger, coffee, actor=CorrectionActor.AUTO)
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None
    await _decision(ledger, coffee)
    await _decision(ledger, coffee)
    # 2 user + 3 auto: still short of three VOTES.
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_a_later_auto_entry_supersedes_that_transactions_user_vote(db) -> None:
    ledger, coffee = await _ledger()
    txn_id = uuid.uuid7()
    await _decision(ledger, coffee, txn_id=txn_id)  # user decided...
    # ...then auto re-filed
    await _decision(ledger, coffee, txn_id=txn_id, actor=CorrectionActor.AUTO)
    await _decision(ledger, coffee)
    await _decision(ledger, coffee)
    # The superseded transaction casts no vote: 2 votes, no mint.
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_voided_decisions_are_excluded(db) -> None:
    ledger, coffee = await _ledger()
    entries = [await _decision(ledger, coffee) for _ in range(3)]
    await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=entries[0].transaction_id,
        kind=CorrectionKind.VOID,
        actor=CorrectionActor.USER,
        voids=entries[0].id,
        void_reason="import undone",
    )
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_latest_decision_per_transaction_wins(db) -> None:
    """A changed mind repairs consistency: the deviation's transaction was
    re-decided to Y, so all three latest decisions agree."""
    ledger, coffee = await _ledger()
    dining = await Category.create(ledger=ledger, name="Dining")
    txn_id = uuid.uuid7()
    await _decision(ledger, dining, txn_id=txn_id)  # the old deviation
    await _decision(ledger, coffee, txn_id=txn_id)  # re-decided
    await _decision(ledger, coffee)
    await _decision(ledger, coffee)
    rule = await maybe_propose_rule(ledger, PAYEE, coffee.id)
    assert rule is not None


@pytest.mark.parametrize("status", list(RuleStatus))
async def test_a_covering_equals_rule_in_any_state_blocks(db, status: RuleStatus) -> None:
    ledger, coffee = await _ledger()
    await Rule.create(
        ledger=ledger,
        status=status,
        condition={"version": 1, "payee": {"op": "equals", "value": PAYEE}},
        action_category_id=coffee.id,
    )
    for _ in range(3):
        await _decision(ledger, coffee)
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_a_covering_contains_rule_blocks(db) -> None:
    ledger, coffee = await _ledger()
    await Rule.create(
        ledger=ledger,
        status=RuleStatus.ACTIVE,
        condition={"version": 1, "payee": {"op": "contains", "value": "STARBUCKS"}},
        action_category_id=coffee.id,
    )
    for _ in range(3):
        await _decision(ledger, coffee)
    # The stored value normalizes to "starbucks", a substring of the payee.
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_a_rule_without_a_payee_clause_does_not_block(db) -> None:
    ledger, coffee = await _ledger()
    await Rule.create(
        ledger=ledger,
        status=RuleStatus.ACTIVE,
        condition={
            "version": 1,
            "amount": {"op": "equals", "value": 500, "direction": "out", "currency": "USD"},
        },
        action_category_id=coffee.id,
    )
    for _ in range(3):
        await _decision(ledger, coffee)
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is not None


async def test_a_non_matching_payee_rule_does_not_block(db) -> None:
    ledger, coffee = await _ledger()
    await Rule.create(
        ledger=ledger,
        status=RuleStatus.ACTIVE,
        condition={"version": 1, "payee": {"op": "equals", "value": "peets"}},
        action_category_id=coffee.id,
    )
    for _ in range(3):
        await _decision(ledger, coffee)
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is not None


async def test_null_category_never_checks(db) -> None:
    ledger, coffee = await _ledger()
    for _ in range(3):
        await _decision(ledger, coffee)
    assert await maybe_propose_rule(ledger, PAYEE, None) is None


async def test_unexpressible_payees_never_mint(db) -> None:
    """PayeeCondition.value is bounded 1-200; a payee outside those bounds
    simply never promotes (it cannot be expressed as a rule)."""
    ledger, coffee = await _ledger()
    long_payee = "x" * 201
    for _ in range(3):
        await _decision(ledger, coffee, payee=long_payee)
    assert await maybe_propose_rule(ledger, long_payee, coffee.id) is None


async def test_evidence_is_ledger_scoped(db) -> None:
    ledger, coffee = await _ledger()
    other, other_coffee = await _ledger()
    for _ in range(2):
        await _decision(ledger, coffee)
    await _decision(other, other_coffee)
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None
