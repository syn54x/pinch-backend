"""The category taxonomy: the seeded starter set and depth-agnostic tree
helpers (PRD M5, issue #19).

The two-level depth cap lives in exactly one constant, ``MAX_DEPTH``. Every
helper walks until done rather than assuming a depth, so raising the cap is a
one-line change and nothing else in the system knows the number.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from litestar.exceptions import ClientException

from pinch_backend.models import Category

if TYPE_CHECKING:
    import uuid

MAX_DEPTH = 2
"""Top-level groups plus one child level (CONTEXT.md: Food → Restaurants).
The only place the depth is written down."""


async def category_depth(category: Category) -> int:
    """1 for a root, 2 for its child, … — walk to the root, counting hops."""
    depth = 1
    current = category
    while current.parent_id is not None:  # ty: ignore
        parent = await Category.get(current.parent_id)  # ty: ignore
        depth += 1
        current = parent
    return depth


async def validate_placement(ledger_id: uuid.UUID, parent: Category | None) -> None:
    """Reject (400) a child placed under ``parent`` if it would exceed the cap.
    A root (parent None) is always depth 1 and always allowed."""
    if parent is None:
        return
    if await category_depth(parent) >= MAX_DEPTH:
        raise ClientException(detail=f"Categories may nest at most {MAX_DEPTH} levels deep")


async def check_no_cycle(category: Category, new_parent: Category | None) -> None:
    """Reject (400) re-parenting ``category`` under itself or a descendant."""
    current = new_parent
    while current is not None:
        if current.id == category.id:
            raise ClientException(detail="A category cannot be its own ancestor")
        current = (
            await Category.get(current.parent_id)  # ty: ignore
            if current.parent_id  # ty: ignore
            else None
        )


async def collect_descendant_ids(root_ids: list[uuid.UUID], ledger_id: uuid.UUID) -> set[uuid.UUID]:
    """The closure of ``root_ids`` and all their descendants within the ledger.
    One query loads the ledger's categories (tiny); the walk is in memory."""
    cats = await Category.where(lambda c: c.ledger_id == ledger_id).all()
    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    for c in cats:
        if c.parent_id is not None:  # ty: ignore
            children.setdefault(c.parent_id, []).append(c.id)  # ty: ignore
    result: set[uuid.UUID] = set()
    stack = list(root_ids)
    while stack:
        node = stack.pop()
        if node in result:
            continue
        result.add(node)
        stack.extend(children.get(node, ()))
    return result
