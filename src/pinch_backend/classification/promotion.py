"""Consented rule promotion (PRD M5 D14, M5 CP4 #22): inline at review
time, scoped to the just-reviewed payee. Trigger: >=3 user-actor, non-voided
log decisions filing payee X as category Y, all-time consistency (one
deviation kills — mixed payees are AI territory), and no rule in ANY state
covering X. Mints `payee equals` (never `contains` — auto-minted substring
rules are a footgun) in status=proposed; accepting is a status flip
(PATCH /rules/{id}).

Promotion reads the LOG; history reads transactions — the log answers "what
did the user decide", transactions answer "how are things filed now".
"Latest decision per transaction wins" is derived here, never stored. Auto
entries are never evidence, but a later auto entry supersedes that
transaction's user vote (the user's decision is no longer the standing one).

Called AFTER the consume transaction commits: a minting failure never rolls
back a review. Two same-payee reviews racing could double-mint — a
documented residual (single-tenant, microsecond window), same class as the
sweep's TOCTOU notes.
"""

from typing import TYPE_CHECKING

from pinch_backend.imports.fingerprint import normalize_description
from pinch_backend.models import (
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Ledger,
    Rule,
    RuleStatus,
)
from pinch_backend.observability import get_logger
from pinch_backend.rules.spec import ConditionSpec, PayeeCondition

if TYPE_CHECKING:
    import uuid

log = get_logger(__name__)

MIN_PROMOTION_DECISIONS = 3
"""Consistent user filings before a rule is proposed (PRD M5 D14)."""

_MAX_PAYEE_CONDITION_LENGTH = 200
"""PayeeCondition.value's upper bound; longer payees are unexpressible as
rule conditions and simply never promote."""


def _covers(rule: Rule, payee: str) -> bool:
    """Does this rule's payee clause match ``payee``? Evaluator semantics
    (rules.evaluator.matches), minus the stages promotion doesn't test:
    a rule without a payee clause never covers a payee."""
    spec = ConditionSpec(**rule.condition)
    if spec.payee is None:
        return False
    needle = normalize_description(spec.payee.value)
    if spec.payee.op == "equals":
        return payee == needle
    return needle in payee


async def maybe_propose_rule(
    ledger: Ledger, payee: str, category_id: "uuid.UUID | None"
) -> Rule | None:
    """The inline promotion check. ``category_id`` is the just-decided
    category (Y); the just-appended log entry is already evidence because
    this runs after the consume transaction commits."""
    if category_id is None:
        return None
    if not payee or len(payee) > _MAX_PAYEE_CONDITION_LENGTH:
        return None

    ledger_id = ledger.id
    entries = (
        await CorrectionLogEntry.where(
            lambda e, lid=ledger_id, p=payee: (
                (e.ledger_id == lid) & (e.kind == CorrectionKind.DECISION) & (e.input_payee == p)
            )
        )
        .order_by(lambda e: e.id)
        .all()
    )
    if len(entries) < MIN_PROMOTION_DECISIONS:
        return None
    entry_ids = [e.id for e in entries]
    voided = {
        v.voids
        for v in await CorrectionLogEntry.where(lambda v, ids=entry_ids: v.voids.in_(ids)).all()
    }
    latest: dict[uuid.UUID, CorrectionLogEntry] = {}
    for entry in entries:  # id-ascending (uuid7): the last write wins
        if entry.id in voided:
            continue
        latest[entry.transaction_id] = entry
    votes = [e for e in latest.values() if e.actor == CorrectionActor.USER]
    if len(votes) < MIN_PROMOTION_DECISIONS:
        return None
    if any(vote.decision_category_id != category_id for vote in votes):
        return None  # one deviation kills (uncategorized was a decision too)

    rules = await Rule.where(lambda r, lid=ledger_id: r.ledger_id == lid).all()
    if any(_covers(rule, payee) for rule in rules):
        return None  # ANY state: proposed awaits consent, dismissed is a tombstone

    condition = ConditionSpec(payee=PayeeCondition(op="equals", value=payee))
    rule = await Rule.create(
        ledger=ledger,
        status=RuleStatus.PROPOSED,
        condition=condition.model_dump(exclude_none=True),
        action_category_id=category_id,
    )
    log.info(
        "rule.promoted",
        rule_id=str(rule.id),
        ledger_id=str(ledger_id),
        payee=payee,
        category_id=str(category_id),
        decisions=len(votes),
    )
    return rule
