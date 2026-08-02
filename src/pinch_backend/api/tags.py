"""/api/v1/tags — free-form labels (PRD M5 #19).

Standard domain conventions: current_ledger (I-2), Page[T], allowlist
responses, tenancy 404s, scope guard by construction.
"""

import uuid
from datetime import datetime

from ferro import transaction
from litestar import Router, delete, get, patch, post
from litestar.di import NamedDependency
from litestar.exceptions import ClientException, HTTPException, NotFoundException
from litestar.params import FromPath
from litestar.status_codes import HTTP_409_CONFLICT
from pydantic import BaseModel, Field

from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.models import Ledger, Tag, TransactionTag
from pinch_backend.observability import get_logger
from pinch_backend.tags import resolve_tags

log = get_logger(__name__)


class TagCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TagOut(BaseModel):
    id: uuid.UUID
    name: str
    transaction_count: int
    net_minor: int
    """Signed sum of the tagged transactions' amounts."""
    pending_minor: int
    """The unsettled slice of net_minor — tagged transactions the
    institution still reports as pending."""
    created_at: datetime


def _out(t: Tag, agg: "_TagAggregate | None" = None) -> TagOut:
    agg = agg or _TagAggregate()
    return TagOut(
        id=t.id,
        name=t.name,
        transaction_count=agg.count,
        net_minor=agg.net_minor,
        pending_minor=agg.pending_minor,
        created_at=t.created_at,
    )


class _TagAggregate(BaseModel):
    count: int = 0
    net_minor: int = 0
    pending_minor: int = 0


async def _aggregates_for(tag_ids: list[uuid.UUID]) -> dict[uuid.UUID, _TagAggregate]:
    """Per-tag totals over the join, grouped in SQL (the M8 dict-lambda
    idiom); two queries because the pending slice is a filtered sum."""
    if not tag_ids:
        return {}
    totals = (
        await TransactionTag.select(
            lambda tt: {
                "tag": tt.tag_id,
                "n": tt.id.count(),
                "net": tt.transaction.amount_minor.sum(),
            }
        )
        .where(lambda tt, ids=tag_ids: tt.tag_id.in_(ids))
        .all()
    )
    pending = (
        await TransactionTag.select(
            lambda tt: {"tag": tt.tag_id, "net": tt.transaction.amount_minor.sum()}
        )
        .where(lambda tt, ids=tag_ids: tt.tag_id.in_(ids) & (tt.transaction.pending == True))  # noqa: E712
        .all()
    )
    out: dict[uuid.UUID, _TagAggregate] = {}
    for row in totals:
        out[row.tag] = _TagAggregate(  # ty: ignore[unresolved-attribute]
            count=row.n,  # ty: ignore[unresolved-attribute]
            net_minor=row.net or 0,  # ty: ignore[unresolved-attribute]
        )
    for row in pending:
        out.setdefault(row.tag, _TagAggregate()).pending_minor = (  # ty: ignore[unresolved-attribute]
            row.net or 0  # ty: ignore[unresolved-attribute]
        )
    return out


@post("/")
async def create_tag(data: TagCreateIn, current_ledger: NamedDependency[Ledger]) -> TagOut:
    if not data.name.strip():
        raise ClientException(detail="Tag name cannot be blank")
    fold = data.name.strip().casefold()
    ledger_id = current_ledger.id
    existing = await Tag.where(lambda t: (t.ledger_id == ledger_id) & (t.name_fold == fold)).first()
    if existing is not None:
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail="A tag with that name exists")
    (tag,) = await resolve_tags(current_ledger, [data.name])
    log.info("tag.created", tag_id=str(tag.id), ledger_id=str(current_ledger.id))
    return _out(tag)


@get("/")
async def list_tags(
    current_ledger: NamedDependency[Ledger],
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[TagOut]:
    ledger_id = current_ledger.id
    rows, next_cursor = await paginate(
        Tag.where(lambda t: t.ledger_id == ledger_id), cursor=cursor, limit=limit
    )
    aggregates = await _aggregates_for([t.id for t in rows])
    return Page(items=[_out(t, aggregates.get(t.id)) for t in rows], next_cursor=next_cursor)


@patch("/{tag_id:uuid}")
async def rename_tag(
    tag_id: FromPath[uuid.UUID],
    data: TagCreateIn,
    current_ledger: NamedDependency[Ledger],
) -> TagOut:
    """Rename in place — id stable, links untouched, so the fix lands
    everywhere the tag appears (F4 Enabler A, #66). Same identity rule as
    create: trimmed + casefolded, collisions refused (409); re-casing the
    same tag is not a collision."""
    if not data.name.strip():
        raise ClientException(detail="Tag name cannot be blank")
    ledger_id = current_ledger.id
    tag = await Tag.where(lambda t: (t.id == tag_id) & (t.ledger_id == ledger_id)).first()
    if tag is None:
        raise NotFoundException(detail="No such tag")
    name = data.name.strip()
    fold = name.casefold()
    taken = await Tag.where(
        lambda t: (t.ledger_id == ledger_id) & (t.name_fold == fold) & (t.id != tag_id)
    ).first()
    if taken is not None:
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail="A tag with that name exists")
    tag.name = name
    tag.name_fold = fold
    await tag.save()
    log.info("tag.renamed", tag_id=str(tag_id), ledger_id=str(ledger_id))
    aggregates = await _aggregates_for([tag.id])
    return _out(tag, aggregates.get(tag.id))


@delete("/{tag_id:uuid}")
async def delete_tag(tag_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]) -> None:
    ledger_id = current_ledger.id
    tag = await Tag.where(lambda t: (t.id == tag_id) & (t.ledger_id == ledger_id)).first()
    if tag is None:
        raise NotFoundException(detail="No such tag")
    async with transaction():
        await TransactionTag.where(lambda tt: tt.tag_id == tag_id).delete()
        await tag.delete()
    log.info("tag.deleted", tag_id=str(tag_id), ledger_id=str(current_ledger.id))


tags_router = Router(
    path="/api/v1/tags", route_handlers=[create_tag, list_tags, rename_tag, delete_tag]
)
