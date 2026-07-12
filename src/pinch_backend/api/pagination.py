"""Cursor pagination: the list-endpoint convention every milestone copies
(PRD M3, issue #9).

Keyset pagination over UUIDv7 primary keys: the cursor is the id of the last
item on the previous page, and a page is "the next ``limit`` rows with a
greater id". uuid7 ids are time-ordered, so this is creation order, stable
under concurrent inserts, and needs no OFFSET scan. A cursor is a *position*,
not a row reference — deleting the row it names does not invalidate it, so
list-and-revoke loops (sessions, PATs) page correctly.
"""

import uuid
from typing import TYPE_CHECKING, Annotated, Protocol

if TYPE_CHECKING:
    from ferro.query import Query

from litestar.exceptions import ClientException
from litestar.params import QueryParameter
from pydantic import BaseModel


class HasUuid7Id(Protocol):
    """What a row must offer to be keyset-paginated: the uuid7 primary key
    every table has by the M1 model conventions."""

    id: uuid.UUID


DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100

CursorParam = Annotated[
    str | None,
    QueryParameter(description="Opaque page position from a previous `next_cursor`."),
]
"""Declare list-endpoint ``cursor`` params with this; default it to None."""

LimitParam = Annotated[
    int,
    QueryParameter(
        ge=1,
        le=MAX_PAGE_LIMIT,
        description=f"Page size, 1-{MAX_PAGE_LIMIT}.",
    ),
]
"""Declare list-endpoint ``limit`` params with this: out-of-range values
answer 400 in the error envelope, never a silently clamped page."""


class Page[ItemT](BaseModel):
    """The one list-response envelope (M3 story 9): M4's first domain list
    endpoint returns this same shape or it's wrong."""

    items: list[ItemT]
    next_cursor: str | None
    """Pass back as ``?cursor=`` for the next page; null means exhausted —
    it signals "no more rows", never "the page happened to be full"."""


def decode_cursor(cursor: str) -> uuid.UUID:
    """A cursor is the canonical string form of a row id; anything else is a
    client error. The detail never echoes the value — request bodies and
    query strings don't get reflected into responses."""
    try:
        return uuid.UUID(cursor)
    except ValueError:
        raise ClientException(detail="Invalid cursor") from None


async def paginate[ModelT: HasUuid7Id](
    query: Query[ModelT], *, cursor: str | None, limit: int
) -> tuple[list[ModelT], str | None]:
    """Run ``query`` as one keyset page: id-ascending, ``limit`` rows, plus
    one probe row to learn whether a next page exists without a COUNT."""
    if cursor is not None:
        after = decode_cursor(cursor)
        query = query.where(lambda row: row.id > after)
    rows = await query.order_by(lambda row: row.id).limit(limit + 1).all()
    if len(rows) > limit:
        return rows[:limit], str(rows[limit - 1].id)
    return rows, None
