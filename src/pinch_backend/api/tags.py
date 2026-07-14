"""/api/v1/tags — free-form labels (PRD M5 #19).

Standard domain conventions: current_ledger (I-2), Page[T], allowlist
responses, tenancy 404s, scope guard by construction.
"""

import uuid
from datetime import datetime

from ferro import transaction
from litestar import Router, delete, get, post
from litestar.di import NamedDependency
from litestar.exceptions import HTTPException, NotFoundException
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
    created_at: datetime


def _out(t: Tag) -> TagOut:
    return TagOut(id=t.id, name=t.name, created_at=t.created_at)


@post("/")
async def create_tag(data: TagCreateIn, current_ledger: NamedDependency[Ledger]) -> TagOut:
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
    return Page(items=[_out(t) for t in rows], next_cursor=next_cursor)


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


tags_router = Router(path="/api/v1/tags", route_handlers=[create_tag, list_tags, delete_tag])
