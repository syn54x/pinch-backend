"""/api/v1/categories — the editable classification taxonomy (PRD M5 #19).

Same conventions as every domain surface: current_ledger (I-2), Page[T]
lists, allowlist responses, tenancy 404s, and the scope guard by
construction on every unsafe method. The two-level depth cap and cycle
prevention live in pinch_backend.taxonomy; nothing here hardcodes a depth.
"""

import uuid
from datetime import datetime

from ferro import transaction
from litestar import Router, delete, get, patch, post
from litestar.di import NamedDependency
from litestar.exceptions import ClientException, NotFoundException
from litestar.params import FromPath
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pinch_backend import taxonomy
from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.models import (
    Category,
    Ledger,
    Proposal,
    ProposalProvenance,
    Rule,
    SplitLine,
    Transaction,
    utcnow,
)
from pinch_backend.observability import get_logger

log = get_logger(__name__)


class CategoryCreateIn(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    name: str = Field(min_length=1, max_length=100)
    parent_id: uuid.UUID | None = None
    """A top-level node when null; otherwise nests under the named parent
    (depth-capped, validated server-side)."""


class CategoryUpdateIn(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    parent_id: uuid.UUID | None = None
    """Re-parent target. Present-and-null moves the node to top level;
    absent leaves the parent unchanged (see reparent field)."""
    reparent: bool = False
    """True to apply parent_id (including null → top level). Distinguishes
    "move to top level" from "don't touch the parent" without a sentinel."""

    @model_validator(mode="after")
    def _parent_id_requires_reparent(self) -> "CategoryUpdateIn":
        if "parent_id" in self.model_fields_set and not self.reparent:
            raise ValueError("parent_id requires reparent: true")
        return self


class CategoryDeleteIn(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    reassign_to: uuid.UUID | None
    """Where this category's transactions go: another category, or null to
    make them uncategorized. Required — no default — because silently
    uncategorizing a year of history is exactly what we refuse to do (I-1)."""


class CategoryOut(BaseModel):
    """What a client may see about a category — an allowlist, never the row."""

    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    created_at: datetime


def _out(c: Category) -> CategoryOut:
    return CategoryOut(
        id=c.id,
        name=c.name,
        parent_id=c.parent_id,  # ty: ignore[unresolved-attribute]
        created_at=c.created_at,
    )


async def _get(ledger: Ledger, category_id: uuid.UUID) -> Category:
    c = await Category.where(lambda x: (x.id == category_id) & (x.ledger_id == ledger.id)).first()
    if c is None:
        raise NotFoundException(detail="No such category")
    return c


async def _assert_sibling_name_free(
    ledger_id: uuid.UUID, parent_id: uuid.UUID | None, name: str, exclude: uuid.UUID | None
) -> None:
    """Sibling names are unique (works for null and non-null parents, which a
    DB unique on a nullable column cannot guarantee alone). Compared
    trimmed+casefolded — same identity rule as Tag.name_fold — so "Coffee "
    and "coffee" collide; stored casing is preserved as-entered."""
    siblings = await Category.where(
        lambda c: (c.ledger_id == ledger_id) & (c.parent_id == parent_id)
    ).all()
    fold = name.strip().casefold()
    if any(s.name.strip().casefold() == fold and s.id != exclude for s in siblings):
        raise ClientException(detail="A sibling category already has that name")


@post("/")
async def create_category(
    data: CategoryCreateIn, current_ledger: NamedDependency[Ledger]
) -> CategoryOut:
    parent = await _get(current_ledger, data.parent_id) if data.parent_id else None
    await taxonomy.validate_placement(parent)
    await _assert_sibling_name_free(current_ledger.id, data.parent_id, data.name, None)
    category = await Category.create(ledger=current_ledger, name=data.name, parent=parent)
    log.info("category.created", category_id=str(category.id), ledger_id=str(current_ledger.id))
    return _out(category)


@get("/")
async def list_categories(
    current_ledger: NamedDependency[Ledger],
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[CategoryOut]:
    ledger_id = current_ledger.id
    rows, next_cursor = await paginate(
        Category.where(lambda c: c.ledger_id == ledger_id), cursor=cursor, limit=limit
    )
    return Page(items=[_out(c) for c in rows], next_cursor=next_cursor)


@get("/{category_id:uuid}")
async def get_category(
    category_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> CategoryOut:
    return _out(await _get(current_ledger, category_id))


@patch("/{category_id:uuid}")
async def update_category(
    category_id: FromPath[uuid.UUID],
    data: CategoryUpdateIn,
    current_ledger: NamedDependency[Ledger],
) -> CategoryOut:
    category = await _get(current_ledger, category_id)
    new_parent_id = category.parent_id  # ty: ignore[unresolved-attribute]
    if data.reparent:
        new_parent = await _get(current_ledger, data.parent_id) if data.parent_id else None
        await taxonomy.check_no_cycle(category, new_parent)
        # Depth: the *moved subtree* must fit under the new parent within the
        # cap — checking only the new parent's depth would let a node with
        # children land one level too deep (D3).
        new_node_depth = (await taxonomy.category_depth(new_parent) + 1) if new_parent else 1
        if new_node_depth + await taxonomy.subtree_height(category) - 1 > taxonomy.MAX_DEPTH:
            raise ClientException(
                detail=f"Re-parenting would nest deeper than {taxonomy.MAX_DEPTH} levels"
            )
        category.parent_id = new_parent.id if new_parent else None  # ty: ignore[unresolved-attribute]
        new_parent_id = data.parent_id
    if data.name is not None:
        category.name = data.name
    await _assert_sibling_name_free(current_ledger.id, new_parent_id, category.name, category.id)
    await category.save()
    log.info("category.updated", category_id=str(category.id), ledger_id=str(current_ledger.id))
    return _out(category)


@delete("/{category_id:uuid}")
async def delete_category(
    category_id: FromPath[uuid.UUID],
    data: CategoryDeleteIn,
    current_ledger: NamedDependency[Ledger],
) -> None:
    """Hard delete with an explicit disposition (CONTEXT.md / D4). Children and
    targeting rules block (409); transactions and pending proposals re-point at
    the target (or empty, on a null disposition). The request carries a JSON
    body — unusual for DELETE; some proxies strip DELETE bodies, so scripting
    clients should send it explicitly."""
    category = await _get(current_ledger, category_id)
    if data.reassign_to == category_id:
        raise ClientException(
            detail="Cannot reassign a category's transactions to itself", status_code=409
        )
    child = await Category.where(lambda c: c.parent_id == category_id).first()
    if child is not None:
        raise ClientException(
            detail="Move or delete this category's children first", status_code=409
        )
    blocking = await Rule.where(lambda r: r.action_category_id == category_id).all()
    if blocking:
        raise ClientException(
            detail="Retarget or delete the rules targeting this category first",
            status_code=409,
            extra={"rules": [str(r.id) for r in blocking]},
        )
    target: Category | None = None
    if data.reassign_to is not None:
        target = await _get(current_ledger, data.reassign_to)
    cid = category.id
    async with transaction():
        await Transaction.where(lambda t: t.category_id == cid).update(
            category_id=target.id if target else None, updated_at=utcnow()
        )
        # The guarded disposition extends to split lines (M6 CP1): re-pointed
        # at the target, or nulled to an uncategorized line. The FK's SET NULL
        # remains the backstop if this path is ever missed.
        await SplitLine.where(lambda ln: ln.category_id == cid).update(
            category_id=target.id if target else None, updated_at=utcnow()
        )
        # Pending proposals follow the disposition (PRD M5 D4): re-pointed at
        # the target, or emptied to provenance=none — the pipeline's decision
        # died with the category. Tags/rename survive; they were never the
        # category's decision. Must precede the delete (FK cascade).
        if target is not None:
            await Proposal.where(lambda p: p.category_id == cid).update(
                category_id=target.id, updated_at=utcnow()
            )
        else:
            await Proposal.where(lambda p: p.category_id == cid).update(
                category_id=None,
                provenance=ProposalProvenance.NONE,
                provenance_detail=None,
                updated_at=utcnow(),
            )
        await category.delete()
    log.info(
        "category.deleted",
        category_id=str(cid),
        ledger_id=str(current_ledger.id),
        reassigned_to=str(target.id) if target else None,
    )


categories_router = Router(
    path="/api/v1/categories",
    route_handlers=[
        create_category,
        list_categories,
        get_category,
        update_category,
        delete_category,
    ],
)
