"""Mailed single-use token flows: email verification and password reset.

Each flow is start (mint token, mail link) → confirm (consume token, apply
the effect). Confirms return False for every invalid-token cause — unknown,
expired, already used — so the HTTP layer can answer them identically.
"""

from ferro import transaction

from pinch_backend.auth.models import EmailVerificationToken, PasswordResetToken, Session
from pinch_backend.auth.passwords import hash_password
from pinch_backend.auth.tokens import generate_token, hash_token
from pinch_backend.mailer import get_mailer
from pinch_backend.models import User, utcnow
from pinch_backend.observability import get_logger
from pinch_backend.settings import settings

log = get_logger(__name__)


async def start_email_verification(user: User) -> None:
    token = generate_token()
    await EmailVerificationToken.create(
        user=user,
        token_hash=token.token_hash,
        expires_at=utcnow() + settings.verification_token_ttl,
    )
    await get_mailer().send(
        to=user.email,
        subject="Verify your Pinch email",
        body=f"Confirm your address:\n"
        f"{settings.frontend_base_url}/verify-email?token={token.secret}",
    )
    log.info("auth.verification.requested", user_id=str(user.id))


async def confirm_email_verification(secret: str) -> bool:
    row = await EmailVerificationToken.where(lambda t: t.token_hash == hash_token(secret)).first()
    if row is None or row.consumed_at is not None or row.expires_at <= utcnow():
        return False
    async with transaction():
        row.consumed_at = utcnow()
        await row.save()
        user = await User.get(row.user_id)  # ty: ignore[unresolved-attribute]
        if user.email_verified_at is None:
            user.email_verified_at = utcnow()
            await user.save()
    log.info("auth.verification.confirmed", user_id=str(user.id))
    return True


async def start_password_reset(email: str) -> None:
    """Silently a no-op for an unknown email — the caller answers 202 either
    way, so the response can't be used to probe for accounts."""
    user = await User.where(lambda u: u.email == email).first()
    if user is None:
        log.info("auth.reset.requested_unknown_email", email=email)
        return
    token = generate_token()
    await PasswordResetToken.create(
        user=user,
        token_hash=token.token_hash,
        expires_at=utcnow() + settings.reset_token_ttl,
    )
    await get_mailer().send(
        to=user.email,
        subject="Reset your Pinch password",
        body=f"Choose a new password:\n"
        f"{settings.frontend_base_url}/reset-password?token={token.secret}",
    )
    log.info("auth.reset.requested", user_id=str(user.id))


async def complete_password_reset(secret: str, new_password: str) -> bool:
    """Set the new password, consume every outstanding reset token (an
    attacker's older unused link dies too), and revoke all sessions —
    recovery from compromise is one action (PRD story 9)."""
    row = await PasswordResetToken.where(lambda t: t.token_hash == hash_token(secret)).first()
    if row is None or row.consumed_at is not None or row.expires_at <= utcnow():
        return False
    now = utcnow()
    user_id = row.user_id  # ty: ignore[unresolved-attribute]
    async with transaction():
        outstanding = await PasswordResetToken.where(lambda t: t.user_id == user_id).all()
        for token_row in outstanding:
            if token_row.consumed_at is None:
                token_row.consumed_at = now
                await token_row.save()
        user = await User.get(user_id)
        user.password_hash = hash_password(new_password)
        await user.save()
        revoked = await Session.where(lambda s: s.user_id == user_id).delete()
    log.info("auth.reset.completed", user_id=str(user_id), sessions_revoked=revoked)
    return True
