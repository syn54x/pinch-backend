"""The connection-provider seam (M7 CP1, issue #33; PRD #31).

A thin internal interface shaped by exactly what the milestone consumes —
the same stance as valuation providers, not plugin machinery. Plaid is the
first implementation: an owned async httpx client over the handful of
endpoints Pinch speaks (the official SDK is sync-only and generated-heavy,
fighting both the Litestar app and the Procrastinate worker).

Tests substitute a scriptable fake at ``get_provider`` — CI never touches
the network; the opt-in live-sandbox smoke test proves the real client.
"""

from typing import Protocol

import httpx
from pydantic import BaseModel

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


class ExchangedToken(BaseModel):
    access_token: str
    item_id: str


class ProviderAccount(BaseModel):
    """An account as the provider describes it, already in Pinch vocabulary."""

    provider_account_id: str
    name: str
    kind: AccountKind
    currency: str | None
    """None when the provider doesn't say; the caller falls back to the
    acting user's primary currency."""


class ProviderError(Exception):
    """A provider-side failure, carrying the provider's error code — the
    only provider detail that may ever surface (PRD #31: request payloads
    and tokens never appear in errors or logs)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class SyncProvider(Protocol):
    async def create_link_token(self, *, client_user_id: str) -> str: ...

    async def exchange_public_token(self, public_token: str) -> ExchangedToken: ...

    async def get_accounts(self, access_token: str) -> list[ProviderAccount]: ...


class PlaidProvider:
    """The owned Plaid client. Every call is one JSON POST with instance
    credentials injected; errors surface as ``ProviderError`` with Plaid's
    ``error_code`` and nothing else."""

    def __init__(self, *, client_id: str, secret: str, environment: str) -> None:
        self._client_id = client_id
        self._secret = secret
        self._base_url = PLAID_BASE_URLS[environment]

    async def _post(self, path: str, payload: dict) -> dict:
        body = {"client_id": self._client_id, "secret": self._secret, **payload}
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30) as client:
            response = await client.post(path, json=body)
        data = response.json()
        if response.status_code != 200:
            raise ProviderError(
                code=data.get("error_code", f"HTTP_{response.status_code}"),
                message=data.get("error_message", "Plaid request failed"),
            )
        return data

    async def create_link_token(self, *, client_user_id: str) -> str:
        data = await self._post(
            "/link/token/create",
            {
                "user": {"client_user_id": client_user_id},
                "client_name": "Pinch",
                "products": ["transactions"],
                "transactions": {"days_requested": BACKFILL_DAYS},
                "country_codes": settings.plaid_country_codes,
                "language": "en",
            },
        )
        return data["link_token"]

    async def exchange_public_token(self, public_token: str) -> ExchangedToken:
        data = await self._post("/item/public_token/exchange", {"public_token": public_token})
        return ExchangedToken(access_token=data["access_token"], item_id=data["item_id"])

    async def get_accounts(self, access_token: str) -> list[ProviderAccount]:
        data = await self._post("/accounts/get", {"access_token": access_token})
        return [
            ProviderAccount(
                provider_account_id=a["account_id"],
                name=a["name"],
                kind=_PLAID_KIND.get(a["type"], AccountKind.ASSET),
                currency=(a.get("balances") or {}).get("iso_currency_code"),
            )
            for a in data["accounts"]
        ]


def get_provider() -> SyncProvider:
    """The one place a provider is materialized; tests monkeypatch here.
    Callers gate on ``settings.plaid_configured`` before reaching this."""
    return PlaidProvider(
        client_id=settings.plaid_client_id,
        secret=settings.plaid_secret,
        environment=settings.plaid_environment,
    )
