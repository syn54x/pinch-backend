"""Model-layer invariants for the M5 CP1 tables (issue #19)."""

import pytest
from ferro import ForeignKeyViolationError, OperationalError, UniqueViolationError

from pinch_backend.models import (
    Category,
    Ledger,
    Tag,
    provision_user,
)


async def _ledger(db) -> Ledger:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    return (await Ledger.all())[0]


async def test_category_parent_is_a_nullable_self_reference(db) -> None:
    ledger = await _ledger(db)
    food = await Category.create(ledger=ledger, name="Food")
    rest = await Category.create(ledger=ledger, name="Restaurants", parent=food)

    assert rest.parent_id == food.id
    children = await Category.where(lambda c: c.parent_id == food.id).all()
    assert [c.name for c in children] == ["Restaurants"]
    roots = await Category.where(lambda c: c.parent_id == None).all()  # noqa: E711
    assert food.id in {c.id for c in roots}


async def test_tag_fold_is_unique_per_ledger(db) -> None:
    ledger = await _ledger(db)
    await Tag.create(ledger=ledger, name="Vacation", name_fold="vacation")
    with pytest.raises(UniqueViolationError):
        await Tag.create(ledger=ledger, name="vacation", name_fold="vacation")


async def test_category_parent_fk_restricts_delete_at_the_db(db) -> None:
    """PR #23 review, finding 2: Category.parent is ON DELETE RESTRICT — a
    backstop behind the API's 409 children-block, driven here directly at
    the model seam (bypassing the API guard) to prove the DB itself refuses
    the cascade rather than silently deleting the child along with it.

    The exception CLASS is Postgres-version-dependent, so this test accepts
    both: ferro's delete-path classification is sensitive to the server's
    error wording — PG 17's "violates foreign key constraint" maps to
    ForeignKeyViolationError, while PG 18's new "violates RESTRICT setting
    of foreign key constraint" wording falls through to OperationalError
    with sqlstate=None (ferro-orm#306). Pin ForeignKeyViolationError alone
    once ferro classifies by SQLSTATE. What matters here is the invariant:
    the DB rejects the delete outright rather than cascading."""
    ledger = await _ledger(db)
    food = await Category.create(ledger=ledger, name="Food")
    child = await Category.create(ledger=ledger, name="Restaurants", parent=food)
    with pytest.raises((ForeignKeyViolationError, OperationalError)):
        await food.delete()
    assert await Category.get(food.id) is not None
    fresh_child = await Category.get(child.id)
    assert fresh_child.parent_id == food.id  # ty: ignore[unresolved-attribute]
