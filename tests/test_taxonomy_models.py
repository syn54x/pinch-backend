"""Model-layer invariants for the M5 CP1 tables (issue #19)."""

import pytest
from ferro import UniqueViolationError

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
