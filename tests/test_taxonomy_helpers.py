"""Depth-agnostic tree helpers for the category taxonomy (M5 CP1, #19)."""

import pytest
from litestar.exceptions import ClientException

from pinch_backend import taxonomy
from pinch_backend.models import Category, Ledger, provision_user


async def _ledger(db) -> Ledger:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    return (await Ledger.all())[0]


async def test_depth_counts_from_root(db) -> None:
    ledger = await _ledger(db)
    food = await Category.create(ledger=ledger, name="Food")
    rest = await Category.create(ledger=ledger, name="Restaurants", parent=food)
    assert await taxonomy.category_depth(food) == 1
    assert await taxonomy.category_depth(rest) == 2


async def test_placement_under_a_depth_2_node_is_rejected(db) -> None:
    ledger = await _ledger(db)
    food = await Category.create(ledger=ledger, name="Food")
    rest = await Category.create(ledger=ledger, name="Restaurants", parent=food)
    with pytest.raises(ClientException):
        await taxonomy.validate_placement(ledger.id, rest)


async def test_cycle_is_rejected(db) -> None:
    ledger = await _ledger(db)
    food = await Category.create(ledger=ledger, name="Food")
    rest = await Category.create(ledger=ledger, name="Restaurants", parent=food)
    # Re-parenting Food under its own descendant Restaurants is a cycle.
    with pytest.raises(ClientException):
        await taxonomy.check_no_cycle(food, rest)


async def test_collect_descendants_is_the_closure(db) -> None:
    ledger = await _ledger(db)
    food = await Category.create(ledger=ledger, name="Food")
    rest = await Category.create(ledger=ledger, name="Restaurants", parent=food)
    coffee = await Category.create(ledger=ledger, name="Coffee", parent=food)
    other = await Category.create(ledger=ledger, name="Travel")
    ids = await taxonomy.collect_descendant_ids([food.id], ledger.id)
    assert ids == {food.id, rest.id, coffee.id}
    assert other.id not in ids
