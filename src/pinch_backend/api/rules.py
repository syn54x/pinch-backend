"""/api/v1/rules — deterministic user law: CRUD + match preview (PRD M5 #20).

Rules never write user data; their actions ride the proposal (CP3). Same
conventions as every domain surface: current_ledger (I-2), Page[T] lists,
allowlist responses, tenancy 404s, scope guard by construction. Condition
semantics live exclusively in pinch_backend.rules.evaluator.
"""

import uuid
from datetime import datetime
from typing import Annotated

from ferro import transaction
from litestar import Router, delete, get, patch, post
from litestar.di import NamedDependency
from litestar.exceptions import ClientException, NotFoundException
from litestar.params import FromPath, QueryParameter
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.api.transactions import CategoryRef, TransactionOut, hydrate_transactions
from pinch_backend.jobs import classify_ledger
from pinch_backend.models import (
    Category,
    CorrectionKind,
    CorrectionLogEntry,
    Ledger,
    ProposalProvenance,
    Rule,
    RuleOrigin,
    RuleStatus,
    User,
)
from pinch_backend.observability import get_logger
from pinch_backend.rules.apply import (
    RetroApplyTier,
    apply_to_reviewed,
    breakdown,
    clear_proposals,
)
from pinch_backend.rules.evaluator import scan_matches
from pinch_backend.rules.spec import ConditionSpec

log = get_logger(__name__)

TagNames = list[Annotated[str, Field(min_length=1, max_length=100)]]


class RuleCreateIn(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    condition: dict
    """A ConditionSpec payload; amount.currency may be omitted and is filled
    from the user's primary currency before storage."""
    action_category_id: uuid.UUID | None = None
    action_add_tags: TagNames = Field(default_factory=list, max_length=50)
    action_rename_to: str | None = Field(default=None, min_length=1, max_length=100)
    action_mark_transfer: bool = False
    """Propose an untracked transfer (M6 CP4); mutually exclusive with
    action_category_id — one rule, one classification stance."""
    apply: RetroApplyTier = RetroApplyTier.FORWARD
    """The escalating consent tiers, chosen at rule creation (CONTEXT.md:
    Retro-apply). Cumulative: UNREVIEWED includes forward; FULL includes
    both. Never re-offered on edit."""
    # ^ Verbatim copy of RetroApplyTier's class docstring, deliberately:
    # the OpenAPI generator nondeterministically crowns either the enum's
    # docstring or this field's as the shared component's description
    # (~1 in 4 runs differed), which made the frontend's check-drift a
    # coin flip. Identical texts make the race unobservable.


class RulePatchIn(BaseModel):
    """Partial update; only fields present in the body apply. The condition
    is replaced whole, never merged."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    condition: dict | None = None
    action_category_id: uuid.UUID | None = None
    """Present-and-null clears the category action."""
    action_add_tags: TagNames | None = Field(default=None, max_length=50)
    action_rename_to: str | None = Field(default=None, min_length=1, max_length=100)
    action_mark_transfer: bool | None = None
    status: RuleStatus | None = None
    apply: RetroApplyTier | None = None
    """A retro-apply tier, honored ONLY when this same PATCH transitions
    proposed -> active: accepting a promoted rule IS the creation consent
    (CONTEXT.md: Retro-apply). Any other use is a 400 — active law never
    re-offers the tiers."""


class RuleApplyReport(BaseModel):
    """What one retro-apply consent actually did (F4 Enabler B, #67)."""

    tier: RetroApplyTier
    refreshed_unreviewed: int
    """Unreviewed matches whose proposals were vacated for re-sweep under
    the new rule."""
    recategorized_reviewed: int
    """Reviewed matches recategorized in place (full tier only)."""
    skipped: int
    """Split parents and transfer members — structure kept, counted."""
    batch_id: uuid.UUID | None
    """Groups the full tier's log entries; null below the full tier."""


class RuleOut(BaseModel):
    """What a client may see about a rule — an allowlist, never the row."""

    id: uuid.UUID
    status: RuleStatus
    origin: RuleOrigin
    matched_count: int
    """Reviewed decisions this rule contributed to, counted from the log's
    provenance snapshots (F4 Enabler A, #66) — survives anything but the
    log itself. The wireframe's "matched 12"."""
    condition: dict
    action_category: CategoryRef | None
    action_add_tags: list[str]
    action_rename_to: str | None
    action_mark_transfer: bool
    applied: RuleApplyReport | None = None
    """Present only on the create response, when a retro tier ran."""
    created_at: datetime


PREVIEW_CAP = 50
"""A sample, not a cursor walk (D14): enough to build a rule with evidence."""


class RulePreviewOut(BaseModel):
    """Up to PREVIEW_CAP existing transactions the condition would match.
    ``truncated`` means at least one more match exists beyond the sample.
    The counts cover the FULL match set (F4 Enabler B, #67) — the consent
    numbers for the retro-apply tiers; skipped = split parents and transfer
    members, whose structure retro-apply never touches."""

    items: list[TransactionOut]
    truncated: bool
    unreviewed_count: int
    reviewed_count: int
    skipped_count: int


def parse_condition(payload: dict, default_currency: str) -> ConditionSpec:
    """Validate a wire condition, filling the amount currency from the
    user's primary when omitted — stored specs are always explicit. Invalid
    is a 400 in the envelope, never a 500."""
    filled = dict(payload)
    amount = filled.get("amount")
    if isinstance(amount, dict) and amount.get("currency") is None:
        filled["amount"] = amount | {"currency": default_currency}
    try:
        return ConditionSpec.model_validate(filled)
    except ValidationError as error:
        raise ClientException(
            detail="Invalid condition",
            extra=[
                {"field": ".".join(str(loc) for loc in e["loc"]), "message": e["msg"]}
                for e in error.errors()
            ],
        ) from None


def _assert_coherent_actions(
    category_id: uuid.UUID | None,
    add_tags: list[str],
    rename_to: str | None,
    mark_transfer: bool,
) -> None:
    if category_id is None and not add_tags and rename_to is None and not mark_transfer:
        raise ClientException(detail="A rule must carry at least one action")
    if category_id is not None and mark_transfer:
        # One rule, one classification stance — cross-rule precedence is the
        # pipeline's job; a self-contradictory rule is a user error.
        raise ClientException(detail="A rule cannot both set a category and mark a transfer")


async def _get(ledger: Ledger, rule_id: uuid.UUID) -> Rule:
    rule = await Rule.where(lambda r: (r.id == rule_id) & (r.ledger_id == ledger.id)).first()
    if rule is None:
        raise NotFoundException(detail="No such rule")
    return rule


async def _resolve_category(ledger: Ledger, category_id: uuid.UUID) -> Category:
    category = await Category.where(
        lambda c: (c.id == category_id) & (c.ledger_id == ledger.id)
    ).first()
    if category is None:
        raise NotFoundException(detail="No such category")
    return category


async def matched_counts(ledger_id: uuid.UUID, rule_ids: list[uuid.UUID]) -> dict[str, int]:
    """Per-rule decision counts folded in Python from the rule-provenance
    snapshots (detail.rule_ids names every contributor) — JSONB is never
    queried into (M5 D11), so the set-shaped part stays SQL and the fold
    stays here (the M8 tier law). Voided decisions no longer describe
    reality and are excluded."""
    if not rule_ids:
        return {}
    wanted = {str(rid) for rid in rule_ids}
    entries = await CorrectionLogEntry.where(
        lambda e: (
            (e.ledger_id == ledger_id)
            & (e.kind == CorrectionKind.DECISION)
            & (e.proposal_provenance == ProposalProvenance.RULE)
        )
    ).all()
    entry_ids = [e.id for e in entries]
    voided: set[uuid.UUID] = set()
    if entry_ids:
        voided = {
            v.voids
            for v in await CorrectionLogEntry.where(lambda v, ids=entry_ids: v.voids.in_(ids)).all()
            if v.voids is not None
        }
    counts: dict[str, int] = {}
    for e in entries:
        if e.id in voided:
            continue
        for rid in (e.proposal_detail or {}).get("rule_ids", []):
            if rid in wanted:
                counts[rid] = counts.get(rid, 0) + 1
    return counts


async def rule_out(rule: Rule, matched_count: int | None = None) -> RuleOut:
    """Public: review responses embed the minted rule (M5 CP4)."""
    if matched_count is None:
        assert rule.ledger_id is not None
        found = await matched_counts(rule.ledger_id, [rule.id])
        matched_count = found.get(str(rule.id), 0)
    category = None
    if rule.action_category_id is not None:
        row = await Category.get(rule.action_category_id)
        category = CategoryRef(id=row.id, name=row.name)
    return RuleOut(
        id=rule.id,
        status=rule.status,
        origin=rule.origin,
        matched_count=matched_count,
        condition=rule.condition,
        action_category=category,
        action_add_tags=rule.action_add_tags,
        action_rename_to=rule.action_rename_to,
        action_mark_transfer=rule.action_mark_transfer,
        created_at=rule.created_at,
    )


async def _run_retro_apply(
    ledger: Ledger, rule: Rule, spec: ConditionSpec, tier: RetroApplyTier
) -> RuleApplyReport:
    """The consented tier, shared by create and the acceptance PATCH —
    caller wraps in a transaction and defers the sweep after commit."""
    matched, _ = await scan_matches(spec, ledger.id, cap=None)
    split = await breakdown(matched)
    await clear_proposals(split.unreviewed)
    batch_id: uuid.UUID | None = None
    recategorized = 0
    if tier is RetroApplyTier.FULL and split.reviewed:
        batch_id, recategorized = await apply_to_reviewed(ledger, rule, split.reviewed)
    return RuleApplyReport(
        tier=tier,
        refreshed_unreviewed=len(split.unreviewed),
        recategorized_reviewed=recategorized,
        skipped=len(split.skipped),
        batch_id=batch_id,
    )


@post("/")
async def create_rule(
    data: RuleCreateIn,
    current_ledger: NamedDependency[Ledger],
    current_user: NamedDependency[User],
) -> RuleOut:
    """A user-created rule is law immediately (status=active): consent by
    authorship. PROPOSED is what CP4's promotion mints. ``apply`` runs the
    consented retro tier in the same request (F4 Enabler B, #67) —
    creation-time by construction, so re-apply doesn't exist."""
    spec = parse_condition(data.condition, current_user.primary_currency)
    _assert_coherent_actions(
        data.action_category_id,
        data.action_add_tags,
        data.action_rename_to,
        data.action_mark_transfer,
    )
    if data.action_mark_transfer and data.apply is not RetroApplyTier.FORWARD:
        raise ClientException(
            detail="Transfer-marking rules are forward-only — a rule sees one "
            "transaction and cannot re-interpret settled history"
        )
    category = (
        await _resolve_category(current_ledger, data.action_category_id)
        if data.action_category_id
        else None
    )
    report: RuleApplyReport | None = None
    async with transaction():
        rule = await Rule.create(
            ledger=current_ledger,
            condition=spec.model_dump(exclude_none=True),
            action_category=category,
            action_add_tags=data.action_add_tags,
            action_rename_to=data.action_rename_to,
            action_mark_transfer=data.action_mark_transfer,
        )
        if data.apply is not RetroApplyTier.FORWARD:
            report = await _run_retro_apply(current_ledger, rule, spec, data.apply)
    if report is not None and report.refreshed_unreviewed:
        # Defer-after-commit (jobs module contract): the sweep re-proposes
        # the vacated backlog under the now-active rule.
        await classify_ledger.configure(lock=f"ledger:{current_ledger.id}").defer_async(
            ledger_id=str(current_ledger.id)
        )
    log.info(
        "rule.created",
        rule_id=str(rule.id),
        ledger_id=str(current_ledger.id),
        apply_tier=str(data.apply),
    )
    out = await rule_out(rule)
    out.applied = report
    return out


@get("/")
async def list_rules(
    current_ledger: NamedDependency[Ledger],
    status: Annotated[RuleStatus | None, QueryParameter()] = None,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[RuleOut]:
    """Creation (uuid7) order — which is also evaluation order (D13)."""
    ledger_id = current_ledger.id
    query = Rule.where(lambda r: r.ledger_id == ledger_id)
    if status is not None:
        wanted = status
        query = query.where(lambda r, s=wanted: r.status == s)
    rows, next_cursor = await paginate(query, cursor=cursor, limit=limit)
    counts = await matched_counts(ledger_id, [r.id for r in rows])
    return Page(
        items=[await rule_out(r, counts.get(str(r.id), 0)) for r in rows],
        next_cursor=next_cursor,
    )


@post("/preview", status_code=HTTP_200_OK)
async def preview_rule(
    data: dict,
    current_ledger: NamedDependency[Ledger],
    current_user: NamedDependency[User],
) -> RulePreviewOut:
    """Dry-run a bare condition against the ledger (story 9): works before
    the rule exists, so rules are built with evidence, not hope."""
    spec = parse_condition(data, current_user.primary_currency)
    found, _ = await scan_matches(spec, current_ledger.id, cap=None)
    split = await breakdown(found)
    sample = found[:PREVIEW_CAP]
    return RulePreviewOut(
        items=await hydrate_transactions(sample),
        truncated=len(found) > PREVIEW_CAP,
        unreviewed_count=len(split.unreviewed),
        reviewed_count=len(split.reviewed),
        skipped_count=len(split.skipped),
    )


@get("/{rule_id:uuid}")
async def get_rule(
    rule_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> RuleOut:
    return await rule_out(await _get(current_ledger, rule_id))


@patch("/{rule_id:uuid}")
async def update_rule(
    rule_id: FromPath[uuid.UUID],
    data: RulePatchIn,
    current_ledger: NamedDependency[Ledger],
    current_user: NamedDependency[User],
) -> RuleOut:
    rule = await _get(current_ledger, rule_id)
    fields = data.model_fields_set

    apply_tier = data.apply if "apply" in fields and data.apply is not None else None
    # Retro-apply rides exactly one PATCH shape: the acceptance of a
    # promoted rule (proposed -> active) — that transition IS the creation
    # consent (CONTEXT.md: Retro-apply). Everything else is law being
    # edited, and edited law never re-offers the tiers.
    if apply_tier is not None and (
        rule.status is not RuleStatus.PROPOSED or data.status is not RuleStatus.ACTIVE
    ):
        raise ClientException(
            detail="apply is the acceptance consent — it rides the "
            "proposed -> active transition only"
        )

    if "condition" in fields:
        if data.condition is None:
            raise ClientException(detail="A rule cannot lose its condition")
        spec = parse_condition(data.condition, current_user.primary_currency)
        rule.condition = spec.model_dump(exclude_none=True)
    if "action_category_id" in fields:
        if data.action_category_id is not None:
            category = await _resolve_category(current_ledger, data.action_category_id)
            rule.action_category_id = category.id
        else:
            rule.action_category_id = None
    if "action_add_tags" in fields:
        # Present-and-null clears, same tri-state as action_category_id —
        # subject to the same "some action must survive" invariant below.
        rule.action_add_tags = data.action_add_tags if data.action_add_tags is not None else []
    if "action_rename_to" in fields:
        rule.action_rename_to = data.action_rename_to
    if "action_mark_transfer" in fields:
        if data.action_mark_transfer is None:
            raise ClientException(detail="action_mark_transfer cannot be null; use false")
        rule.action_mark_transfer = data.action_mark_transfer
    if "status" in fields:
        if data.status is None:
            raise ClientException(detail="status cannot be null")
        if data.status == RuleStatus.PROPOSED:
            raise ClientException(detail="only promotion proposes a rule")
        rule.status = data.status

    _assert_coherent_actions(
        rule.action_category_id,
        rule.action_add_tags,
        rule.action_rename_to,
        rule.action_mark_transfer,
    )
    if apply_tier is not None and rule.action_mark_transfer:
        raise ClientException(
            detail="Transfer-marking rules are forward-only — a rule sees one "
            "transaction and cannot re-interpret settled history"
        )
    report: RuleApplyReport | None = None
    async with transaction():
        await rule.save()
        if apply_tier is not None and apply_tier is not RetroApplyTier.FORWARD:
            spec = ConditionSpec(**rule.condition)
            report = await _run_retro_apply(current_ledger, rule, spec, apply_tier)
    if report is not None and report.refreshed_unreviewed:
        # Defer-after-commit (jobs module contract), same as create.
        await classify_ledger.configure(lock=f"ledger:{current_ledger.id}").defer_async(
            ledger_id=str(current_ledger.id)
        )
    log.info(
        "rule.updated",
        rule_id=str(rule.id),
        ledger_id=str(current_ledger.id),
        apply_tier=str(apply_tier) if apply_tier else None,
    )
    out = await rule_out(rule)
    out.applied = report
    return out


@delete("/{rule_id:uuid}")
async def delete_rule(
    rule_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> None:
    """Delete = forget this ever happened. A proposed or dismissed rule's
    promotion tombstone is erased along with it, so the same payee can be
    re-proposed from scratch (promotion.maybe_propose_rule only sees rules
    that still exist) — deleting a dismissed rule is therefore the only
    undo for a fat-fingered dismiss. Contrast `PATCH status: dismissed`,
    which means never ask again: the tombstone stays and keeps blocking
    re-proposal. Rules carry no history either way (disable/dismiss is the
    soft option; this is the hard one)."""
    rule = await _get(current_ledger, rule_id)
    await rule.delete()
    log.info("rule.deleted", rule_id=str(rule_id), ledger_id=str(current_ledger.id))


rules_router = Router(
    path="/api/v1/rules",
    route_handlers=[create_rule, list_rules, preview_rule, get_rule, update_rule, delete_rule],
)
