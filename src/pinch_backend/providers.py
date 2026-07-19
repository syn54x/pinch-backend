"""The connection-provider seam (M7 CP1, issue #33; PRD #31).

A thin internal interface shaped by exactly what the milestone consumes —
the same stance as valuation providers, not plugin machinery. Plaid is the
first implementation: an owned async httpx client over the handful of
endpoints Pinch speaks (the official SDK is sync-only and generated-heavy,
fighting both the Litestar app and the Procrastinate worker).

Tests substitute a scriptable fake at ``get_provider`` — CI never touches
the network; the opt-in live-sandbox smoke test proves the real client.
"""

from datetime import date
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

    async def sync_transactions(self, access_token: str, cursor: str | None) -> SyncBatch: ...

    async def remove_item(self, access_token: str) -> None: ...


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

    async def _post(self, path: str, payload: dict) -> dict:
        """Every failure mode funnels into ``ProviderError`` — transport
        faults and unparseable bodies included — so the sync engine's
        error contract (retry transients, record exhaustion) can't be
        bypassed by the network layer."""
        body = {"client_id": self._client_id, "secret": self._secret, **payload}
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=30, transport=self._transport
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
        else:
            payload["access_token"] = access_token
        data = await self._post("/link/token/create", payload)
        return data["link_token"]

    async def exchange_public_token(self, public_token: str) -> ExchangedToken:
        data = await self._post("/item/public_token/exchange", {"public_token": public_token})
        return ExchangedToken(access_token=data["access_token"], item_id=data["item_id"])

    async def remove_item(self, access_token: str) -> None:
        """Revoke Plaid's side: stops Item billing and invalidates the
        token. Pinch-side severing is the caller's business."""
        await self._post("/item/remove", {"access_token": access_token})

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
                    balance_minor=None if current is None else _to_minor(current, currency),
                )
            )
        return accounts

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
            if cursor is not None:
                payload["cursor"] = cursor
            data = await self._post("/transactions/sync", payload)
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
