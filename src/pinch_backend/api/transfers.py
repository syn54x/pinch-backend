"""/api/v1/transfers — create, dissolve, and list transfer links (M6 CP2,
#27).

A transfer's sides are structurally directional (outflow/inflow), placed by
sign, never by argument order. Creation vacates the members' categories —
being a transfer IS the classification — and neither creation nor
dissolution touches review state or the correction log (edits edit, review
reviews; consume-awareness is CP3's slice). current_ledger (I-2), Page[T],
allowlist responses, tenancy 404s, scope guard by construction.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from ferro import UniqueViolationError, transaction
from litestar import Router, delete, get, post
from litestar.di import NamedDependency
from litestar.exceptions import ClientException, NotFoundException
from litestar.params import FromPath, QueryParameter
from litestar.status_codes import HTTP_409_CONFLICT, HTTP_422_UNPROCESSABLE_ENTITY
from pydantic import BaseModel, ConfigDict, Field

from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.models import Ledger, SplitLine, Transaction, Transfer
from pinch_backend.observability import get_logger

log = get_logger(__name__)

OCCUPIED_DETAIL = "A transaction belongs to at most one transfer; dissolve the existing one first"


class TransferKind(StrEnum):
    """Derived, never stored: both sides present = linked, one = untracked."""

    LINKED = "linked"
    UNTRACKED = "untracked"


class TransferCreateIn(BaseModel):
    """One or two transaction ids — the sides sort themselves by sign.
    Unknown keys are a 400 (extra="forbid")."""

    model_config = ConfigDict(use_attribute_docstrings=True, extra="forbid")

    transaction_ids: list[uuid.UUID] = Field(min_length=1, max_length=2)


class TransferOut(BaseModel):
    """What a client may see about a transfer — an allowlist."""

    id: uuid.UUID
    kind: TransferKind
    outflow_transaction_id: uuid.UUID | None
    inflow_transaction_id: uuid.UUID | None
    created_at: datetime


def kind_of(transfer: Transfer) -> TransferKind:
    both = (
        transfer.outflow_transaction_id is not None  # ty: ignore[unresolved-attribute]
        and transfer.inflow_transaction_id is not None  # ty: ignore[unresolved-attribute]
    )
    return TransferKind.LINKED if both else TransferKind.UNTRACKED


def _out(transfer: Transfer) -> TransferOut:
    return TransferOut(
        id=transfer.id,
        kind=kind_of(transfer),
        outflow_transaction_id=transfer.outflow_transaction_id,  # ty: ignore[unresolved-attribute]
        inflow_transaction_id=transfer.inflow_transaction_id,  # ty: ignore[unresolved-attribute]
        created_at=transfer.created_at,
    )


def _unprocessable(detail: str) -> ClientException:
    return ClientException(detail=detail, status_code=HTTP_422_UNPROCESSABLE_ENTITY)


async def assert_not_in_transfer(txn_id: "uuid.UUID") -> None:
    """Split x transfer exclusivity, transfer side (M6 CP3): a transaction
    already in a transfer refuses a split document. Public: put_splits and
    the review-with-splits motion both guard through this."""
    occupied = await Transfer.where(
        lambda tr, tid=txn_id: (
            (tr.outflow_transaction_id == tid) | (tr.inflow_transaction_id == tid)
        )
    ).first()
    if occupied is not None:
        raise ClientException(
            detail="Transaction is in a transfer; dissolve it before splitting",
            status_code=HTTP_409_CONFLICT,
        )


async def establish_transfer(ledger: Ledger, txns: list[Transaction]) -> Transfer:
    """Validate and create a transfer link, vacating the members' categories
    — the implementation behind POST /transfers and the review-with-
    transfer motion (CP3). Raises 422 on pair-shape violations, 409 on a
    split member or an occupied one. Opens a transaction that nests under a
    caller's, so a review motion stays one atomic write.

    One deliberate sibling exists (M7 CP4): consume's detection-accept
    re-validates the same invariants in ``_eligible_counterpart`` with a
    degrade-not-error posture — a stale proposal accepts without a link
    instead of failing the review. Keep the invariant lists in sync."""
    for txn in txns:
        if txn.amount_minor == 0:
            raise _unprocessable("Zero-amount transactions cannot be linked as transfers")

    if len(txns) == 2:
        first, second = txns
        if (first.amount_minor < 0) == (second.amount_minor < 0):
            raise _unprocessable(
                "A linked transfer needs one negative and one positive transaction"
            )
        if abs(first.amount_minor) != abs(second.amount_minor):
            raise _unprocessable("Linked transfer sides must have equal magnitudes")
        if first.currency != second.currency:
            raise _unprocessable("Linked transfer sides must share a currency")
        if first.account_id == second.account_id:  # ty: ignore[unresolved-attribute]
            raise _unprocessable("Linked transfer sides must be on different accounts")
        outflow, inflow = (first, second) if first.amount_minor < 0 else (second, first)
    else:
        (only,) = txns
        outflow, inflow = (only, None) if only.amount_minor < 0 else (None, only)

    member_ids = [t.id for t in txns]
    split_member = await SplitLine.where(
        lambda ln, ids=member_ids: ln.transaction_id.in_(ids)
    ).first()
    if split_member is not None:
        # Split x transfer exclusivity, split side (CP3).
        raise ClientException(
            detail="Split transactions cannot join a transfer; unsplit first",
            status_code=HTTP_409_CONFLICT,
        )

    # Friendly pre-check; the unique FK indexes remain the race-proof
    # enforcement (CP0-verified) — a concurrent winner surfaces below as
    # UniqueViolationError and answers the same 409.
    occupied = await Transfer.where(
        lambda tr, wanted=member_ids: (
            (tr.outflow_transaction_id.in_(wanted)) | (tr.inflow_transaction_id.in_(wanted))
        )
    ).first()
    if occupied is not None:
        raise ClientException(detail=OCCUPIED_DETAIL, status_code=HTTP_409_CONFLICT)

    try:
        async with transaction():
            transfer = await Transfer.create(
                ledger=ledger,
                outflow_transaction_id=outflow.id if outflow else None,
                inflow_transaction_id=inflow.id if inflow else None,
            )
            # In a transfer => category NULL, both sides (creation vacates).
            for txn in txns:
                txn.category_id = None  # ty: ignore[unresolved-attribute]
                await txn.save()
    except UniqueViolationError:
        raise ClientException(detail=OCCUPIED_DETAIL, status_code=HTTP_409_CONFLICT) from None
    return transfer


@post("/")
async def create_transfer(
    data: TransferCreateIn, current_ledger: NamedDependency[Ledger]
) -> TransferOut:
    ids = list(data.transaction_ids)
    if len(ids) != len(set(ids)):
        raise _unprocessable("A linked transfer needs two distinct transactions")
    ledger_id = current_ledger.id
    txns = await Transaction.where(
        lambda t, wanted=ids, lid=ledger_id: (t.id.in_(wanted)) & (t.ledger_id == lid)
    ).all()
    if len(txns) < len(ids):
        # Cross-ledger and nonexistent ids answer identically — a foreign id's
        # existence is never confirmed.
        raise NotFoundException(detail="No such transaction")

    transfer = await establish_transfer(current_ledger, list(txns))
    log.info(
        "transfer.created",
        transfer_id=str(transfer.id),
        ledger_id=str(ledger_id),
        kind=kind_of(transfer).value,
    )
    return _out(transfer)


@delete("/{transfer_id:uuid}")
async def dissolve_transfer(
    transfer_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> None:
    """Dissolve the link. Members stay exactly as they are — reviewed if they
    were reviewed, uncategorized either way (vacating is not undone)."""
    transfer = await Transfer.where(
        lambda tr: (tr.id == transfer_id) & (tr.ledger_id == current_ledger.id)
    ).first()
    if transfer is None:
        raise NotFoundException(detail="No such transfer")
    await transfer.delete()
    log.info("transfer.dissolved", transfer_id=str(transfer_id), ledger_id=str(current_ledger.id))


@get("/")
async def list_transfers(
    current_ledger: NamedDependency[Ledger],
    account_id: Annotated[list[uuid.UUID] | None, QueryParameter()] = None,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[TransferOut]:
    """The transfer register (parity — and M8's loan-payment input made
    user-inspectable). ``account_id`` matches either side."""
    ledger_id = current_ledger.id
    query = Transfer.where(lambda tr: tr.ledger_id == ledger_id)
    if account_id:
        accounts = list(account_id)
        # Both edges explicitly LEFT: an untracked transfer has one NULL side,
        # and an INNER traversal there would drop it before the OR could
        # evaluate the populated side.
        query = (
            query.left_join(lambda tr: tr.outflow_transaction)
            .left_join(lambda tr: tr.inflow_transaction)
            .where(
                lambda tr: (
                    (tr.outflow_transaction.account_id.in_(accounts))
                    | (tr.inflow_transaction.account_id.in_(accounts))
                )
            )
        )
    rows, next_cursor = await paginate(query, cursor=cursor, limit=limit)
    return Page(items=[_out(t) for t in rows], next_cursor=next_cursor)


transfers_router = Router(
    path="/api/v1/transfers",
    route_handlers=[create_transfer, dissolve_transfer, list_transfers],
)
