"""POST /api/v1/transactions/{id}/review and /transactions/review (M5 CP4,
#22): the human half of the flywheel. The body carries the FINAL user data;
the server diffs against the proposal to record accepted-vs-corrected;
empty body accepts as-is. Wraps CP3's consume_proposal and runs the inline
promotion check. Never accept-by-filter: reviewing data the user never saw
is not review.

Own Router (same /api/v1/transactions path as transactions_router) so this
module can import rules/transactions helpers without a cycle."""

import uuid
from typing import Annotated, Literal

from ferro import transaction
from litestar import Router, post
from litestar.di import NamedDependency
from litestar.exceptions import ClientException, HTTPException, NotFoundException
from litestar.params import FromPath
from litestar.status_codes import HTTP_200_OK, HTTP_409_CONFLICT, HTTP_422_UNPROCESSABLE_ENTITY
from pydantic import BaseModel, ConfigDict, Field

from pinch_backend.api.rules import RuleOut, rule_out
from pinch_backend.api.transactions import (
    SplitLineIn,
    TransactionOut,
    hydrate_transactions,
    replace_split_lines,
    resolve_split_categories,
    validate_split_document,
)
from pinch_backend.api.transfers import assert_not_in_transfer, establish_transfer
from pinch_backend.classification.consume import (
    AlreadyReviewedError,
    consume_proposal,
    log_transfer_decision_on_reviewed,
)
from pinch_backend.classification.promotion import maybe_propose_rule
from pinch_backend.jobs import classify_ledger
from pinch_backend.models import (
    Category,
    CorrectionActor,
    Ledger,
    Proposal,
    ProposalTag,
    Transaction,
)
from pinch_backend.observability import get_logger
from pinch_backend.tags import dedupe_tag_names

log = get_logger(__name__)

ALREADY_REVIEWED_DETAIL = "Already reviewed; un-review first (PATCH reviewed: false)"
"""One 409 message for both paths to it: the pre-check fast path and
consume's in-transaction CAS losing to a concurrent decision."""


class ReviewTransferIn(BaseModel):
    """The transfer decision (M6 CP3), in exactly one form:
    ``{"untracked": true}`` (the counterparty isn't in Pinch) or
    ``{"counterpart": <transaction id>}`` (link the named pair)."""

    model_config = ConfigDict(use_attribute_docstrings=True, extra="forbid")

    untracked: bool | None = None
    counterpart: uuid.UUID | None = None


class ReviewIn(BaseModel):
    """The FINAL user data. Field-present merge against the proposal: an
    absent field means "the proposal's value", a present one is the user's
    final word. Empty body accepts as-is. notes is not reviewable — that is
    PATCH's job. An explicit ``"display_name": null`` REJECTS the proposal's
    rename (a correction; the rename is not applied — consume applies
    display_name only when not None); clearing an already-applied override
    is PATCH's job. ``splits`` and ``transfer`` (M6 CP3) are one-motion
    decisions, mutually exclusive with ``category_id`` and each other."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    category_id: uuid.UUID | None = None
    tags: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(
        default=None, max_length=50
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    splits: list[SplitLineIn] | None = None
    transfer: ReviewTransferIn | None = None


def _mutual_exclusion(detail: str) -> ClientException:
    return ClientException(detail=detail, status_code=HTTP_422_UNPROCESSABLE_ENTITY)


class ReviewOut(BaseModel):
    """The review envelope: the consent moment rides the response — a
    just-minted proposed rule is shown right here, never polled for."""

    transaction: TransactionOut
    result: Literal["accepted", "corrected"]
    proposed_rule: RuleOut | None


async def _pending_proposal(txn_id: uuid.UUID) -> tuple[Proposal | None, list[str]]:
    proposal = await Proposal.where(lambda p, tid=txn_id: p.transaction_id == tid).first()
    if proposal is None:
        return None, []
    proposal_id = proposal.id
    names = [
        pt.name
        for pt in await ProposalTag.where(lambda pt, pid=proposal_id: pt.proposal_id == pid)
        .order_by(lambda pt: pt.id)
        .all()
    ]
    return proposal, names


@post("/{txn_id:uuid}/review", status_code=HTTP_200_OK)
async def review_transaction(
    txn_id: FromPath[uuid.UUID],
    current_ledger: NamedDependency[Ledger],
    data: ReviewIn | None = None,
) -> ReviewOut:
    ledger_id = current_ledger.id
    txn = await Transaction.where(
        lambda t, tid=txn_id, lid=ledger_id: (t.id == tid) & (t.ledger_id == lid)
    ).first()
    if txn is None:
        raise NotFoundException(detail="No such transaction")
    if txn.reviewed_at is not None:
        # Fast path; the airtight guard is consume's CAS claim below.
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail=ALREADY_REVIEWED_DETAIL)

    body = data if data is not None else ReviewIn()
    fields = data.model_fields_set if data is not None else set()

    # One decision shape per motion (M6 CP3): category, splits, or transfer.
    shapes = sum(
        (
            "category_id" in fields and body.category_id is not None,
            body.splits is not None,
            body.transfer is not None,
        )
    )
    if shapes > 1:
        raise _mutual_exclusion("category_id, splits, and transfer are mutually exclusive")
    if body.transfer is not None:
        untracked = body.transfer.untracked is True
        if untracked == (body.transfer.counterpart is not None):
            raise _mutual_exclusion(
                'The transfer decision takes exactly one form: {"untracked": true} '
                'or {"counterpart": <transaction id>}'
            )

    if "category_id" in fields and body.category_id is not None:
        wanted = body.category_id
        category = await Category.where(
            lambda c, cid=wanted, lid=ledger_id: (c.id == cid) & (c.ledger_id == lid)
        ).first()
        if category is None:
            raise NotFoundException(detail="No such category")

    proposal, proposal_tags = await _pending_proposal(txn.id)
    prop_category_id = proposal.category_id if proposal else None  # ty: ignore[unresolved-attribute]
    prop_display = proposal.proposed_display_name if proposal else None

    final_category = body.category_id if "category_id" in fields else prop_category_id
    final_tags = dedupe_tag_names(list(body.tags or []) if "tags" in fields else proposal_tags)
    final_display = body.display_name if "display_name" in fields else prop_display

    # An explicit null display_name against a proposal that HAS a rename is
    # a correction too: the user rejected the rename (it is not applied).
    display_rejected = (
        "display_name" in fields and body.display_name is None and prop_display is not None
    )
    corrected = (
        final_category != prop_category_id
        or {t.casefold() for t in final_tags} != {t.casefold() for t in proposal_tags}
        or (final_display is not None and final_display != prop_display)
        or display_rejected
    )

    try:
        if body.splits is not None:
            # One-motion split: the same document rules as PUT /splits, plus
            # the consume — one database transaction end to end.
            await assert_not_in_transfer(txn.id)
            validate_split_document(txn, body.splits)
            await resolve_split_categories(current_ledger, body.splits)
            async with transaction():
                await replace_split_lines(current_ledger, txn, body.splits)
                entry = await consume_proposal(
                    current_ledger,
                    txn,
                    category_id=None,
                    tags=final_tags,
                    display_name=final_display,
                    actor=CorrectionActor.USER,
                )
        elif body.transfer is not None and body.transfer.untracked is True:
            async with transaction():
                await establish_transfer(current_ledger, [txn])
                entry = await consume_proposal(
                    current_ledger,
                    txn,
                    category_id=None,
                    tags=final_tags,
                    display_name=final_display,
                    actor=CorrectionActor.USER,
                )
        elif body.transfer is not None:
            counterpart_id = body.transfer.counterpart
            if counterpart_id == txn.id:
                raise _mutual_exclusion("A transaction cannot be its own counterpart")
            counterpart = await Transaction.where(
                lambda t, cid=counterpart_id, lid=ledger_id: (t.id == cid) & (t.ledger_id == lid)
            ).first()
            if counterpart is None:
                raise NotFoundException(detail="No such transaction")
            counterpart_proposal, counterpart_tags = await _pending_proposal(counterpart.id)
            async with transaction():
                # Both sides consumed, two log entries, one database
                # transaction — accept-by-explicit-id, never filter. An
                # already-reviewed counterpart is fair game (M7 CP4 relaxed
                # the M6 409): the link is created, its category vacated by
                # establish, its reviewed state stands, and the transfer
                # decision lands as a later entry.
                await establish_transfer(current_ledger, [txn, counterpart])
                entry = await consume_proposal(
                    current_ledger,
                    txn,
                    category_id=None,
                    tags=final_tags,
                    display_name=final_display,
                    actor=CorrectionActor.USER,
                )
                if counterpart.reviewed_at is None:
                    await consume_proposal(
                        current_ledger,
                        counterpart,
                        category_id=None,
                        tags=dedupe_tag_names(counterpart_tags),
                        display_name=(
                            counterpart_proposal.proposed_display_name
                            if counterpart_proposal
                            else None
                        ),
                        actor=CorrectionActor.USER,
                    )
                else:
                    await log_transfer_decision_on_reviewed(current_ledger, counterpart)
        else:
            # Accept-as-is of a transfer-shaped proposal creates the untracked
            # Transfer (M6 CP4) — unless the user's final word was a category.
            entry = await consume_proposal(
                current_ledger,
                txn,
                category_id=final_category,
                tags=final_tags,
                display_name=final_display,
                actor=CorrectionActor.USER,
                apply_proposed_transfer=not (
                    "category_id" in fields and body.category_id is not None
                ),
            )
    except AlreadyReviewedError:
        # A concurrent decision won between the pre-check and the claim (on
        # either side of a linked motion — the whole write rolled back).
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail=ALREADY_REVIEWED_DETAIL) from None

    # The entry is what actually happened: a decision that landed as a split
    # or transfer deviates from the proposal — unless the proposal itself was
    # transfer-shaped and the decision is the matching link: untracked for a
    # rule/history proposal (CP4), or the detector's exact counterpart (M7).
    prop_transfer = bool(proposal and proposal.proposed_transfer)
    prop_counterpart = proposal.counterpart_transaction_id if proposal else None  # ty: ignore[unresolved-attribute]
    decided_untracked = (
        entry.decision_transfer is not None and entry.decision_transfer["kind"] == "untracked"
    )
    decided_linked_as_proposed = (
        entry.decision_transfer is not None
        and entry.decision_transfer["kind"] == "linked"
        and prop_counterpart is not None
        and entry.decision_transfer.get("counterpart_transaction_id") == str(prop_counterpart)
    )
    shape_accepted = prop_transfer and (decided_untracked or decided_linked_as_proposed)
    if (
        entry.decision_splits is not None or entry.decision_transfer is not None
    ) and not shape_accepted:
        corrected = True
    if prop_transfer and entry.decision_transfer is None:
        corrected = True  # the transfer proposal was rejected
    rule = await maybe_propose_rule(
        current_ledger,
        txn.description_normalized,
        entry.decision_category_id,
        untracked_transfer=decided_untracked,
    )
    if prop_counterpart is not None and not decided_linked_as_proposed:
        # The mirror on the counterpart died with this decision (consume
        # invalidated it); its owner re-enters classification for a fresh,
        # non-transfer proposal. The rejected pairing is now correction-log
        # memory, so the detector won't re-propose it.
        await classify_ledger.configure(lock=f"ledger:{ledger_id}").defer_async(
            ledger_id=str(ledger_id)
        )

    result: Literal["accepted", "corrected"] = "corrected" if corrected else "accepted"
    log.info(
        "review.corrected" if corrected else "review.accepted",
        transaction_id=str(txn.id),
        ledger_id=str(ledger_id),
        promoted_rule_id=str(rule.id) if rule else None,
    )
    (out,) = await hydrate_transactions([txn])
    return ReviewOut(
        transaction=out,
        result=result,
        proposed_rule=await rule_out(rule) if rule else None,
    )


class ReviewBatchIn(BaseModel):
    """Explicit ids only (<=1,000 clears a realistic month) — never
    accept-by-filter. Duplicates are deduped preserving order."""

    ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)


class ReviewBatchOut(BaseModel):
    accepted: int
    skipped: int
    proposed_rules: list[RuleOut]


@post("/review", status_code=HTTP_200_OK)
async def review_batch(
    data: ReviewBatchIn, current_ledger: NamedDependency[Ledger]
) -> ReviewBatchOut:
    ledger_id = current_ledger.id
    ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for wanted in data.ids:
        if wanted not in seen:
            seen.add(wanted)
            ids.append(wanted)

    txns = await Transaction.where(
        lambda t, wanted=ids, lid=ledger_id: (t.ledger_id == lid) & (t.id.in_(wanted))
    ).all()
    by_id = {t.id: t for t in txns}
    missing = [str(i) for i in ids if i not in by_id]
    if missing:
        # Validate-all-first: skipped means "already reviewed", never
        # "silently didn't exist" — a stale or foreign id fails loudly.
        raise NotFoundException(
            detail="Unknown transactions in batch", extra={"missing_ids": missing}
        )

    accepted = skipped = 0
    decided: dict[str, tuple[uuid.UUID | None, bool]] = {}
    for wanted in ids:
        txn = by_id[wanted]
        if txn.reviewed_at is not None:
            skipped += 1
            continue
        proposal, proposal_tags = await _pending_proposal(txn.id)
        final_category = proposal.category_id if proposal else None  # ty: ignore[unresolved-attribute]
        try:
            entry = await consume_proposal(
                current_ledger,
                txn,
                category_id=final_category,
                tags=dedupe_tag_names(proposal_tags),
                display_name=proposal.proposed_display_name if proposal else None,
                actor=CorrectionActor.USER,
                apply_proposed_transfer=True,  # accept-as-is IS the batch (CP4)
            )
        except AlreadyReviewedError:
            # A concurrent decision won ⇒ it IS already reviewed — the same
            # honest skip as the pre-check above; and a decision this batch
            # didn't make must not feed the promotion evidence below.
            skipped += 1
            continue
        accepted += 1
        # What was DECIDED, not what was proposed: on a split or transferred
        # transaction consume never applies the category (M6 CP3), and the
        # promotion evidence below must see that truth.
        decided[txn.description_normalized] = (
            entry.decision_category_id,
            entry.decision_transfer is not None and entry.decision_transfer["kind"] == "untracked",
        )

    proposed: list[RuleOut] = []
    for payee, (category_id, untracked) in decided.items():
        rule = await maybe_propose_rule(
            current_ledger, payee, category_id, untracked_transfer=untracked
        )
        if rule is not None:
            proposed.append(await rule_out(rule))
    log.info(
        "review.batch_completed",
        ledger_id=str(ledger_id),
        accepted=accepted,
        skipped=skipped,
        rules_proposed=len(proposed),
    )
    return ReviewBatchOut(accepted=accepted, skipped=skipped, proposed_rules=proposed)


reviews_router = Router(
    path="/api/v1/transactions", route_handlers=[review_transaction, review_batch]
)
