"""/webhooks/plaid — the doorbell receiver (M11 CP0, issue #78; ADR 0008).

Provider plumbing, never Developer API: the route is sessionless,
CSRF-exempt, and excluded from the OpenAPI schema, so the frontend
contract seam does not move. Verification is Plaid's JWT scheme in full —
ES256 only, key resolved by kid through the provider seam, iat freshness,
constant-time body-hash compare — with no bypass in any configuration.

Past the front door the doorbell-only law (CONTEXT.md: Webhook) governs:
read which connection rang and which kind of change, enqueue the same
lock-serialized job a clicked Refresh would, answer 200. Payload contents
are never read — Plaid documents duplicate and out-of-order delivery, and
sync is replay-safe by construction. Unknown codes and unmatched item ids
log-and-200: an error answer only makes Plaid retry something we chose to
ignore.
"""

import base64
import binascii
import hashlib
import hmac
import json
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from litestar import Request, Router, post
from litestar.exceptions import NotAuthorizedException
from litestar.status_codes import HTTP_200_OK

from pinch_backend import providers
from pinch_backend.jobs import sync_connection, sync_investments
from pinch_backend.models import Connection, ConnectionStatus
from pinch_backend.observability import get_logger
from pinch_backend.settings import settings
from pinch_backend.sync import AUTH_ERROR_CODES, record_broken

log = get_logger(__name__)

VERIFICATION_HEADER = "plaid-verification"
IAT_MAX_AGE_SECONDS = 300
"""Plaid's documented freshness window: a verification JWT older than
~5 minutes is a replay, not a doorbell."""

_jwk_cache: dict[str, dict] = {}
"""Per-kid key cache (PRD #77 decision 4): Plaid rotates by issuing new
kids, so a kid's key is immutable and one fetch serves the process. The
JWK's ``expired_at`` is deliberately ignored — expiry matters when scanning
a key *list* for refresh; a token naming an expired kid simply fails
signature verification."""

_DOORBELLS = {
    # (webhook_type, webhook_code) → the job a clicked Refresh would run.
    # HOLDINGS fires DEFAULT_UPDATE only (fact sheet); anything absent
    # here and not an ITEM lifecycle code logs-and-200s.
    ("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE"): sync_connection,
    ("HOLDINGS", "DEFAULT_UPDATE"): sync_investments,
    ("INVESTMENTS_TRANSACTIONS", "DEFAULT_UPDATE"): sync_investments,
    ("INVESTMENTS_TRANSACTIONS", "HISTORICAL_UPDATE"): sync_investments,
}

_ITEM_ACTING = {"ERROR", "LOGIN_REPAIRED", "USER_PERMISSION_REVOKED"}
"""ITEM codes that touch connection health (M11 CP2, issue #80) — only
ever through the transitions the sync engine already writes (the
no-new-states law: webhooks change when we learn, never what states
exist)."""

_ITEM_LOG_ONLY = {"PENDING_DISCONNECT", "PENDING_EXPIRATION", "WEBHOOK_UPDATE_ACKNOWLEDGED"}
"""Advance warnings and acknowledgements: logged and nothing more — the
eventual ERROR is the acting event, and the pending pair has no
user-facing surface yet (PRD #77, out of scope)."""


class VerificationFailure(Exception):
    """One rejection type for every way a token can be wrong; the reason
    is for the log line — the response is an undifferentiated 401, which
    never confirms to a prober which check they failed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _b64url_decode(segment: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (ValueError, binascii.Error) as error:
        raise VerificationFailure("malformed_token") from error


async def _resolve_key(kid: str) -> ec.EllipticCurvePublicKey:
    """kid → P-256 public key, through the provider seam (fetched once per
    kid). Any irregularity — fetch failure, non-EC key, off-curve point —
    is a verification failure, never a 500: a forged kid must cost the
    forger a 401, not us an alert."""
    jwk = _jwk_cache.get(kid)
    if jwk is None:
        try:
            jwk = await providers.get_provider().get_webhook_verification_key(kid)
        except providers.ProviderError as error:
            raise VerificationFailure("key_fetch_failed") from error
        _jwk_cache[kid] = jwk
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise VerificationFailure("unexpected_key_type")
    try:
        x = int.from_bytes(_b64url_decode(jwk["x"]), "big")
        y = int.from_bytes(_b64url_decode(jwk["y"]), "big")
        return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    except (KeyError, ValueError) as error:
        raise VerificationFailure("invalid_key") from error


async def _verify(token: str, body: bytes) -> None:
    """Plaid's webhook-verification scheme in full (fact sheet): require
    ES256, verify the signature with the kid-named key, then the claims —
    iat within the freshness window, request_body_sha256 matching the raw
    body under a constant-time compare."""
    try:
        header_b64, claims_b64, signature_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(claims_b64))
    except ValueError as error:
        raise VerificationFailure("malformed_token") from error
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise VerificationFailure("malformed_token")
    if header.get("alg") != "ES256":
        raise VerificationFailure("algorithm_rejected")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise VerificationFailure("missing_kid")
    public_key = await _resolve_key(kid)
    signature = _b64url_decode(signature_b64)
    if len(signature) != 64:  # JWS ES256: raw r||s, 32 bytes each
        raise VerificationFailure("bad_signature")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    try:
        public_key.verify(
            encode_dss_signature(r, s),
            f"{header_b64}.{claims_b64}".encode(),
            ec.ECDSA(hashes.SHA256()),
        )
    except InvalidSignature as error:
        raise VerificationFailure("bad_signature") from error
    iat = claims.get("iat")
    if not isinstance(iat, int | float) or isinstance(iat, bool):
        raise VerificationFailure("missing_iat")
    if abs(time.time() - iat) > IAT_MAX_AGE_SECONDS:
        raise VerificationFailure("stale_iat")
    claimed_hash = claims.get("request_body_sha256")
    if not isinstance(claimed_hash, str):
        raise VerificationFailure("missing_body_hash")
    if not hmac.compare_digest(claimed_hash, hashlib.sha256(body).hexdigest()):
        raise VerificationFailure("body_hash_mismatch")


@post("/plaid", include_in_schema=False, exclude_from_csrf=True, status_code=HTTP_200_OK)
async def receive_plaid_webhook(request: Request) -> None:
    """Verify, dispatch, 200 — within Plaid's 10-second answer window.
    Rejections are an undifferentiated 401; everything verified answers
    200, enqueued or not (a non-200 only schedules a retry of something
    we already chose how to handle)."""
    body = await request.body()
    token = request.headers.get(VERIFICATION_HEADER)
    try:
        if not settings.plaid_configured:
            raise VerificationFailure("plaid_not_configured")
        if not token:
            raise VerificationFailure("missing_verification_header")
        await _verify(token, body)
    except VerificationFailure as failure:
        log.warning("webhook.rejected", reason=failure.reason)
        raise NotAuthorizedException(detail="Webhook verification failed") from failure

    # Doorbell-only from here: which connection rang, which kind of change.
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        # Verified as genuinely Plaid's (the body hash matched), but not a
        # shape we know. Log-and-200 — a retry would not go better.
        log.warning("webhook.unparseable_body")
        return
    webhook_type = payload.get("webhook_type")
    webhook_code = payload.get("webhook_code")
    item_id = payload.get("item_id")
    if webhook_type == "ITEM" and webhook_code in _ITEM_LOG_ONLY:
        log.info("webhook.item_acknowledged", webhook_code=webhook_code)
        return
    job = _DOORBELLS.get((webhook_type, webhook_code))
    acts_on_item = webhook_type == "ITEM" and webhook_code in _ITEM_ACTING
    if job is None and not acts_on_item:
        log.info("webhook.ignored", webhook_type=webhook_type, webhook_code=webhook_code)
        return
    connection = await Connection.where(lambda c, iid=item_id: c.provider_item_id == iid).first()
    if connection is None:
        # Membership churn or a stale Item: acknowledged, never confirmed —
        # the 200 tells a prober nothing about which item ids exist.
        log.info("webhook.unmatched_item", webhook_type=webhook_type)
        return
    if job is None:
        await _handle_item_event(webhook_code, payload, connection)
        return
    await job.configure(lock=f"sync:{connection.id}").defer_async(connection_id=str(connection.id))
    log.info(
        "webhook.dispatched",
        webhook_type=webhook_type,
        webhook_code=webhook_code,
        connection_id=str(connection.id),
        job=job.name,
    )


async def _handle_item_event(webhook_code: str, payload: dict, connection: Connection) -> None:
    """Connection health becomes prompt (M11 CP2, issue #80): the same
    transitions the sync engine writes, fired the moment Plaid learns
    instead of at the next manual sync. The one payload read beyond the
    doorbell — ITEM ERROR's ``error.error_code`` — IS the kind of change
    for this family, and it lands only where sync failures already land."""
    if webhook_code == "ERROR":
        error_code = (payload.get("error") or {}).get("error_code") or "ITEM_ERROR"
        status = (
            ConnectionStatus.REAUTH_REQUIRED
            if error_code in AUTH_ERROR_CODES
            else ConnectionStatus.ERROR
        )
        await record_broken(connection, status, error_code)
    elif webhook_code == "USER_PERMISSION_REVOKED":
        await record_broken(connection, ConnectionStatus.ERROR, "USER_PERMISSION_REVOKED")
    elif webhook_code == "LOGIN_REPAIRED":
        # The heal half of the sync engine's own transition: status and
        # error_detail clear; last_synced_at stays the sync's stamp — the
        # proving sync enqueued here writes it on success.
        connection.status = ConnectionStatus.ACTIVE
        connection.error_detail = None
        await connection.save()
        await sync_connection.configure(lock=f"sync:{connection.id}").defer_async(
            connection_id=str(connection.id)
        )
    else:  # pragma: no cover — _ITEM_ACTING and this chain move together
        raise RuntimeError(f"ITEM code {webhook_code} is listed as acting but unhandled")
    log.info(
        "webhook.item_lifecycle",
        webhook_code=webhook_code,
        connection_id=str(connection.id),
        status=connection.status.value,
    )


webhooks_router = Router(path="/webhooks", route_handlers=[receive_plaid_webhook])
