"""/api/v1/transactions — the transaction list, get, and user-data PATCH
(PRD M5 #19).

The inbox and every classification screen read from this list: it inlines
the assigned category and tags (batch-fetched per page — no N+1, and never
via INNER-join traversal that would drop uncategorized rows), and orders
newest-first behind a composite (date, id) keyset cursor. current_ledger
(I-2), Page[T], allowlist responses, tenancy 404s, scope guard by
construction throughout.
"""

import uuid
from datetime import date, datetime
from typing import Annotated

from litestar import Router, get, patch
from litestar.di import NamedDependency
from litestar.exceptions import NotFoundException
from litestar.params import FromPath, QueryParameter
from pydantic import BaseModel, ConfigDict

from pinch_backend import taxonomy
from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate_by_date,
)
from pinch_backend.models import Category, Ledger, Tag, Transaction, TransactionTag
from pinch_backend.observability import get_logger
from pinch_backend.tags import resolve_tags

log = get_logger(__name__)


class CategoryRef(BaseModel):
    id: uuid.UUID
    name: str


class TagRef(BaseModel):
    id: uuid.UUID
    name: str


class TransactionOut(BaseModel):
    """What a client may see about a transaction — an allowlist (M5 CP1).
    A ``proposal`` field is added additively in CP3."""

    id: uuid.UUID
    account_id: uuid.UUID
    date: date
    amount_minor: int
    currency: str
    description_raw: str
    description_normalized: str
    display_name: str | None
    notes: str | None
    reviewed_at: datetime | None
    category: CategoryRef | None
    tags: list[TagRef]
    created_at: datetime


class TransactionPatchIn(BaseModel):
    """User-data allowlist (M5). Only the fields present in the request body
    are applied — source data (date, amount, description, fingerprint) is not
    addressable here."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    category_id: uuid.UUID | None = None
    """Present-and-null clears the category (→ uncategorized)."""
    tags: list[str] | None = None
    """The complete tag set for the transaction; reconciled (implicit-create
    new names, detach removed ones). Present-and-empty clears all tags."""
    display_name: str | None = None
    notes: str | None = None
    reviewed: bool | None = None
    """True sets reviewed_at to now; False clears it (back to the inbox)."""


async def _get(ledger: Ledger, txn_id: uuid.UUID) -> Transaction:
    txn = await Transaction.where(lambda t: (t.id == txn_id) & (t.ledger_id == ledger.id)).first()
    if txn is None:
        raise NotFoundException(detail="No such transaction")
    return txn


async def _out_page(txns: list[Transaction]) -> list[TransactionOut]:
    """Batch-hydrate categories and tags for a page in two queries each,
    never per-row."""
    cat_ids = sorted({t.category_id for t in txns if t.category_id is not None})  # ty: ignore[unresolved-attribute]
    cats = (
        {c.id: c for c in await Category.where(lambda c: c.id.in_(cat_ids)).all()}
        if cat_ids
        else {}
    )
    txn_ids = [t.id for t in txns]
    links = (
        await TransactionTag.where(lambda tt: tt.transaction_id.in_(txn_ids)).all()
        if txn_ids
        else []
    )
    tag_ids = sorted({link.tag_id for link in links})  # ty: ignore[unresolved-attribute]
    tags = {t.id: t for t in await Tag.where(lambda t: t.id.in_(tag_ids)).all()} if tag_ids else {}
    by_txn: dict[uuid.UUID, list[TagRef]] = {}
    for link in links:
        tag = tags[link.tag_id]  # ty: ignore[unresolved-attribute]
        by_txn.setdefault(link.transaction_id, []).append(  # ty: ignore[unresolved-attribute]
            TagRef(id=tag.id, name=tag.name)
        )
    result = []
    for t in txns:
        cat = cats.get(t.category_id) if t.category_id else None  # ty: ignore[unresolved-attribute]
        result.append(
            TransactionOut(
                id=t.id,
                account_id=t.account_id,  # ty: ignore[unresolved-attribute]
                date=t.date,
                amount_minor=t.amount_minor,
                currency=t.currency,
                description_raw=t.description_raw,
                description_normalized=t.description_normalized,
                display_name=t.display_name,
                notes=t.notes,
                reviewed_at=t.reviewed_at,
                category=CategoryRef(id=cat.id, name=cat.name) if cat else None,
                tags=by_txn.get(t.id, []),
                created_at=t.created_at,
            )
        )
    return result


@get("/")
async def list_transactions(
    current_ledger: NamedDependency[Ledger],
    account_id: Annotated[list[uuid.UUID] | None, QueryParameter()] = None,
    date_from: Annotated[date | None, QueryParameter()] = None,
    date_to: Annotated[date | None, QueryParameter()] = None,
    reviewed: Annotated[bool | None, QueryParameter()] = None,
    category_id: Annotated[list[uuid.UUID] | None, QueryParameter()] = None,
    uncategorized: Annotated[bool | None, QueryParameter()] = None,
    tag: Annotated[list[str] | None, QueryParameter()] = None,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[TransactionOut]:
    ledger_id = current_ledger.id
    query = Transaction.where(lambda t: t.ledger_id == ledger_id)

    if account_id:
        accounts = list(account_id)
        query = query.where(lambda t: t.account_id.in_(accounts))
    if date_from is not None:
        start = date_from
        query = query.where(lambda t: t.date >= start)
    if date_to is not None:
        end = date_to
        query = query.where(lambda t: t.date <= end)
    if reviewed is True:
        query = query.where(lambda t: t.reviewed_at != None)  # noqa: E711
    elif reviewed is False:
        query = query.where(lambda t: t.reviewed_at == None)  # noqa: E711
    if uncategorized:
        query = query.where(lambda t: t.category_id == None)  # noqa: E711
    if category_id:
        subtree = await taxonomy.collect_descendant_ids(list(category_id), ledger_id)
        ids = sorted(subtree)
        query = query.where(lambda t: t.category_id.in_(ids))
    if tag:
        wanted = list(tag)
        wanted_folds = sorted({name.strip().casefold() for name in wanted})
        matched_tags = await Tag.where(
            lambda t: (t.ledger_id == ledger_id) & (t.name_fold.in_(wanted_folds))
        ).all()
        if len(matched_tags) < len(wanted_folds):
            return Page(items=[], next_cursor=None)  # an unknown tag matches nothing
        keep: set[uuid.UUID] | None = None
        for tg in matched_tags:
            tid = tg.id
            links = await TransactionTag.where(lambda tt, tid=tid: tt.tag_id == tid).all()
            ids_for_tag = {link.transaction_id for link in links}  # ty: ignore[unresolved-attribute]
            keep = ids_for_tag if keep is None else (keep & ids_for_tag)
        keep_ids = sorted(keep or set())
        if not keep_ids:
            return Page(items=[], next_cursor=None)
        query = query.where(lambda t: t.id.in_(keep_ids))

    rows, next_cursor = await paginate_by_date(query, cursor=cursor, limit=limit)
    return Page(items=await _out_page(rows), next_cursor=next_cursor)


@get("/{txn_id:uuid}")
async def get_transaction(
    txn_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> TransactionOut:
    txn = await _get(current_ledger, txn_id)
    (out,) = await _out_page([txn])
    return out


@patch("/{txn_id:uuid}")
async def patch_transaction(
    txn_id: FromPath[uuid.UUID],
    data: TransactionPatchIn,
    current_ledger: NamedDependency[Ledger],
) -> TransactionOut:
    from pinch_backend.models import utcnow

    txn = await _get(current_ledger, txn_id)
    fields = data.model_fields_set

    if "category_id" in fields:
        if data.category_id is not None:
            category = await Category.where(
                lambda c: (c.id == data.category_id) & (c.ledger_id == current_ledger.id)
            ).first()
            if category is None:
                raise NotFoundException(detail="No such category")
            txn.category_id = category.id  # ty: ignore[unresolved-attribute]
        else:
            txn.category_id = None  # ty: ignore[unresolved-attribute]
    if "display_name" in fields:
        txn.display_name = data.display_name
    if "notes" in fields:
        txn.notes = data.notes
    if "reviewed" in fields:
        txn.reviewed_at = utcnow() if data.reviewed else None
    await txn.save()

    if "tags" in fields:
        wanted = await resolve_tags(current_ledger, data.tags or [])
        wanted_ids = {t.id for t in wanted}
        tid = txn.id
        existing = await TransactionTag.where(lambda tt: tt.transaction_id == tid).all()
        existing_ids = {tt.tag_id for tt in existing}  # ty: ignore[unresolved-attribute]
        for tt in existing:
            if tt.tag_id not in wanted_ids:  # ty: ignore[unresolved-attribute]
                await tt.delete()
        for tg in wanted:
            if tg.id not in existing_ids:
                await TransactionTag.create(ledger=current_ledger, transaction=txn, tag=tg)

    log.info("transaction.updated", transaction_id=str(txn.id), ledger_id=str(current_ledger.id))
    (out,) = await _out_page([txn])
    return out


transactions_router = Router(
    path="/api/v1/transactions",
    route_handlers=[list_transactions, get_transaction, patch_transaction],
)
