"""DELETE /me — full-account erasure (S7a, epic #97 issue #101).

The emptiness proof is registry-driven: every ferro model carrying a
``ledger_id`` or ``user_id`` column is asserted empty for the erased
identity, discovered from the model modules rather than a hand list —
a future table cannot silently escape erasure without failing here.
"""

from datetime import date
from typing import TYPE_CHECKING

import pytest
from cryptography.fernet import Fernet
from ferro import Model

import pinch_backend.auth.models as auth_model_module
import pinch_backend.models as domain_model_module
from pinch_backend import providers
from pinch_backend.auth.models import AuthAttempt
from pinch_backend.crypto import encrypt_secret
from pinch_backend.models import (
    Account,
    AccountKind,
    BalanceEntry,
    Category,
    Connection,
    ConnectionProvider,
    Conversation,
    CorrectionLogEntry,
    Enrollment,
    Holding,
    Import,
    ImportProfile,
    ImportRow,
    InvestmentActivity,
    InvestmentActivityType,
    Ledger,
    LedgerMember,
    Proposal,
    ProposalTag,
    RecurringCadence,
    RecurringSeries,
    Rule,
    Security,
    SplitLine,
    Tag,
    Transaction,
    TransactionTag,
    Transfer,
    User,
    utcnow,
)

if TYPE_CHECKING:
    import uuid

PASSWORD = "correct horse battery staple"


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email: str = "taylor@example.com") -> None:
    response = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "display_name": "Taylor"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text


async def _delete_me(client, password: str = PASSWORD):
    """DELETE with a body rides ``request`` — httpx's ``delete`` takes no
    payload. The password is the fresh-possession proof (PR #113 review)."""
    return await client.request(
        "DELETE",
        "/api/v1/auth/me",
        json={"current_password": password},
        headers=await _csrf(client),
    )


class FakeEraseProvider:
    """Scriptable both-providers fake at the registry seam: one instance
    answers every materialization, recording bindings; per-kind error
    attributes script refusal shapes."""

    def __init__(self) -> None:
        self.materialized: list[dict] = []
        self.removed: list[dict] = []
        self.users_deleted: list[str] = []
        self.remove_error: str | None = None
        self.delete_user_error: str | None = None
        self.on_remove = None
        """Async hook fired inside remove_item — the seam for racing a
        write into the revocation window (the mid-erasure connect)."""
        self._binding: dict = {}

    def materialize(
        self,
        provider,
        *,
        secret: str | None = None,
        user_guid: str | None = None,
        member_guid: str | None = None,
        stored_window_ids=None,
    ) -> "FakeEraseProvider":
        self._binding = {
            "provider": provider,
            "secret": secret,
            "user_guid": user_guid,
            "member_guid": member_guid,
        }
        self.materialized.append(self._binding)
        return self

    async def remove_item(self) -> None:
        if self.on_remove is not None:
            await self.on_remove()
        if self.remove_error is not None:
            raise providers.ProviderError(code=self.remove_error, message="scripted")
        self.removed.append(dict(self._binding))

    async def delete_user(self) -> None:
        if self.delete_user_error is not None:
            raise providers.ProviderError(code=self.delete_user_error, message="scripted")
        assert self._binding["user_guid"] is not None
        self.users_deleted.append(self._binding["user_guid"])


@pytest.fixture
def fake(monkeypatch):
    """Both providers configured, the registry faked."""
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "plaid_client_id", "test-client-id")
    monkeypatch.setattr(settings, "plaid_secret", "test-secret")
    monkeypatch.setattr(settings, "secret_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "mx_client_id", "test-mx-client-id")
    monkeypatch.setattr(settings, "mx_api_key", "test-mx-api-key")
    fake = FakeEraseProvider()
    monkeypatch.setattr(providers, "get_provider", fake.materialize)
    return fake


async def _user_and_ledger(email: str) -> tuple[User, Ledger]:
    user = await User.where(lambda u, e=email: u.email == e).first()
    assert user is not None
    membership = await LedgerMember.where(lambda m, uid=user.id: m.user_id == uid).first()
    assert membership is not None and membership.ledger_id is not None
    ledger = await Ledger.get(membership.ledger_id)
    return user, ledger


async def _seed_domain(ledger: Ledger) -> None:
    """Every ledger-owned table gets at least one row (enforced against
    the registry by the happy-path test — an unseeded table would make
    its emptiness assertion vacuous), including both RESTRICT FKs
    (rule → category, child category → parent) that a naive ledger
    cascade would trip over."""
    await Enrollment.create(ledger=ledger, provider=ConnectionProvider.MX, provider_user_id="USR-1")
    await Connection.create(
        ledger=ledger,
        provider=ConnectionProvider.PLAID,
        provider_item_id="item-1",
        encrypted_secret=encrypt_secret("access-token-1"),
    )
    await Connection.create(ledger=ledger, provider=ConnectionProvider.MX, provider_item_id="MBR-1")
    account = await Account.create(ledger=ledger, kind=AccountKind.DEPOSITORY, label="Checking")
    parent = await Category.create(ledger=ledger, name="Food")
    child = await Category.create(ledger=ledger, name="Coffee", parent=parent)
    await Rule.create(ledger=ledger, condition={"version": 1}, action_category=child)
    txn = await Transaction.create(
        ledger=ledger,
        account=account,
        date=date(2026, 1, 5),
        amount_minor=-500,
        currency="USD",
        description_raw="COFFEE SHOP",
        fingerprint="fp-1",
        description_normalized="coffee shop",
        category=child,
    )
    tag = await Tag.create(ledger=ledger, name="Trip", name_fold="trip")
    await TransactionTag.create(ledger=ledger, transaction=txn, tag=tag)
    await Conversation.create(ledger=ledger, title="hi penny", messages=[{"role": "user"}])
    await BalanceEntry.create(
        ledger=ledger, account=account, amount_minor=100_00, currency="USD", as_of=utcnow()
    )
    security = await Security.create(
        ledger=ledger, provider_security_id="SEC-1", name="ACME", type="equity"
    )
    await Holding.create(
        ledger=ledger, account=account, security=security, quantity=2.0, currency="USD"
    )
    await InvestmentActivity.create(
        ledger=ledger,
        account=account,
        provider_activity_id="ACT-1",
        date=date(2026, 1, 6),
        name="BUY ACME",
        amount_minor=-200_00,
        type=InvestmentActivityType.BUY,
        currency="USD",
    )
    batch = await Import.create(
        ledger=ledger, account=account, filename="jan.csv", file_bytes=b"date,amount\n"
    )
    await ImportRow.create(ledger=ledger, import_batch=batch, row_index=0, raw_cells=["x", "y"])
    await ImportProfile.create(
        ledger=ledger, shape_key="k", header_tuple=["date", "amount"], delimiter=",", mapping={}
    )
    await RecurringSeries.create(
        ledger=ledger,
        account=account,
        payee="coffee shop",
        direction=-1,
        cadence=RecurringCadence.MONTHLY,
    )
    await SplitLine.create(ledger=ledger, transaction=txn, amount_minor=-250)
    await Transfer.create(ledger=ledger, outflow_transaction=txn)
    proposal = await Proposal.create(ledger=ledger, transaction=txn)
    await ProposalTag.create(ledger=ledger, proposal=proposal, name="trip")
    await CorrectionLogEntry.create(ledger=ledger, transaction_id=txn.id)


def _models_with(column: str) -> list[type[Model]]:
    found: dict[str, type[Model]] = {}
    for module in (domain_model_module, auth_model_module):
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, Model)
                and obj is not Model
                and column in getattr(obj, "model_fields", {})
            ):
                found[obj.__name__] = obj
    return list(found.values())


async def _assert_identity_erased(user_id: "uuid.UUID", ledger_id: "uuid.UUID") -> None:
    assert await User.get_or_none(user_id) is None
    assert await Ledger.get_or_none(ledger_id) is None
    ledger_models = _models_with("ledger_id")
    user_models = _models_with("user_id")
    assert len(ledger_models) >= 20, "model registry sweep looks broken"
    for model in ledger_models:
        rows = await model.where(lambda m, lid=ledger_id: m.ledger_id == lid).all()
        assert rows == [], f"{model.__name__} rows survived erasure"
    for model in user_models:
        rows = await model.where(lambda m, uid=user_id: m.user_id == uid).all()
        assert rows == [], f"{model.__name__} rows survived erasure"


# --- the happy path --------------------------------------------------------


async def test_delete_me_erases_everything_and_revokes_upstream(client, fake):
    await _signup(client)
    user, ledger = await _user_and_ledger("taylor@example.com")
    await _seed_domain(ledger)
    # Seed the user-owned auth tables too: a PAT (via the API) and a
    # password-reset token; signup already minted the verification token,
    # membership, and session.
    pat = await client.post(
        "/api/v1/auth/pats",
        json={"name": "ci", "scopes": ["read"]},
        headers=await _csrf(client),
    )
    assert pat.status_code == 201, pat.text
    reset = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "taylor@example.com"},
        headers=await _csrf(client),
    )
    assert reset.status_code == 202, reset.text
    # The vacuity guard (PR #113 review): every model the emptiness sweep
    # will assert over must actually hold a row now, or its assertion
    # proves nothing. A future table failing here is the feature.
    for model in _models_with("ledger_id"):
        rows = await model.where(lambda m, lid=ledger.id: m.ledger_id == lid).all()
        assert rows != [], f"{model.__name__} unseeded — its emptiness assertion would be vacuous"
    for model in _models_with("user_id"):
        rows = await model.where(lambda m, uid=user.id: m.user_id == uid).all()
        assert rows != [], f"{model.__name__} unseeded — its emptiness assertion would be vacuous"
    # A second user is the collateral-damage canary.
    other_client_email = "neighbor@example.com"
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))
    await _signup(client, email=other_client_email)
    survivor, survivor_ledger = await _user_and_ledger(other_client_email)
    await Tag.create(ledger=survivor_ledger, name="Keep", name_fold="keep")
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "taylor@example.com", "password": PASSWORD},
        headers=await _csrf(client),
    )
    assert login.status_code == 200
    response = await _delete_me(client)
    assert response.status_code == 204, response.text

    # Upstream first: the Plaid Item by its decrypted token, the MX
    # member under its enrollment user, then the MX user container.
    plaid = [r for r in fake.removed if r["provider"] is ConnectionProvider.PLAID]
    mx = [r for r in fake.removed if r["provider"] is ConnectionProvider.MX]
    assert [r["secret"] for r in plaid] == ["access-token-1"]
    assert [(r["user_guid"], r["member_guid"]) for r in mx] == [("USR-1", "MBR-1")]
    assert fake.users_deleted == ["USR-1"]

    assert user.id is not None and ledger.id is not None
    await _assert_identity_erased(user.id, ledger.id)
    # The canary and every one of their rows survive.
    assert await User.get_or_none(survivor.id) is not None
    survivor_tags = await Tag.where(lambda t, lid=survivor_ledger.id: t.ledger_id == lid).all()
    assert [t.name for t in survivor_tags] == ["Keep"]

    # The acting session died with the account.
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    # Hard delete frees the email: the same address can sign up afresh.
    await _signup(client, email="taylor@example.com")


async def test_delete_me_purges_email_keyed_rate_limit_rows(client, fake):
    email = "taylor@example.com"
    await _signup(client, email=email)
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))
    bad = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "wrong password entirely"},
        headers=await _csrf(client),
    )
    assert bad.status_code == 401
    key = f"login:email:{email}"
    assert await AuthAttempt.where(lambda a, k=key: a.key == k).all() != []
    good = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD},
        headers=await _csrf(client),
    )
    assert good.status_code == 200
    assert (await _delete_me(client)).status_code == 204
    assert await AuthAttempt.where(lambda a, k=key: a.key == k).all() == []


# --- failure semantics -----------------------------------------------------


async def test_provider_refusal_deletes_nothing_and_retry_converges(client, fake):
    await _signup(client)
    user, ledger = await _user_and_ledger("taylor@example.com")
    await _seed_domain(ledger)

    fake.remove_error = "INSTITUTION_DOWN"
    refused = await _delete_me(client)
    assert refused.status_code == 502
    assert "INSTITUTION_DOWN" in refused.json()["detail"]
    # Nothing local died: the user, the ledger, every connection.
    assert await User.get_or_none(user.id) is not None
    connections = await Connection.where(lambda c, lid=ledger.id: c.ledger_id == lid).all()
    assert len(connections) == 2
    # The refusal did not kill the acting session either — the user can retry.
    fake.remove_error = None
    retried = await _delete_me(client)
    assert retried.status_code == 204, retried.text
    assert user.id is not None and ledger.id is not None
    await _assert_identity_erased(user.id, ledger.id)


async def test_already_gone_upstream_counts_as_revoked(client, fake):
    await _signup(client)
    _, ledger = await _user_and_ledger("taylor@example.com")
    await _seed_domain(ledger)
    # Every removal answers "no such thing" — the erasure's success case.
    fake.remove_error = "ITEM_NOT_FOUND"
    fake.delete_user_error = "USER_NOT_FOUND"
    response = await _delete_me(client)
    assert response.status_code == 204, response.text


async def test_unconfigured_provider_refuses_erasure(client, fake, monkeypatch):
    from pinch_backend.settings import settings

    await _signup(client)
    _, ledger = await _user_and_ledger("taylor@example.com")
    await _seed_domain(ledger)
    # The instance dropped its Plaid credentials after the connect: a
    # silent skip would orphan a live Item, so erasure refuses instead.
    monkeypatch.setattr(settings, "plaid_client_id", "")
    monkeypatch.setattr(settings, "plaid_secret", "")
    response = await _delete_me(client)
    assert response.status_code == 502
    assert "PROVIDER_NOT_CONFIGURED" in response.json()["detail"]
    # The refusal short-circuits before the transaction: nothing died.
    user, _ = await _user_and_ledger("taylor@example.com")
    assert user is not None
    connections = await Connection.where(lambda c, lid=ledger.id: c.ledger_id == lid).all()
    assert len(connections) == 2


async def test_undecryptable_secret_is_a_named_refusal(client, fake):
    """A rotated/lost PINCH_SECRET_ENCRYPTION_KEY must refuse with a
    code, never crash into a 500 that bypasses the failure design."""
    await _signup(client)
    _, ledger = await _user_and_ledger("taylor@example.com")
    await Connection.create(
        ledger=ledger,
        provider=ConnectionProvider.PLAID,
        provider_item_id="item-bad",
        encrypted_secret=b"not-a-fernet-blob",
    )
    response = await _delete_me(client)
    assert response.status_code == 502
    assert "SECRET_UNDECRYPTABLE" in response.json()["detail"]
    assert await User.where(lambda u: u.email == "taylor@example.com").all() != []


async def test_mx_connection_without_enrollment_is_a_named_refusal(client, fake):
    """Corrupted state refuses observably instead of a mid-sweep 500 —
    and the other provider's revocation is still attempted first."""
    await _signup(client)
    _, ledger = await _user_and_ledger("taylor@example.com")
    await Connection.create(
        ledger=ledger,
        provider=ConnectionProvider.PLAID,
        provider_item_id="item-1",
        encrypted_secret=encrypt_secret("access-token-1"),
    )
    await Connection.create(
        ledger=ledger, provider=ConnectionProvider.MX, provider_item_id="MBR-orphan"
    )
    response = await _delete_me(client)
    assert response.status_code == 502
    assert "ENROLLMENT_MISSING" in response.json()["detail"]
    # One refusal did not hide the other connection's attempt.
    assert [r["secret"] for r in fake.removed] == ["access-token-1"]
    assert await Ledger.get_or_none(ledger.id) is not None


async def test_connection_minted_mid_erasure_refuses_then_retry_converges(client, fake):
    """The revocation window is real: a connection created while the
    sweep runs (a second tab finishing a Link exchange) must refuse —
    cascading it away unrevoked would orphan a live provider token."""
    await _signup(client)
    _, ledger = await _user_and_ledger("taylor@example.com")
    await _seed_domain(ledger)

    async def _mint_mid_sweep() -> None:
        fake.on_remove = None
        await Connection.create(
            ledger=ledger,
            provider=ConnectionProvider.PLAID,
            provider_item_id="item-raced",
            encrypted_secret=encrypt_secret("access-raced"),
        )

    fake.on_remove = _mint_mid_sweep
    refused = await _delete_me(client)
    assert refused.status_code == 502
    assert "CREATED_DURING_ERASURE" in refused.json()["detail"]
    assert await Ledger.get_or_none(ledger.id) is not None
    # The retry's sweep sees the newcomer and revokes it too.
    retried = await _delete_me(client)
    assert retried.status_code == 204, retried.text
    assert "access-raced" in [r["secret"] for r in fake.removed]


# --- the credential fence --------------------------------------------------


async def test_wrong_password_refuses_erasure(client, fake):
    """A live session alone is not enough (PR #113 review): the most
    destructive verb demands fresh possession, like change_password."""
    await _signup(client)
    response = await _delete_me(client, password="not the password")
    assert response.status_code == 401
    assert await User.where(lambda u: u.email == "taylor@example.com").all() != []
    # The right password still works afterwards.
    assert (await _delete_me(client)).status_code == 204


async def test_pat_cannot_delete_the_account(client, fake):
    await _signup(client)
    created = await client.post(
        "/api/v1/auth/pats",
        json={"name": "ci", "scopes": ["write"]},
        headers=await _csrf(client),
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    response = await client.request(
        "DELETE",
        "/api/v1/auth/me",
        json={"current_password": PASSWORD},
        headers={"authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401
    assert await User.where(lambda u: u.email == "taylor@example.com").all() != []


async def test_unverified_user_can_still_delete_themselves(client, fake, monkeypatch):
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "verification_required", True)
    await _signup(client)
    # The hosted gate holds the domain surface closed...
    assert (await client.get("/api/v1/accounts")).status_code == 403
    # ...but never the exit.
    response = await _delete_me(client)
    assert response.status_code == 204, response.text


# --- aftermath -------------------------------------------------------------


async def test_queued_classification_job_tolerates_an_erased_ledger(
    client, fake, job_connector, run_jobs
):
    from pinch_backend.jobs import classify_ledger

    await _signup(client)
    _, ledger = await _user_and_ledger("taylor@example.com")
    assert (await _delete_me(client)).status_code == 204
    await classify_ledger.defer_async(ledger_id=str(ledger.id))
    # A sweep deferred before (or during) erasure must no-op quietly,
    # not traceback through five retries over a deliberately gone row.
    await run_jobs()
