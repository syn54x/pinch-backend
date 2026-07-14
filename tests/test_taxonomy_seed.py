"""Every new ledger is born with the starter taxonomy (M5 CP1, #19)."""

from pinch_backend.models import Category, Ledger, provision_user
from pinch_backend.taxonomy import DEFAULT_TAXONOMY


async def test_provisioning_seeds_the_default_taxonomy(db) -> None:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]

    cats = await Category.where(lambda c: c.ledger_id == ledger.id).all()
    top = [c for c in cats if c.parent_id is None]
    children = [c for c in cats if c.parent_id is not None]

    assert len(top) == len(DEFAULT_TAXONOMY)
    assert len(children) == sum(len(kids) for _, kids in DEFAULT_TAXONOMY)
    # Seeds are ordinary rows: every child points at a seeded top-level parent.
    top_ids = {c.id for c in top}
    assert all(c.parent_id in top_ids for c in children)


async def test_seed_is_deletable_like_any_row(db) -> None:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]
    a_child = await Category.where(
        lambda c: (c.ledger_id == ledger.id) & (c.parent_id != None)  # noqa: E711
    ).first()
    await a_child.delete()  # no special-casing; nothing raises
