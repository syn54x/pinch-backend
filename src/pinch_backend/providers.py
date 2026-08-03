"""The connection-provider seam (M7 CP1, issue #33; PRD #31).

A thin internal interface shaped by exactly what the milestone consumes —
the same stance as valuation providers, not plugin machinery. Plaid is the
first implementation: an owned async httpx client over the handful of
endpoints Pinch speaks (the official SDK is sync-only and generated-heavy,
fighting both the Litestar app and the Procrastinate worker).

Tests substitute a scriptable fake at ``get_provider`` — CI never touches
the network; the opt-in live-sandbox smoke test proves the real client.
"""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict

from pinch_backend.models import AccountKind
from pinch_backend.settings import settings

PLAID_BASE_URLS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

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


def _to_minor(amount: float, currency: str | None) -> int:
    exponent = _CURRENCY_EXPONENTS.get(currency or "", 2)
    quantum = Decimal(10) ** -exponent
    return int((Decimal(str(amount)) / quantum).to_integral_value(rounding=ROUND_HALF_UP))


class ExchangedToken(BaseModel):
    access_token: str
    item_id: str


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
    async def create_link_token(
        self, *, client_user_id: str, access_token: str | None = None
    ) -> str: ...

    async def exchange_public_token(self, public_token: str) -> ExchangedToken: ...

    async def get_accounts(self, access_token: str) -> list[ProviderAccount]: ...

    async def get_institution_name(self, access_token: str) -> str | None: ...

    async def sync_transactions(self, access_token: str, cursor: str | None) -> SyncBatch: ...

    async def get_holdings(self, access_token: str) -> InvestmentsBatch: ...

    async def get_investment_activities(
        self, access_token: str, start_date: date, end_date: date
    ) -> ActivitiesBatch: ...

    async def remove_item(self, access_token: str) -> None: ...

    async def get_webhook_verification_key(self, key_id: str) -> dict: ...

    async def update_webhook(self, access_token: str, url: str) -> None: ...

    async def get_item_state(self, access_token: str) -> ItemState: ...


class PlaidProvider:
    """The owned Plaid client. Every call is one JSON POST with instance
    credentials injected; errors surface as ``ProviderError`` with Plaid's
    ``error_code`` and nothing else."""

    def __init__(
        self,
        *,
        client_id: str,
        secret: str,
        environment: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._secret = secret
        self._base_url = PLAID_BASE_URLS[environment]
        self._transport = transport
        """httpx's documented test seam: wire-shape tests hand in a
        MockTransport; production leaves it None."""

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

    async def create_link_token(
        self, *, client_user_id: str, access_token: str | None = None
    ) -> str:
        """Creation mode requests products; update mode (reauth repair)
        carries the Item's access token instead — Plaid Link then walks the
        user through re-login and the same token stays valid after."""
        payload: dict = {
            "user": {"client_user_id": client_user_id},
            "client_name": "Pinch",
            "country_codes": settings.plaid_country_codes,
            "language": "en",
        }
        if access_token is None:
            payload["products"] = ["transactions"]
            payload["transactions"] = {"days_requested": BACKFILL_DAYS}
            if settings.plaid_webhook_url:
                # New Items are born registered (M11, ADR 0008). Creation
                # mode only: update-mode repair never touches registration —
                # that's the reconciler's job.
                payload["webhook"] = settings.plaid_webhook_url
        else:
            payload["access_token"] = access_token
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

    async def exchange_public_token(self, public_token: str) -> ExchangedToken:
        data = await self._post("/item/public_token/exchange", {"public_token": public_token})
        return ExchangedToken(access_token=data["access_token"], item_id=data["item_id"])

    async def remove_item(self, access_token: str) -> None:
        """Revoke Plaid's side: stops Item billing and invalidates the
        token. Pinch-side severing is the caller's business."""
        await self._post("/item/remove", {"access_token": access_token})

    async def get_webhook_verification_key(self, key_id: str) -> dict:
        """The receiver's key resolution (M11 CP0): the JWK Plaid signed a
        webhook's JWT with, named by the token header's kid. An instance
        credential call — no access token; Items don't own signing keys."""
        data = await self._post("/webhook_verification_key/get", {"key_id": key_id})
        return data["key"]

    async def update_webhook(self, access_token: str, url: str) -> None:
        """Re-register the Item's webhook URL (M11 CP3): the reconciler's
        healer for URL drift and the pre-M11 retrofit — the only
        registration home besides link-token creation."""
        await self._post("/item/webhook/update", {"access_token": access_token, "webhook": url})

    async def get_item_state(self, access_token: str) -> ItemState:
        """The probe half of probe-then-decide (M11 CP3): free, read-only,
        one call. Sibling of ``get_item_status`` (the developer CLI's
        two-call diagnostic) — this one is load-bearing and protocol-level."""

        def timestamp(product: str) -> datetime | None:
            raw = (status.get(product) or {}).get("last_successful_update")
            return None if raw is None else datetime.fromisoformat(raw)

        data = await self._post("/item/get", {"access_token": access_token})
        status = data.get("status") or {}
        return ItemState(
            webhook=(data.get("item") or {}).get("webhook") or "",
            transactions_updated_at=timestamp("transactions"),
            investments_updated_at=timestamp("investments"),
        )

    async def get_accounts(self, access_token: str) -> list[ProviderAccount]:
        data = await self._post("/accounts/get", {"access_token": access_token})
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

    async def get_item_status(self, access_token: str) -> dict:
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
        data = await self._post("/item/get", {"access_token": access_token})
        item = data.get("item") or {}
        sync_probe = await self._post(
            "/transactions/sync", {"access_token": access_token, "count": 1}
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

    async def get_institution_name(self, access_token: str) -> str | None:
        """Two documented steps: the Item names its institution id, the
        institutions endpoint names the institution. Institution-less Items
        (some sandbox constructs) answer None without a second call."""
        item = await self._post("/item/get", {"access_token": access_token})
        institution_id = (item.get("item") or {}).get("institution_id")
        if not institution_id:
            return None
        data = await self._post(
            "/institutions/get_by_id",
            {"institution_id": institution_id, "country_codes": settings.plaid_country_codes},
        )
        return (data.get("institution") or {}).get("name")

    async def get_holdings(self, access_token: str) -> InvestmentsBatch:
        """One holdings pull (M10 CP0). Plaid's floats become minor units
        at the seam, like every money value; the per-share price rides raw
        (a quote, not an Amount). A synchronous call on a fresh Item may
        block minutes-scale — hence the investments timeout."""
        data = await self._post(
            "/investments/holdings/get",
            {"access_token": access_token},
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

    async def get_investment_activities(
        self, access_token: str, start_date: date, end_date: date
    ) -> ActivitiesBatch:
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
                    "access_token": access_token,
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

    async def sync_transactions(self, access_token: str, cursor: str | None) -> SyncBatch:
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
            payload: dict = {"access_token": access_token, "count": 500}
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


def get_provider() -> SyncProvider:
    """The one place a provider is materialized; tests monkeypatch here.
    Callers gate on ``settings.plaid_configured`` before reaching this."""
    return PlaidProvider(
        client_id=settings.plaid_client_id,
        secret=settings.plaid_secret,
        environment=settings.plaid_environment,
    )
