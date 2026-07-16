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

from litestar import Router, post
from litestar.di import NamedDependency
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import FromPath
from litestar.status_codes import HTTP_200_OK, HTTP_409_CONFLICT
from pydantic import BaseModel, ConfigDict, Field

from pinch_backend.api.rules import RuleOut, rule_out
from pinch_backend.api.transactions import TransactionOut, hydrate_transactions
from pinch_backend.classification.consume import consume_proposal
from pinch_backend.classification.promotion import maybe_propose_rule
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


class ReviewIn(BaseModel):
    """The FINAL user data. Field-present merge against the proposal: an
    absent field means "the proposal's value", a present one is the user's
    final word. Empty body accepts as-is. notes is not reviewable — that is
    PATCH's job, and clearing display_name likewise (consume applies
    display_name only when not None)."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    category_id: uuid.UUID | None = None
    tags: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(
        default=None, max_length=50
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=100)


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
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail="Already reviewed; un-review first (PATCH reviewed: false)",
        )

    body = data if data is not None else ReviewIn()
    fields = data.model_fields_set if data is not None else set()

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

    corrected = (
        final_category != prop_category_id
        or {t.casefold() for t in final_tags} != {t.casefold() for t in proposal_tags}
        or (final_display is not None and final_display != prop_display)
    )

    await consume_proposal(
        current_ledger,
        txn,
        category_id=final_category,
        tags=final_tags,
        display_name=final_display,
        actor=CorrectionActor.USER,
    )
    rule = await maybe_propose_rule(current_ledger, txn.description_normalized, final_category)

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


reviews_router = Router(path="/api/v1/transactions", route_handlers=[review_transaction])
