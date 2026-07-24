"""History matching + the classifier seam (M5 CP3, #21)."""

import uuid
from datetime import UTC, date, datetime

from pinch_backend.classification.classifier import active_classifier
from pinch_backend.classification.history import history_match
from pinch_backend.models import (
    Account,
    AccountKind,
    Category,
    Ledger,
    Transaction,
    provision_user,
)


async def _seed(db) -> tuple[Ledger, Account, Category]:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]
    account = await Account.create(ledger=ledger, kind=AccountKind.DEPOSITORY, label="Checking")
    category = await Category.create(ledger=ledger, name="Coffee Shops X")
    return ledger, account, category


async def _txn(ledger, account, payee, *, reviewed_at=None, category=None, day=1):
    return await Transaction.create(
        ledger=ledger,
        account=account,
        date=date(2026, 6, day),
        amount_minor=-500,
        currency="USD",
        description_raw=payee.upper(),
        description_normalized=payee,
        fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
        reviewed_at=reviewed_at,
        category=category,
    )


async def test_most_recent_decision_wins(db) -> None:
    ledger, account, coffee = await _seed(db)
    dining = await Category.create(ledger=ledger, name="Dining X")
    # Older transaction, NEWER decision: its category must win (Q4 — history
    # orders by reviewed_at, decision recency, not transaction date).
    await _txn(
        ledger,
        account,
        "starbucks",
        day=1,
        reviewed_at=datetime(2026, 7, 2, tzinfo=UTC),
        category=coffee,
    )
    await _txn(
        ledger,
        account,
        "starbucks",
        day=30,
        reviewed_at=datetime(2026, 7, 1, tzinfo=UTC),
        category=dining,
    )
    hit = await history_match(ledger.id, "starbucks")
    assert hit is not None
    assert hit.category_id == coffee.id


async def test_reviewed_but_uncategorized_is_not_a_signal(db) -> None:
    ledger, account, _ = await _seed(db)
    await _txn(
        ledger, account, "starbucks", reviewed_at=datetime(2026, 7, 1, tzinfo=UTC), category=None
    )
    assert await history_match(ledger.id, "starbucks") is None


async def test_unreviewed_categorized_is_not_a_signal(db) -> None:
    ledger, account, coffee = await _seed(db)
    await _txn(ledger, account, "starbucks", reviewed_at=None, category=coffee)
    assert await history_match(ledger.id, "starbucks") is None


async def test_history_is_ledger_scoped(db) -> None:
    ledger, account, coffee = await _seed(db)
    await _txn(
        ledger, account, "starbucks", reviewed_at=datetime(2026, 7, 1, tzinfo=UTC), category=coffee
    )
    assert await history_match(uuid.uuid7(), "starbucks") is None


async def test_keyless_classifier_deterministically_abstains(db) -> None:
    """M9 CP3 swapped Penny in behind the seam; keyless (the suite's
    baseline) still abstains deterministically without touching a model —
    the v0 abstainer's contract, preserved."""
    from pinch_backend.penny.categorization import PennyClassifier

    ledger, account, _ = await _seed(db)
    txn = await _txn(ledger, account, "mystery merchant")
    assert isinstance(active_classifier, PennyClassifier)
    assert await active_classifier.classify(txn) is None
