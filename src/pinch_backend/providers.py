"""The sync-provider seam (M7 CP1, issue #33; reshaped M13 CP1, #88;
ADR 0009).

A thin internal interface shaped by exactly what the product consumes —
the same stance as valuation providers, not plugin machinery. The
``SyncProvider`` protocol holds only universal verbs (every aggregator
has them); provider-only plumbing (Plaid's webhook verification and
registration) lives as methods on the concrete client, the
``get_item_status`` precedent. Credentials ride constructors: a provider
instance is materialized per use, bound to a connection's decrypted
credential where one exists, so protocol methods never carry tokens.

Plaid is the first implementation: an owned async httpx client over the
handful of endpoints Pinch speaks (the official SDK is sync-only and
generated-heavy, fighting both the Litestar app and the Procrastinate
worker). MX is the second (M13 CP2, #89) — the same owned-client stance,
bound by guids instead of a token: MX mints no per-connection secret, so
an instance's client_id/api_key plus the enrollment's user guid and the
connection's member guid are the whole credential story (ADR 0009).

Tests substitute a scriptable fake at ``get_provider`` — the one
materialization point and the one monkeypatch seam; CI never touches the
network. The opt-in live-sandbox smoke test proves the real client.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Literal, Protocol, cast

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

if TYPE_CHECKING:
    from collections.abc import Callable

from pinch_backend.models import AccountKind, ConnectionProvider
from pinch_backend.observability import get_logger
from pinch_backend.settings import settings

log = get_logger(__name__)

PLAID_BASE_URLS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

MX_BASE_URLS = {
    # MX calls its sandbox the integration environment (CP0 spike, #87).
    "sandbox": "https://int-api.mx.com",
    "production": "https://api.mx.com",
}

MX_ACCEPT = "application/vnd.mx.api.v1+json"
"""MX's versioned media type — the vnd Accept alone suffices on every
endpoint (CP0 spike: no Accept-Version header needed)."""

BACKFILL_DAYS = 730
"""History requested at link time (PRD #31): depth is fuel for M8's
reports and projections."""

READINESS_ERROR_CODE = "PRODUCT_NOT_READY"
"""Plaid's the-pull-isn't-finished-yet answer. Not a failure since M11
(ADR 0008): the initial sync call arms the doorbell that finishes the
story, so both sync phases treat this code as a quiet wait — never the
retry ladder, never a recorded error."""

INVESTMENTS_TIMEOUT = 180
"""Seconds. A fresh Item's synchronous investments extraction can block
1-2 minutes on Plaid's side (PRD #72) — the 30s default that suits banking
calls would misfire NETWORK_ERROR mid-extraction."""

INVESTMENTS_WINDOW_DAYS = 730
"""Plaid's flat 24-month investments-history cap (PRD #72) — the mirror
window every activities sync re-fetches. Not configurable: the cap is
Plaid's, and the retention floor keeps everything ever captured."""

_PLAID_KIND = {
    "depository": AccountKind.DEPOSITORY,
    "credit": AccountKind.CREDIT,
    "loan": AccountKind.LOAN,
    "investment": AccountKind.INVESTMENT,
    # Plaid's catch-all maps to ours: an account is anything holding value.
    "other": AccountKind.ASSET,
}

_MX_KIND = {
    "CHECKING": AccountKind.DEPOSITORY,
    "SAVINGS": AccountKind.DEPOSITORY,
    "MONEY_MARKET": AccountKind.DEPOSITORY,
    "PREPAID": AccountKind.DEPOSITORY,
    "CASH": AccountKind.DEPOSITORY,
    "CREDIT_CARD": AccountKind.CREDIT,
    "LINE_OF_CREDIT": AccountKind.CREDIT,
    "CHECKING_LINE_OF_CREDIT": AccountKind.CREDIT,
    "LOAN": AccountKind.LOAN,
    "MORTGAGE": AccountKind.LOAN,
    "INVESTMENT": AccountKind.INVESTMENT,
    # Everything else (PROPERTY, INSURANCE, PENSION, UNKNOWN, ANY, …)
    # falls to the same catch-all as Plaid's `other`.
}

_MX_DEBT_KINDS = frozenset({AccountKind.CREDIT, AccountKind.LOAN})
"""The kinds whose MX balances flip sign at the seam: MX reports what's
owed as positive on debt accounts (CP0 spike, empirical) while Pinch
signs balances from the account's perspective — loans and credit carry
negative balances (CONTEXT.md: Accounts). An overpaid card (MX negative)
lands positive, honestly an asset."""


# ISO 4217 minor-unit exponents that differ from the default 2. Plaid
# reports floats in major units; Pinch speaks integer minor units, so the
# conversion must know the exponent — naive *100 would corrupt JPY.
_CURRENCY_EXPONENTS = {
    **dict.fromkeys(
        ("BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG",
         "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"), 0
    ),
    **dict.fromkeys(("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"), 3),
}  # fmt: skip


def _to_minor(amount: float | str, currency: str | None) -> int:
    """Major units → integer minor units, exponent-aware. Accepts the
    string spelling too: MX sends decimal dollars as JSON numbers or
    strings, and ``Decimal(str(...))`` reads both without a float
    round-trip."""
    exponent = _CURRENCY_EXPONENTS.get(currency or "", 2)
    quantum = Decimal(10) ** -exponent
    return int((Decimal(str(amount)) / quantum).to_integral_value(rounding=ROUND_HALF_UP))


ProviderCapability = Literal["transactions", "balances", "holdings", "activity"]
"""One kind of data a sync provider delivers (CONTEXT.md: Provider
capability). What a provider lacks is a stated limit, never an error."""

ALREADY_DISCONNECTED_CODES = frozenset({"ITEM_NOT_FOUND", "MEMBER_NOT_FOUND"})
"""The remove_item codes meaning the provider already doesn't know the
connection (Plaid's Item, MX's member) — success from disconnect's seat,
so the sever proceeds (PRD #31; M13 CP2)."""


def provider_configured(provider: ConnectionProvider) -> bool:
    """Per-provider configuration truth: partial configuration is a valid
    state, not an error state (PRD #86 story 16)."""
    return PROVIDERS[provider].configured()


class ConnectResult(BaseModel):
    """What completing a connect yields (M13 CP1): the connection's
    provider identity, plus the per-connection credential where the
    provider mints one."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    provider_item_id: str
    provider_institution_id: str | None = None
    """The provider's institution identity — the dupe guard's basis.
    Best-effort: an institution-less Item (some sandbox constructs) or a
    failed lookup leaves it None; sync backfills opportunistically."""
    institution_name: str | None = None
    secret: SecretStr | None = None
    """The per-connection credential to encrypt at rest (Plaid: the
    exchanged access token). None for guid-shaped providers (MX) whose
    only credentials are instance-level (ADR 0009). ``SecretStr`` makes
    the no-tokens-in-logs rule structural: a stray repr shows stars."""


class ProviderInstitution(BaseModel):
    """A connection's institution identity as the provider names it —
    id and name captured together (M13 CP1), so the dupe guard and the
    display name can never disagree about which lookup they came from."""

    provider_institution_id: str | None = None
    name: str | None = None


class ProviderAccount(BaseModel):
    """An account as the provider describes it, already in Pinch vocabulary."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    provider_account_id: str
    name: str
    kind: AccountKind
    currency: str | None
    """None when the provider doesn't say; the caller falls back to the
    ledger's primary currency."""
    mask: str | None = None
    """The provider's last-2-4 display digits; a nicety, absent for many
    account types."""
    balance_minor: int | None = None
    """Current balance in integer minor units (exponent-aware conversion
    from the provider's major-unit float); None when unreported."""


class ProviderTransaction(BaseModel):
    """A transaction as the provider describes it, already in Pinch
    vocabulary: ``amount_minor`` is signed from the account's perspective —
    negative is money out (Plaid's positive-is-debit is flipped here, at
    the seam, so nothing downstream ever sees provider sign conventions)."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    provider_transaction_id: str
    provider_account_id: str
    amount_minor: int
    currency: str | None
    date: date
    description: str
    pending: bool
    pending_provider_transaction_id: str | None = None
    """Set on a posted transaction that replaces a pending one — the
    replacement linkage CP3's in-place rewrite keys on."""


class SyncBatch(BaseModel):
    """One drained cursor sync: every page up to has_more=False."""

    added: list[ProviderTransaction]
    modified: list[ProviderTransaction]
    removed: list[str]
    next_cursor: str


class ProviderSecurity(BaseModel):
    """A security's identity as the provider describes it (M10 CP0).
    ``type`` stays provider vocabulary — identity, never law."""

    provider_security_id: str
    name: str
    ticker_symbol: str | None = None
    type: str
    is_cash_equivalent: bool = False


class ProviderHolding(BaseModel):
    """A position as the provider describes it, already in Pinch
    vocabulary: money in integer minor units; ``institution_price`` kept
    as the raw per-share quote (sub-cent precision is real for fund NAVs
    and it is never used in money arithmetic)."""

    provider_account_id: str
    provider_security_id: str
    quantity: float
    institution_price: float | None = None
    institution_price_as_of: date | None = None
    institution_value_minor: int | None = None
    cost_basis_minor: int | None = None
    currency: str | None = None


class InvestmentsBatch(BaseModel):
    """One holdings pull: the current positions and the securities that
    give them identity — current-state by construction (PRD #72)."""

    securities: list[ProviderSecurity]
    holdings: list[ProviderHolding]


class ProviderInvestmentActivity(BaseModel):
    """An investment-account event as the provider describes it, already
    in Pinch vocabulary: ``amount_minor`` signed from the account's
    perspective — negative is money out, so a buy is negative and a
    dividend positive (Plaid's positive-is-cash-out is flipped here, at
    the seam). ``type`` stays the provider's raw string; the sync phase
    promotes it to the domain enum and skips loudly on vocabulary it
    doesn't know."""

    provider_activity_id: str
    provider_account_id: str
    provider_security_id: str | None = None
    date: date
    name: str
    amount_minor: int
    quantity: float = 0.0
    price: float | None = None
    fees_minor: int | None = None
    type: str
    subtype: str | None = None
    currency: str | None = None


class ActivitiesBatch(BaseModel):
    """One drained activities window: every offset page, plus the
    securities the activities name (Plaid sends them alongside)."""

    securities: list[ProviderSecurity]
    activities: list[ProviderInvestmentActivity]


class ItemState(BaseModel):
    """The reconciler's probe answer (M11 CP3): one free /item/get — the
    Item's registered webhook URL ('' when none, the pre-M11 shape) and
    Plaid's own last-successful-update stamps, the timestamps the
    probe-then-decide verdicts read (production-empirical: ``status``
    rides top-level on a parameterless call)."""

    webhook: str
    transactions_updated_at: datetime | None = None
    investments_updated_at: datetime | None = None


class ProviderError(Exception):
    """A provider-side failure, carrying the provider's error code — the
    only provider detail that may ever surface (PRD #31: request payloads
    and tokens never appear in errors or logs)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class SyncProvider(Protocol):
    """The universal verbs — only what every aggregator has (ADR 0009).
    Instances are bound: the connection's credential rides the
    constructor (via ``get_provider``), never the method signatures.
    The connect pair works pre-credential: ``create_connect_session``
    answers the opaque string the provider's widget consumes (Plaid: a
    link_token; MX: a widget URL) — an instance bound to a connection's
    credential mints a repair session instead — and ``complete_connect``
    answers the connection's provider identity, binding the instance to
    whatever credential it minted along the way. Plaid-only verbs
    (webhook verification, registration) live on ``PlaidProvider``:
    fakes owe them nothing."""

    async def create_connect_session(self, *, client_user_id: str) -> str: ...

    async def complete_connect(self, token: str) -> ConnectResult: ...

    async def get_accounts(self) -> list[ProviderAccount]: ...

    async def get_institution(self) -> ProviderInstitution: ...

    async def sync_transactions(self, cursor: str | None) -> SyncBatch: ...

    async def get_holdings(self) -> InvestmentsBatch: ...

    async def get_investment_activities(
        self, start_date: date, end_date: date
    ) -> ActivitiesBatch: ...

    async def remove_item(self) -> None: ...

    async def get_item_state(self) -> ItemState: ...


class PlaidProvider:
    """The owned Plaid client. Every call is one JSON POST with instance
    credentials injected; errors surface as ``ProviderError`` with Plaid's
    ``error_code`` and nothing else.

    Constructed per use (M13 CP1): ``access_token`` binds the instance to
    one connection's decrypted credential, so no method carries a token.
    Unbound instances serve exactly the pre-credential connect pair —
    ``complete_connect`` binds the instance to the token it mints."""

    def __init__(
        self,
        *,
        client_id: str,
        secret: str,
        environment: str,
        access_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._secret = secret
        self._base_url = PLAID_BASE_URLS[environment]
        self._access_token = access_token
        self._transport = transport
        """httpx's documented test seam: wire-shape tests hand in a
        MockTransport; production leaves it None."""

    @property
    def _token(self) -> str:
        """The bound credential, assert-narrowed: reaching a post-connect
        verb unbound is a caller bug, never a request problem."""
        assert self._access_token is not None, "PlaidProvider is not bound to a connection"
        return self._access_token

    async def _post(self, path: str, payload: dict, *, timeout: float = 30) -> dict:
        """Every failure mode funnels into ``ProviderError`` — transport
        faults and unparseable bodies included — so the sync engine's
        error contract (retry transients, record exhaustion) can't be
        bypassed by the network layer."""
        body = {"client_id": self._client_id, "secret": self._secret, **payload}
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=timeout, transport=self._transport
            ) as client:
                response = await client.post(path, json=body)
        except httpx.HTTPError as error:
            raise ProviderError(code="NETWORK_ERROR", message=type(error).__name__) from error
        try:
            data = response.json()
        except ValueError as error:
            raise ProviderError(
                code=f"HTTP_{response.status_code}", message="non-JSON response"
            ) from error
        if response.status_code != 200:
            raise ProviderError(
                code=data.get("error_code", f"HTTP_{response.status_code}"),
                message=data.get("error_message", "Plaid request failed"),
            )
        return data

    async def create_connect_session(self, *, client_user_id: str) -> str:
        """One link-token create; the mode is the binding (M13 CP1). An
        unbound instance is a fresh connect: creation mode requests
        products. A bound instance is reauth repair: update mode carries
        the Item's access token instead — Plaid Link then walks the user
        through re-login and the same token stays valid after."""
        payload: dict = {
            "user": {"client_user_id": client_user_id},
            "client_name": "Pinch",
            "country_codes": settings.plaid_country_codes,
            "language": "en",
        }
        if self._access_token is None:
            payload["products"] = ["transactions"]
            payload["transactions"] = {"days_requested": BACKFILL_DAYS}
            if settings.plaid_webhook_url:
                # New Items are born registered (M11, ADR 0008). Creation
                # mode only: update-mode repair never touches registration —
                # that's the reconciler's job.
                payload["webhook"] = settings.plaid_webhook_url
        else:
            payload["access_token"] = self._access_token
        # Consent everywhere, billed nowhere until an endpoint is called
        # (M10 CP2, PRD #72): never `products` (hides non-investment
        # banks), never the auto-billing arrays. In update mode this is
        # the retrofit path — consent collected on an existing Item,
        # often without re-login.
        payload["additional_consented_products"] = ["investments"]
        if settings.plaid_redirect_uri:
            # OAuth institutions bounce through the registered redirect URI
            # (F2 enabler, #39); non-OAuth flows ignore it entirely.
            payload["redirect_uri"] = settings.plaid_redirect_uri
        data = await self._post("/link/token/create", payload)
        return data["link_token"]

    async def complete_connect(self, token: str) -> ConnectResult:
        """Exchange the public token and capture the Item's provider
        identity in one motion (M13 CP1). The exchange failing raises;
        the institution lookup is a nicety — its failure must never block
        a consented connect, so it degrades to None (sync backfills
        opportunistically later). Binds the instance to the minted access
        token: the caller's next verbs (get_accounts) just work."""
        data = await self._post("/item/public_token/exchange", {"public_token": token})
        self._access_token = data["access_token"]
        try:
            institution = await self.get_institution()
        except ProviderError as error:
            log.info("connection.institution_lookup_failed", code=error.code)
            institution = ProviderInstitution()
        return ConnectResult(
            provider_item_id=data["item_id"],
            provider_institution_id=institution.provider_institution_id,
            institution_name=institution.name,
            secret=data["access_token"],
        )

    async def remove_item(self) -> None:
        """Revoke Plaid's side: stops Item billing and invalidates the
        token. Pinch-side severing is the caller's business."""
        await self._post("/item/remove", {"access_token": self._token})

    async def get_webhook_verification_key(self, key_id: str) -> dict:
        """The receiver's key resolution (M11 CP0): the JWK Plaid signed a
        webhook's JWT with, named by the token header's kid. An instance
        credential call — no access token; Items don't own signing keys.
        PlaidProvider-only since M13 CP1 (ADR 0009): webhook verification
        is Plaid plumbing, so the universal protocol doesn't owe it."""
        data = await self._post("/webhook_verification_key/get", {"key_id": key_id})
        return data["key"]

    async def update_webhook(self, url: str) -> None:
        """Re-register the Item's webhook URL (M11 CP3): the reconciler's
        healer for URL drift and the pre-M11 retrofit — the only
        registration home besides link-token creation. PlaidProvider-only
        since M13 CP1 (ADR 0009): MX registration is dashboard-side and
        unprobeable, so the universal protocol doesn't owe this verb."""
        await self._post("/item/webhook/update", {"access_token": self._token, "webhook": url})

    async def get_item_state(self) -> ItemState:
        """The probe half of probe-then-decide (M11 CP3): free, read-only,
        one call. Sibling of ``get_item_status`` (the developer CLI's
        two-call diagnostic) — this one is load-bearing and protocol-level."""

        def timestamp(product: str) -> datetime | None:
            raw = (status.get(product) or {}).get("last_successful_update")
            return None if raw is None else datetime.fromisoformat(raw)

        data = await self._post("/item/get", {"access_token": self._token})
        status = data.get("status") or {}
        return ItemState(
            webhook=(data.get("item") or {}).get("webhook") or "",
            transactions_updated_at=timestamp("transactions"),
            investments_updated_at=timestamp("investments"),
        )

    async def get_accounts(self) -> list[ProviderAccount]:
        data = await self._post("/accounts/get", {"access_token": self._token})
        accounts = []
        for a in data["accounts"]:
            balances = a.get("balances") or {}
            currency = balances.get("iso_currency_code")
            current = balances.get("current")
            accounts.append(
                ProviderAccount(
                    provider_account_id=a["account_id"],
                    name=a["name"],
                    kind=_PLAID_KIND.get(a["type"], AccountKind.ASSET),
                    currency=currency,
                    mask=a.get("mask"),
                    balance_minor=None if current is None else _to_minor(current, currency),
                )
            )
        return accounts

    async def get_item_status(self) -> dict:
        """Diagnostic probe (post-Stash-QA): the Item's own account of
        itself — products, the transactions pull's real state, its
        standing error. Two reads: /item/get for identity+products
        (``include_status`` is gone from production — UNKNOWN_FIELDS),
        and a one-row /transactions/sync for ``transactions_update_status``
        (NOT_READY vs INITIAL/HISTORICAL_UPDATE_COMPLETE) — the field that
        separates "Plaid never finished" from "our empty-cursor heuristic
        misreads this Item". Read-only: the probe never persists a cursor.
        PlaidProvider-only, deliberately outside the ``SyncProvider``
        protocol: this feeds the ``plaid-item`` developer CLI, never the
        sync engine, so fakes owe it nothing."""
        data = await self._post("/item/get", {"access_token": self._token})
        item = data.get("item") or {}
        sync_probe = await self._post(
            "/transactions/sync", {"access_token": self._token, "count": 1}
        )
        return {
            "item_id": item.get("item_id"),
            "institution_id": item.get("institution_id"),
            "billed_products": item.get("billed_products"),
            "consented_products": item.get("consented_products"),
            "available_products": item.get("available_products"),
            "error": item.get("error"),
            "transactions_update_status": sync_probe.get("transactions_update_status"),
            "transactions_probe": {
                "has_more": sync_probe.get("has_more"),
                "next_cursor_empty": not sync_probe.get("next_cursor"),
                "added_in_first_page": len(sync_probe.get("added") or []),
            },
        }

    async def get_institution(self) -> ProviderInstitution:
        """Two documented steps: the Item names its institution id, the
        institutions endpoint names the institution — id and name captured
        together (M13 CP1) so the dupe guard's identity and the display
        name never disagree. Institution-less Items (some sandbox
        constructs) answer empty without a second call."""
        item = await self._post("/item/get", {"access_token": self._token})
        institution_id = (item.get("item") or {}).get("institution_id")
        if not institution_id:
            return ProviderInstitution()
        data = await self._post(
            "/institutions/get_by_id",
            {"institution_id": institution_id, "country_codes": settings.plaid_country_codes},
        )
        return ProviderInstitution(
            provider_institution_id=institution_id,
            name=(data.get("institution") or {}).get("name"),
        )

    async def get_holdings(self) -> InvestmentsBatch:
        """One holdings pull (M10 CP0). Plaid's floats become minor units
        at the seam, like every money value; the per-share price rides raw
        (a quote, not an Amount). A synchronous call on a fresh Item may
        block minutes-scale — hence the investments timeout."""
        data = await self._post(
            "/investments/holdings/get",
            {"access_token": self._token},
            timeout=INVESTMENTS_TIMEOUT,
        )
        securities = [
            ProviderSecurity(
                provider_security_id=s["security_id"],
                name=s.get("name") or s.get("ticker_symbol") or "Unknown security",
                ticker_symbol=s.get("ticker_symbol"),
                type=s.get("type") or "other",
                is_cash_equivalent=bool(s.get("is_cash_equivalent")),
            )
            for s in data["securities"]
        ]
        holdings = []
        for h in data["holdings"]:
            currency = h.get("iso_currency_code")
            value = h.get("institution_value")
            cost_basis = h.get("cost_basis")
            as_of = h.get("institution_price_as_of")
            holdings.append(
                ProviderHolding(
                    provider_account_id=h["account_id"],
                    provider_security_id=h["security_id"],
                    quantity=h["quantity"],
                    institution_price=h.get("institution_price"),
                    institution_price_as_of=None if as_of is None else date.fromisoformat(as_of),
                    institution_value_minor=None if value is None else _to_minor(value, currency),
                    cost_basis_minor=(
                        None if cost_basis is None else _to_minor(cost_basis, currency)
                    ),
                    currency=currency,
                )
            )
        return InvestmentsBatch(securities=securities, holdings=holdings)

    async def get_investment_activities(self, start_date: date, end_date: date) -> ActivitiesBatch:
        """Drain the window: /investments/transactions/get is offset-
        paginated (there is no cursor endpoint for investments — PRD #72),
        so every page up to total_investment_transactions comes back in
        one call. Securities dedupe across pages by id."""

        def convert(t: dict) -> ProviderInvestmentActivity:
            currency = t.get("iso_currency_code")
            fees = t.get("fees")
            return ProviderInvestmentActivity(
                provider_activity_id=t["investment_transaction_id"],
                provider_account_id=t["account_id"],
                provider_security_id=t.get("security_id"),
                date=date.fromisoformat(t["date"]),
                name=t["name"],
                # Plaid: positive is cash out; Pinch: negative is money out.
                amount_minor=-_to_minor(t["amount"], currency),
                quantity=t.get("quantity") or 0.0,
                price=t.get("price"),
                fees_minor=None if fees is None else _to_minor(fees, currency),
                type=t["type"],
                subtype=t.get("subtype"),
                currency=currency,
            )

        securities: dict[str, ProviderSecurity] = {}
        activities: list[ProviderInvestmentActivity] = []
        offset = 0
        while True:
            data = await self._post(
                "/investments/transactions/get",
                {
                    "access_token": self._token,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "options": {"count": 500, "offset": offset},
                },
                timeout=INVESTMENTS_TIMEOUT,
            )
            for s in data.get("securities", []):
                securities.setdefault(
                    s["security_id"],
                    ProviderSecurity(
                        provider_security_id=s["security_id"],
                        name=s.get("name") or s.get("ticker_symbol") or "Unknown security",
                        ticker_symbol=s.get("ticker_symbol"),
                        type=s.get("type") or "other",
                        is_cash_equivalent=bool(s.get("is_cash_equivalent")),
                    ),
                )
            page = data["investment_transactions"]
            activities.extend(convert(t) for t in page)
            offset += len(page)
            if offset >= data["total_investment_transactions"] or not page:
                return ActivitiesBatch(securities=list(securities.values()), activities=activities)

    async def sync_transactions(self, cursor: str | None) -> SyncBatch:
        """Drain the cursor: every has_more page in one call. The job is
        idempotent — a retry replays from the last *persisted* cursor."""

        def convert(t: dict) -> ProviderTransaction:
            currency = t.get("iso_currency_code")
            return ProviderTransaction(
                provider_transaction_id=t["transaction_id"],
                provider_account_id=t["account_id"],
                # Plaid: positive is money out; Pinch: negative is money out.
                amount_minor=-_to_minor(t["amount"], currency),
                currency=currency,
                date=date.fromisoformat(t["date"]),
                description=t["name"],
                pending=t["pending"],
                pending_provider_transaction_id=t.get("pending_transaction_id"),
            )

        added: list[ProviderTransaction] = []
        modified: list[ProviderTransaction] = []
        removed: list[str] = []
        while True:
            payload: dict = {"access_token": self._token, "count": 500}
            if cursor:  # "" is the zero-transaction Item's persisted cursor: start over
                payload["cursor"] = cursor
            data = await self._post("/transactions/sync", payload)
            if not data["next_cursor"] and not data["has_more"]:
                # Empty batch with an empty cursor is ambiguous, and
                # ``transactions_update_status`` is the disambiguator
                # (post-Stash probe finding, M10): an Item whose accounts
                # have zero transactions answers exactly like one whose
                # initial pull hasn't finished. A completed pull is a real
                # empty ledger — accepted, with the empty cursor persisted
                # (each later sync re-asks from scratch; replay-safe).
                # Anything else stays the M7 live-sandbox rule: a transient
                # error into the retry ladder, the empty cursor never
                # persisted.
                status = data.get("transactions_update_status") or ""
                if status.endswith("UPDATE_COMPLETE"):
                    return SyncBatch(
                        added=added, modified=modified, removed=removed, next_cursor=""
                    )
                raise ProviderError(
                    code=READINESS_ERROR_CODE,
                    message="initial transaction pull not finished",
                )
            added.extend(convert(t) for t in data["added"])
            modified.extend(convert(t) for t in data["modified"])
            removed.extend(r["transaction_id"] for r in data["removed"])
            cursor = data["next_cursor"]
            if not data["has_more"]:
                return SyncBatch(
                    added=added, modified=modified, removed=removed, next_cursor=cursor
                )


class MXProvider:
    """The owned MX client (M13 CP2, issue #89). Same stance as the Plaid
    one — a handful of endpoints, every failure a ``ProviderError``, the
    status code the only provider detail that may surface (MX error
    bodies are never parsed, so nothing in them can leak). Auth is HTTP
    Basic with the instance credentials on every call (CP0 spike:
    verified against int-api.mx.com).

    Bound by guids, not a token (ADR 0009): ``user_guid`` is the ledger's
    enrollment container, ``member_guid`` the connection's provider
    identity. Unbound instances serve nothing; a user-bound instance
    serves the connect pair; ``complete_connect`` binds the member it
    verified, so the caller's next verbs (get_accounts) just work — the
    PlaidProvider precedent."""

    def __init__(
        self,
        *,
        client_id: str,
        api_key: str,
        environment: str,
        user_guid: str | None = None,
        member_guid: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._api_key = api_key
        self._base_url = MX_BASE_URLS[environment]
        self._user_guid = user_guid
        self._member_guid = member_guid
        self._transport = transport
        """httpx's documented test seam: wire-shape tests hand in a
        MockTransport; production leaves it None."""

    @property
    def _user(self) -> str:
        """The enrollment binding, assert-narrowed: reaching a user-scoped
        verb unbound is a caller bug, never a request problem."""
        assert self._user_guid is not None, "MXProvider is not bound to an enrollment"
        return self._user_guid

    @property
    def _member(self) -> str:
        assert self._member_guid is not None, "MXProvider is not bound to a connection"
        return self._member_guid

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> dict:
        """Every failure mode funnels into ``ProviderError``, the
        PlaidProvider stance. Deliberately blunter on HTTP errors: the
        code is ``HTTP_{status}`` and the body is never read — MX error
        payloads carry free-form messages this seam refuses to relay."""
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=30,
                transport=self._transport,
                auth=(self._client_id, self._api_key),
                headers={"Accept": MX_ACCEPT},
            ) as client:
                response = await client.request(method, path, json=json, params=params)
        except httpx.HTTPError as error:
            raise ProviderError(code="NETWORK_ERROR", message=type(error).__name__) from error
        if response.status_code >= 400:
            raise ProviderError(code=f"HTTP_{response.status_code}", message="MX request failed")
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as error:
            raise ProviderError(
                code=f"HTTP_{response.status_code}", message="non-JSON response"
            ) from error

    async def create_user(self, *, ledger_id: str) -> str:
        """Mint the enrollment's provider-side container (POST /users) —
        MX-only plumbing outside the universal protocol, consumed by the
        lazy enrollment ensure (the ``get_item_status`` precedent). The
        ledger id rides MX's integrator ``id`` field so an operator can
        map dashboard containers back to ledgers; MX's PFM profile fields
        stay empty — Pinch sends no user data."""
        data = await self._request("POST", "/users", json={"user": {"id": f"pinch-{ledger_id}"}})
        return data["user"]["guid"]

    async def create_connect_session(self, *, client_user_id: str) -> str:
        """One widget-URL mint under the enrollment user (CP0 spike:
        request/response shapes verified live). ``client_user_id`` is
        unused — the enrollment container already carries identity, so
        Plaid's per-user tagging has no MX analog. A member-bound
        instance mints repair instead (M13 CP2): ``current_member_guid``
        pins the widget to the broken login and the institution search
        disappears — MX's reconnect story, same one-endpoint-two-modes
        shape as Plaid's."""
        widget: dict = {
            "widget_type": "connect_widget",
            "mode": "aggregation",
            "ui_message_version": 4,
        }
        if self._member_guid is not None:
            widget["current_member_guid"] = self._member_guid
            widget["disable_institution_search"] = True
        data = await self._request(
            "POST", f"/users/{self._user}/widget_urls", json={"widget_url": widget}
        )
        return data["widget_url"]["url"]

    async def complete_connect(self, token: str) -> ConnectResult:
        """``token`` is the member guid the widget's memberConnected
        postMessage answered — never trusted naked: the read is scoped to
        OUR enrollment user, so a guid from anyone else's enrollment
        answers MEMBER_NOT_FOUND (the client's fault, 400 at the
        surface). No readiness wait follows: memberConnected fires after
        aggregation completes (CP0 spike), so accounts and balances are
        immediately listable. The institution code rides the member (part
        of the verifying read); the display-name lookup is a nicety that
        degrades to the code alone (the PlaidProvider stance). Binds the
        verified member: the caller's next verbs just work."""
        try:
            data = await self._request("GET", f"/users/{self._user}/members/{token}")
        except ProviderError as error:
            if error.code == "HTTP_404":
                raise ProviderError(
                    code="MEMBER_NOT_FOUND", message="member is not under this enrollment"
                ) from error
            raise
        member = data["member"]
        self._member_guid = member["guid"]
        institution_code = member.get("institution_code")
        institution_name = None
        if institution_code:
            try:
                institution = await self._request("GET", f"/institutions/{institution_code}")
                institution_name = (institution.get("institution") or {}).get("name")
            except ProviderError as error:
                log.info("connection.institution_lookup_failed", code=error.code)
        return ConnectResult(
            provider_item_id=member["guid"],
            provider_institution_id=institution_code,
            institution_name=institution_name,
            secret=None,  # guid-shaped provider: no per-connection secret exists (ADR 0009)
        )

    async def get_institution(self) -> ProviderInstitution:
        """The member names its institution code; /institutions/{code}
        names the institution — id and name captured together, the M13
        CP1 contract. Member-scoped like every MX read."""
        data = await self._request("GET", f"/users/{self._user}/members/{self._member}")
        institution_code = (data.get("member") or {}).get("institution_code")
        if not institution_code:
            return ProviderInstitution()
        institution = await self._request("GET", f"/institutions/{institution_code}")
        return ProviderInstitution(
            provider_institution_id=institution_code,
            name=(institution.get("institution") or {}).get("name"),
        )

    async def get_accounts(self) -> list[ProviderAccount]:
        """Member-scoped listing, never user-scope (CP0 spike amendment:
        member deletion doesn't cascade user-scope lists, so user-scope
        reads see ghosts forever). Drains MX's page/records_per_page
        pagination; balances flip sign per account kind at the seam
        (``_MX_DEBT_KINDS``)."""
        accounts: list[ProviderAccount] = []
        page = 1
        while True:
            data = await self._request(
                "GET",
                f"/users/{self._user}/members/{self._member}/accounts",
                params={"page": page, "records_per_page": 100},
            )
            for a in data["accounts"]:
                kind = _MX_KIND.get(a.get("type") or "", AccountKind.ASSET)
                currency = a.get("currency_code")
                balance = a.get("balance")
                balance_minor = None
                if balance is not None:
                    minor = _to_minor(balance, currency)
                    balance_minor = -minor if kind in _MX_DEBT_KINDS else minor
                number = a.get("account_number")
                accounts.append(
                    ProviderAccount(
                        provider_account_id=a["guid"],
                        name=a["name"],
                        kind=kind,
                        currency=currency,
                        mask=number[-4:] if number else None,
                        balance_minor=balance_minor,
                    )
                )
            pagination = data.get("pagination") or {}
            if page >= int(pagination.get("total_pages") or 1):
                return accounts
            page += 1

    async def remove_item(self) -> None:
        """Delete the MX-side member; Pinch-side severing is the caller's
        business, same contract as Plaid's. A 404 surfaces as
        MEMBER_NOT_FOUND — the provider already not knowing the member is
        the caller's success case (``ALREADY_DISCONNECTED_CODES``).
        Deletion does not cascade MX's user-scope lists (CP0 spike);
        irrelevant here because nothing in this client reads user-scope —
        recorded so nothing ever does."""
        try:
            await self._request("DELETE", f"/users/{self._user}/members/{self._member}")
        except ProviderError as error:
            if error.code == "HTTP_404":
                raise ProviderError(
                    code="MEMBER_NOT_FOUND", message="member already gone"
                ) from error
            raise

    async def sync_transactions(self, cursor: str | None) -> SyncBatch:
        """CP3's seam (#90): the watermark + re-list-window derivation
        (ADR 0009) is not built yet, and the sync engine skips MX
        connections before ever materializing this client."""
        raise NotImplementedError("MX transaction sync lands in M13 CP3 (#90)")

    async def get_item_state(self) -> ItemState:
        """CP3/CP4 territory: the reconciler's MX probe reads member
        status + last-successful-aggregation (#90, #91). The reconciler
        pass is explicitly Plaid-only until then."""
        raise NotImplementedError("the MX reconciler probe lands in M13 CP3/CP4 (#90, #91)")

    async def get_holdings(self) -> InvestmentsBatch:
        """Never: holdings is a billable MX add-on Pinch doesn't ship —
        a stated provider limit, absent from MX's capability atoms, so a
        call landing here is a caller bug (PRD #86 story 12)."""
        raise RuntimeError("MX does not deliver holdings (stated provider limit, PRD #86)")

    async def get_investment_activities(self, start_date: date, end_date: date) -> ActivitiesBatch:
        """Never: MX has no investment-activity endpoint at all — the
        same stated-limit stance as holdings."""
        raise RuntimeError(
            "MX does not deliver investment activity (stated provider limit, PRD #86)"
        )


def _materialize_plaid(*, secret: str | None = None) -> SyncProvider:
    return PlaidProvider(
        client_id=settings.plaid_client_id,
        secret=settings.plaid_secret,
        environment=settings.plaid_environment,
        access_token=secret,
    )


def _materialize_mx(
    *, user_guid: str | None = None, member_guid: str | None = None
) -> SyncProvider:
    return MXProvider(
        client_id=settings.mx_client_id,
        api_key=settings.mx_api_key,
        environment=settings.mx_environment,
        user_guid=user_guid,
        member_guid=member_guid,
    )


@dataclass(frozen=True)
class ProviderRecord:
    """Everything Pinch knows about one sync provider, in one place —
    the four parallel per-provider registries of M13 CP1 (capabilities,
    labels, configured, client cascade), collapsed per that review's
    note now that MX makes a second full entry (CP2, #89)."""

    label: str
    """Display name for user-facing copy (refusals, surfaced errors) —
    the copy must never assume Plaid is the only provider."""
    capabilities: tuple[ProviderCapability, ...]
    """The catalog's truth: what this provider delivers. A missing atom
    is a stated limit, never an error."""
    configured: Callable[[], bool]
    """Read at call time, not import time — settings are monkeypatched
    per test and this record is module-level."""
    materialize: Callable[..., SyncProvider]
    """The client factory. Its keyword signature IS the provider's
    credential shape (ADR 0009): Plaid takes ``secret=``, MX takes
    ``user_guid=``/``member_guid=`` — a foreign binding kwarg raises
    TypeError, a caller bug answered loudly."""


PROVIDERS: dict[ConnectionProvider, ProviderRecord] = {
    ConnectionProvider.PLAID: ProviderRecord(
        label="Plaid",
        capabilities=("transactions", "balances", "holdings", "activity"),
        configured=lambda: settings.plaid_configured,
        materialize=_materialize_plaid,
    ),
    ConnectionProvider.MX: ProviderRecord(
        label="MX",
        capabilities=("transactions", "balances"),
        # holdings is a billable MX add-on and no investment-activity
        # endpoint exists at all (PRD #86, out of scope).
        configured=lambda: settings.mx_configured,
        materialize=_materialize_mx,
    ),
}
"""One record per known provider — known is not configured: the catalog
reports which of these an instance actually offers."""


def get_provider(provider: ConnectionProvider, **binding) -> SyncProvider:
    """The provider registry: still the one place a provider is
    materialized and the one test-monkeypatch seam (fakes accept the
    same per-provider binding kwargs). ``binding`` is the provider's own
    credential shape — see ``ProviderRecord.materialize``. Callers gate
    on ``provider_configured`` before reaching this."""
    return PROVIDERS[provider].materialize(**binding)


def get_plaid_provider(*, secret: str | None = None) -> PlaidProvider:
    """Plaid-only plumbing's materialization (webhook JWT verification,
    registration healing): the same registry entry, typed to Plaid's full
    surface — the verbs the universal protocol doesn't owe (the
    ``get_item_status`` precedent). The cast is the narrowing the registry
    return type can't express; tests monkeypatch ``get_provider`` and
    their fakes flow through here scripting the Plaid-only verbs they
    exercise."""
    return cast("PlaidProvider", get_provider(ConnectionProvider.PLAID, secret=secret))


def get_mx_provider(*, user_guid: str | None = None, member_guid: str | None = None) -> MXProvider:
    """MX-only plumbing's materialization (user-container creation for
    the lazy enrollment ensure): the same registry entry, typed to MX's
    full surface — the ``get_plaid_provider`` precedent, cast and all."""
    return cast(
        "MXProvider",
        get_provider(ConnectionProvider.MX, user_guid=user_guid, member_guid=member_guid),
    )
