"""Rule model invariants (M5 CP2, #20)."""

from pinch_backend.models import Category, Ledger, Rule, RuleStatus, provision_user


async def _ledger(db) -> Ledger:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    return (await Ledger.all())[0]


async def test_rule_round_trips_condition_and_actions(db) -> None:
    ledger = await _ledger(db)
    cat = await Category.create(ledger=ledger, name="Groceries2")
    rule = await Rule.create(
        ledger=ledger,
        condition={"version": 1, "payee": {"op": "contains", "value": "costco"}},
        action_category=cat,
        action_add_tags=["bulk"],
        action_rename_to="Costco",
    )
    got = await Rule.get(rule.id)
    assert got.status is RuleStatus.ACTIVE  # user-created rules are law by authorship
    assert got.condition["payee"]["value"] == "costco"
    assert got.action_category_id == cat.id
    assert got.action_add_tags == ["bulk"]
    assert got.action_rename_to == "Costco"


async def test_rule_action_category_is_optional(db) -> None:
    ledger = await _ledger(db)
    rule = await Rule.create(
        ledger=ledger,
        condition={"version": 1, "payee": {"op": "equals", "value": "x"}},
        action_add_tags=["t"],
    )
    assert rule.action_category_id is None
