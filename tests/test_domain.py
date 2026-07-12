"""M1 domain-core seam: model-layer behavior against a real database.

Each test asserts a PRD invariant (issue #2): provisioning atomicity, ledger
tenancy, enum closure, UUIDv7 ordering, archive-not-delete. Kept minimal —
M3's API tests re-cover these behaviors from above.
"""

from datetime import timedelta

import pytest
from ferro import UniqueViolationError, evict_instance
from pydantic import ValidationError

from pinch_backend.models import (
    Account,
    AccountKind,
    Connection,
    ConnectionProvider,
    ConnectionStatus,
    Ledger,
    LedgerMember,
    LedgerRole,
    User,
    provision_user,
)


async def test_provisioning_yields_user_with_exactly_one_owned_ledger(db) -> None:
    user = await provision_user(email="taylor@example.com", display_name="Taylor")

    memberships = await LedgerMember.where(lambda m: m.user_id == user.id).all()
    assert len(memberships) == 1
    assert memberships[0].role == LedgerRole.OWNER
    assert await Ledger.select().count() == 1


async def test_provisioning_rolls_back_atomically_on_failure(db) -> None:
    await provision_user(email="taylor@example.com", display_name="Taylor")

    with pytest.raises(UniqueViolationError):
        await provision_user(email="taylor@example.com", display_name="Impostor")

    # The failed attempt's ledger and membership writes rolled back with it.
    assert await Ledger.select().count() == 1
    assert await LedgerMember.select().count() == 1


async def test_same_email_cannot_register_twice_regardless_of_case(db) -> None:
    user = await provision_user(email="Taylor@Example.com", display_name="Taylor")
    assert user.email == "taylor@example.com"

    with pytest.raises(UniqueViolationError):
        await provision_user(email="taylor@example.COM", display_name="Taylor Again")


async def test_ledger_membership_is_unique_per_user_and_ledger(db) -> None:
    user = await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]

    with pytest.raises(UniqueViolationError):
        await LedgerMember.create(user=user, ledger=ledger, role=LedgerRole.OWNER)


async def test_an_account_requires_a_ledger(db) -> None:
    with pytest.raises(ValidationError, match="ledger is required"):
        Account(kind=AccountKind.DEPOSITORY, label="Checking")


async def test_a_manual_account_has_no_connection_and_round_trips(db) -> None:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]

    account = await Account.create(
        ledger=ledger, kind=AccountKind.ASSET, label="House", currency="USD"
    )
    evict_instance("Account", str(account.id))

    fetched = await Account.get(account.id)
    assert fetched.connection_id is None
    assert fetched.provider_account_id is None
    assert fetched.kind == AccountKind.ASSET
    assert fetched.label == "House"
    assert fetched.currency == "USD"
    assert fetched.ledger_id == ledger.id


async def test_a_connected_account_round_trips_its_provider_ids(db) -> None:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]

    connection = await Connection.create(
        ledger=ledger,
        provider=ConnectionProvider.PLAID,
        provider_item_id="item-abc123",
    )
    account = await Account.create(
        ledger=ledger,
        kind=AccountKind.DEPOSITORY,
        label="Checking",
        connection=connection,
        provider_account_id="plaid-acct-xyz",
    )
    evict_instance("Connection", str(connection.id))
    evict_instance("Account", str(account.id))

    fetched_connection = await Connection.get(connection.id)
    assert fetched_connection.provider == ConnectionProvider.PLAID
    assert fetched_connection.provider_item_id == "item-abc123"
    assert fetched_connection.status == ConnectionStatus.ACTIVE
    assert fetched_connection.last_synced_at is None
    assert fetched_connection.error_detail is None

    fetched_account = await Account.get(account.id)
    assert fetched_account.connection_id == connection.id
    assert fetched_account.provider_account_id == "plaid-acct-xyz"
    assert (await fetched_account.connection).provider_item_id == "item-abc123"


async def test_account_kind_and_connection_status_reject_unknown_values(db) -> None:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]

    with pytest.raises(ValidationError):
        Account(ledger=ledger, kind="chequing", label="Nope")

    with pytest.raises(ValidationError):
        Connection(ledger=ledger, provider_item_id="item-1", status="syncing")


async def test_uuidv7_ids_sort_by_creation_order(db) -> None:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]

    ids = [
        (await Account.create(ledger=ledger, kind=AccountKind.DEPOSITORY, label=f"Account {n}")).id
        for n in range(5)
    ]
    assert ids == sorted(ids)


async def test_archiving_an_account_hides_nothing_and_deletes_nothing(db) -> None:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]
    account = await Account.create(ledger=ledger, kind=AccountKind.CREDIT, label="Old Card")

    account.archived = True
    await account.save()
    evict_instance("Account", str(account.id))

    fetched = await Account.get(account.id)
    assert fetched.archived is True
    assert fetched.label == "Old Card"
    assert fetched.kind == AccountKind.CREDIT
    assert await Account.select().count() == 1


async def test_rows_carry_utc_created_and_updated_timestamps(db) -> None:
    user = await provision_user(email="taylor@example.com", display_name="Taylor")

    evict_instance("User", str(user.id))
    fetched = await User.get(user.id)
    assert fetched.created_at.utcoffset() == timedelta(0)
    assert fetched.updated_at.utcoffset() == timedelta(0)

    before_save = fetched.updated_at
    fetched.display_name = "Taylor S."
    await fetched.save()
    assert fetched.updated_at > before_save
    assert fetched.created_at <= fetched.updated_at
