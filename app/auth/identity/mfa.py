"""Second factors: enrollment, verification, recovery codes, lockout.

The flow, end to end:

1. **Enroll.** ``begin_enrollment`` generates a fresh base32 seed, seals it with
   the tenant's identity key, and stores it as a ``pending`` factor. The seed is
   returned to the caller exactly once, as an ``otpauth://`` URI, so the
   authenticator app can register it. A pending factor confers nothing: it
   cannot satisfy a login and cannot be used for reauthentication.
2. **Confirm.** ``confirm_enrollment`` requires a valid code from the app
   *derived from the stored, decrypted seed* — which proves the user really
   holds the factor, not merely that they saw the secret — activates it, and
   returns ten single-use recovery codes. This is the only time those codes
   exist in plaintext; only their SHA-256 digests are stored.
3. **Challenge.** At login, a user who must present a second factor does not
   receive tokens. They receive a challenge token (hashed at rest, short-lived)
   and ``POST /api/mfa/verify`` resolves it into a session. Failures count on
   the challenge row and lock it; the lockout window is a lockout, not a delay.
4. **Reauthenticate.** A dangerous action asks for a fresh factor *on the
   current session* (``verify_user_code``); the session records when it last
   succeeded, which is what ``identity.service.assert_privileged`` reads.

Three properties hold at every step and are pinned by tests:

* A code can be used **once**. The accepted time step is written to
  ``mfa_factors.last_timestep`` and anything at or below it is refused, so a
  code shoulder-surfed inside its 30-second window is worthless.
* A wrong code and an expired code produce the *same* error, with no hint which
  factor was tried. Recovery-code attempts are indistinguishable from TOTP
  attempts to the caller.
* The seed never appears in a log, an audit record, or an error message. It
  lives in exactly one place after enrollment: the encrypted column.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.identity import secrets as identity_secrets
from app.auth.identity import totp
from app.auth.identity import tokens as identity_tokens
from app.auth.identity.events import emit
from app.auth.identity.exceptions import (
    IdentitySecretsUnavailable,
    MFAAlreadyEnrolled,
    MFAChallengeLocked,
    MFADisabled,
    MFAError,
    MFANotEnrolled,
    MFAVerificationFailed,
)
from app.auth.identity.models import (
    ChallengePurpose,
    FactorStatus,
    FactorType,
    MFARecoveryCode,
    MFAFactor,
    MFAChallenge,
)
from app.core.config import settings
from app.core.rate_limit import allow_identity_action
from app.db.models import AuditAction, User

log = structlog.get_logger()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


def ensure_enabled() -> None:
    if not settings.mfa_enabled:
        raise MFADisabled("Multi-factor authentication is disabled on this deployment.")


@dataclass(frozen=True)
class MFAState:
    """The whole MFA posture of one user, in one object."""

    enrolled: bool
    pending: bool
    factor_id: uuid.UUID | None
    confirmed_at: datetime | None
    last_used_at: datetime | None
    recovery_codes_remaining: int
    recovery_codes_total: int

    @property
    def recovery_codes_low(self) -> bool:
        return self.enrolled and self.recovery_codes_remaining <= 2


async def load_state(session: AsyncSession, user: User) -> MFAState:
    """Read the user's factor and count their unused recovery codes.

    Called by ``GET /api/mfa/status``, by the login path (to decide whether to
    challenge) and by the dashboard's security page.
    """
    factor = (
        await session.execute(
            select(MFAFactor).where(
                MFAFactor.user_id == user.id,
                MFAFactor.factor_type == FactorType.TOTP.value,
            )
        )
    ).scalar_one_or_none()

    total = (
        await session.execute(
            select(func.count(MFARecoveryCode.id)).where(MFARecoveryCode.user_id == user.id)
        )
    ).scalar_one()
    remaining = (
        await session.execute(
            select(func.count(MFARecoveryCode.id)).where(
                MFARecoveryCode.user_id == user.id, MFARecoveryCode.used_at.is_(None)
            )
        )
    ).scalar_one()

    return MFAState(
        enrolled=bool(factor and factor.status == FactorStatus.ACTIVE.value),
        pending=bool(factor and factor.status == FactorStatus.PENDING.value),
        factor_id=factor.id if factor else None,
        confirmed_at=factor.confirmed_at if factor else None,
        last_used_at=factor.last_used_at if factor else None,
        recovery_codes_remaining=int(remaining),
        recovery_codes_total=int(total),
    )


async def has_active_factor(session: AsyncSession, *, user_id: uuid.UUID) -> bool:
    """Whether this user has a confirmed factor, without loading the row.

    The login path asks this question on every password sign-in, so it is a
    single-column existence check rather than a full read: the answer decides
    whether to issue tokens or a challenge, and nothing else.
    """
    return (await _active_factor(session, user_id)) is not None


async def _active_factor(session: AsyncSession, user_id: uuid.UUID) -> MFAFactor | None:
    return (
        await session.execute(
            select(MFAFactor).where(
                MFAFactor.user_id == user_id,
                MFAFactor.factor_type == FactorType.TOTP.value,
                MFAFactor.status == FactorStatus.ACTIVE.value,
            )
        )
    ).scalar_one_or_none()


def _decrypt_seed(factor: MFAFactor) -> str:
    return identity_secrets.decrypt_text(
        factor.secret_encrypted,
        tenant_id=str(factor.tenant_id),
        purpose=identity_secrets.PURPOSE_TOTP,
    )


# ---------------------------------------------------------------- enrollment ---


@dataclass(frozen=True)
class EnrollmentStart:
    factor_id: uuid.UUID
    secret: str
    provisioning_uri: str
    digits: int
    period_seconds: int


async def begin_enrollment(
    session: AsyncSession,
    user: User,
    *,
    label: str = "",
    commit: bool = False,
) -> EnrollmentStart:
    """Create (or restart) a pending TOTP factor and return its seed once.

    Restarting is safe and expected — a user who closed the QR dialog has a
    pending row and must be able to start again. An *active* factor is never
    overwritten here: it must be disabled first, with reauthentication, so an
    attacker on a live session cannot silently replace the victim's factor.

    Called by ``POST /api/mfa/enroll``.
    """
    ensure_enabled()

    existing = (
        await session.execute(
            select(MFAFactor).where(
                MFAFactor.user_id == user.id,
                MFAFactor.factor_type == FactorType.TOTP.value,
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status == FactorStatus.ACTIVE.value:
        raise MFAAlreadyEnrolled(
            "A second factor is already active. Disable it before enrolling a new one."
        )

    secret = totp.generate_secret()
    try:
        envelope, key_id = identity_secrets.encrypt_text(
            secret, tenant_id=str(user.tenant_id), purpose=identity_secrets.PURPOSE_TOTP
        )
    except IdentitySecretsUnavailable:
        # Fail closed and say why: without a key ring there is nowhere safe to
        # put the seed, and storing it in the clear is not an option.
        raise

    digits = settings.mfa_totp_digits
    period = settings.mfa_totp_period_seconds

    if existing is None:
        factor = MFAFactor(
            tenant_id=user.tenant_id,
            user_id=user.id,
            factor_type=FactorType.TOTP.value,
            label=(label or "Authenticator app")[:80],
            status=FactorStatus.PENDING.value,
            secret_encrypted=envelope,
            secret_key_id=key_id,
            digits=digits,
            period_seconds=period,
        )
        session.add(factor)
    else:
        factor = existing
        factor.label = (label or factor.label or "Authenticator app")[:80]
        factor.status = FactorStatus.PENDING.value
        factor.secret_encrypted = envelope
        factor.secret_key_id = key_id
        factor.digits = digits
        factor.period_seconds = period
        factor.confirmed_at = None
        factor.last_timestep = None
    await session.flush()

    await emit(
        session,
        AuditAction.MFA_ENROLLMENT_STARTED,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        target_user_id=user.id,
        actor_email=user.email,
        detail={"factor_id": str(factor.id), "factor_type": FactorType.TOTP.value},
        commit=commit,
    )

    return EnrollmentStart(
        factor_id=factor.id,
        secret=secret,
        provisioning_uri=totp.provisioning_uri(
            secret,
            account_name=user.email,
            issuer=settings.mfa_issuer,
            digits=digits,
            period=period,
        ),
        digits=digits,
        period_seconds=period,
    )


async def confirm_enrollment(
    session: AsyncSession,
    user: User,
    *,
    code: str,
    commit: bool = False,
) -> list[str]:
    """Activate a pending factor. Returns the recovery codes, once.

    Called by ``POST /api/mfa/enroll/confirm``.
    """
    ensure_enabled()
    factor = (
        await session.execute(
            select(MFAFactor).where(
                MFAFactor.user_id == user.id,
                MFAFactor.factor_type == FactorType.TOTP.value,
                MFAFactor.status == FactorStatus.PENDING.value,
            )
        )
    ).scalar_one_or_none()
    if factor is None:
        raise MFANotEnrolled("No pending factor to confirm. Start enrollment first.")

    secret = _decrypt_seed(factor)
    step = totp.verify(
        secret,
        code,
        timestamp=_now().timestamp(),
        period=factor.period_seconds,
        digits=factor.digits,
        window=settings.mfa_totp_window,
    )
    if step is None:
        raise MFAVerificationFailed("That code is not valid. Check your app and try again.")

    factor.status = FactorStatus.ACTIVE.value
    factor.confirmed_at = _now()
    factor.last_used_at = _now()
    factor.last_timestep = step

    codes = await _issue_recovery_codes(session, user, commit=False)

    await emit(
        session,
        AuditAction.MFA_ENABLED,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        target_user_id=user.id,
        actor_email=user.email,
        detail={
            "factor_id": str(factor.id),
            "factor_type": factor.factor_type,
            "recovery_codes_issued": len(codes),
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return codes


async def _issue_recovery_codes(
    session: AsyncSession, user: User, *, commit: bool = False
) -> list[str]:
    """Replace the user's recovery codes. Returns the new plaintext, once."""
    existing = (
        await session.execute(
            select(MFARecoveryCode).where(MFARecoveryCode.user_id == user.id)
        )
    ).scalars().all()
    for row in existing:
        await session.delete(row)

    codes = identity_tokens.generate_recovery_codes(settings.mfa_recovery_code_count)
    for code in codes:
        session.add(
            MFARecoveryCode(
                tenant_id=user.tenant_id,
                user_id=user.id,
                code_hash=identity_tokens.hash_token(
                    identity_tokens.normalize_recovery_code(code)
                ),
            )
        )
    await session.flush()
    if commit:
        await session.commit()
    return codes


async def regenerate_recovery_codes(
    session: AsyncSession, user: User, *, commit: bool = False
) -> list[str]:
    """The only path that ever returns recovery codes again.

    Called by ``POST /api/mfa/recovery-codes/regenerate``, which is a
    privileged route: it requires a fresh factor (``assert_privileged``) before
    reaching here.
    """
    ensure_enabled()
    factor = await _active_factor(session, user.id)
    if factor is None:
        raise MFANotEnrolled("No active second factor.")
    codes = await _issue_recovery_codes(session, user, commit=False)

    await emit(
        session,
        AuditAction.MFA_RECOVERY_CODES_REGENERATED,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        target_user_id=user.id,
        actor_email=user.email,
        detail={"count": len(codes)},
        commit=False,
    )
    if commit:
        await session.commit()
    return codes


async def disable_factor(
    session: AsyncSession, user: User, *, reason: str = "user_disabled", commit: bool = False
) -> None:
    """Remove the user's factor and every recovery code.

    Called by ``DELETE /api/mfa`` (privileged) and by the administrative
    "reset this user's MFA" path. The rows are deleted rather than flagged:
    keeping a disabled factor would keep its seed, and there is no reason to
    retain a sealed secret that is no longer in use.
    """
    factors = (
        await session.execute(select(MFAFactor).where(MFAFactor.user_id == user.id))
    ).scalars().all()
    codes = (
        await session.execute(select(MFARecoveryCode).where(MFARecoveryCode.user_id == user.id))
    ).scalars().all()
    for row in list(factors) + list(codes):
        await session.delete(row)

    await emit(
        session,
        AuditAction.MFA_DISABLED,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        target_user_id=user.id,
        actor_email=user.email,
        detail={"reason": reason, "factors_removed": len(factors), "codes_removed": len(codes)},
        commit=False,
    )
    if commit:
        await session.commit()


# ----------------------------------------------------------------- challenge ---


@dataclass(frozen=True)
class IssuedChallenge:
    token: str
    challenge_id: uuid.UUID
    expires_at: datetime


async def create_challenge(
    session: AsyncSession,
    user: User,
    *,
    purpose: ChallengePurpose = ChallengePurpose.LOGIN,
    ip_address: str = "",
    user_agent: str = "",
    session_row_id: uuid.UUID | None = None,
    commit: bool = False,
) -> IssuedChallenge:
    """Raise a second-factor challenge and return the client's opaque token.

    Every earlier open challenge for that user is consumed, whatever its
    purpose: a user has one live challenge at a time, so an attempt cannot fan
    out into several and dilute the per-challenge failure counter. Asking for a
    new challenge is how a user recovers from one they abandoned.
    """
    ensure_enabled()
    if not await has_active_factor(session, user_id=user.id):
        # Refused rather than issued: a challenge for an account with no factor
        # could never be satisfied, and a login that asks for a code the user
        # cannot produce is indistinguishable from a broken account.
        raise MFANotEnrolled("This account has no second factor to challenge.")
    token = identity_tokens.new_token("vdmfa", 32)
    now = _now()
    expires = now + timedelta(seconds=settings.mfa_challenge_ttl_seconds)

    open_rows = (
        await session.execute(
            select(MFAChallenge).where(
                MFAChallenge.user_id == user.id,
                MFAChallenge.consumed_at.is_(None),
            )
        )
    ).scalars().all()
    for row in open_rows:
        row.consumed_at = now

    challenge = MFAChallenge(
        tenant_id=user.tenant_id,
        user_id=user.id,
        challenge_hash=identity_tokens.hash_token(token),
        purpose=purpose.value,
        ip_address=(ip_address or "")[:64],
        user_agent=(user_agent or "")[:300],
        session_id=session_row_id,
        expires_at=expires,
    )
    session.add(challenge)
    await session.flush()
    if commit:
        await session.commit()
    return IssuedChallenge(token=token, challenge_id=challenge.id, expires_at=expires)


@dataclass(frozen=True)
class ChallengeResult:
    ok: bool
    user: User
    challenge: MFAChallenge
    used_recovery_code: bool = False
    failure_reason: str = ""


async def load_challenge(
    session: AsyncSession, *, token: str
) -> MFAChallenge | None:
    """The challenge a token names, or ``None``.

    Split out of ``resolve_challenge`` so a caller can check the challenge's
    *binding* -- purpose and session -- before spending it. Resolving first and
    refusing afterwards would let a request from the wrong session consume the
    challenge the rightful one is about to answer.
    """
    return (
        await session.execute(
            select(MFAChallenge).where(
                MFAChallenge.challenge_hash == identity_tokens.hash_token(token or "")
            )
        )
    ).scalar_one_or_none()


async def resolve_challenge(
    session: AsyncSession,
    *,
    token: str,
    code: str,
    ip_address: str = "",
    commit: bool = False,
) -> ChallengeResult:
    """Verify a challenge token plus a code. Never raises for a wrong code.

    Returning a result rather than raising keeps the caller's response uniform:
    the route answers 401 with one message whether the code was wrong, the
    challenge expired, or the account has no factor.
    """
    ensure_enabled()
    challenge = await load_challenge(session, token=token)
    if challenge is None:
        raise MFAError("That verification request is no longer valid.")

    user = await session.get(User, challenge.user_id)
    if user is None:
        raise MFAError("That verification request is no longer valid.")

    now = _now()
    locked_until = _naive(challenge.locked_until)
    if locked_until is not None and locked_until > now:
        await _audit_failure(session, user, challenge, reason="locked")
        raise MFAChallengeLocked("Too many incorrect codes. Try again later.")

    if challenge.consumed_at is not None:
        raise MFAError("That verification request has already been used.")
    if _naive(challenge.expires_at) <= now:
        raise MFAError("That verification request has expired. Sign in again.")

    if not await allow_identity_action(
        action="mfa_verify",
        who=f"{user.id}:{challenge.id}",
        limit=settings.mfa_max_verifications,
        window=float(settings.mfa_verification_window_seconds),
    ):
        await _register_failure(session, challenge)
        await _audit_failure(session, user, challenge, reason="rate_limited")
        raise MFAChallengeLocked("Too many attempts. Try again later.")

    outcome = await _check_code(session, user, code)

    if not outcome.ok:
        await _register_failure(session, challenge)
        await _audit_failure(session, user, challenge, reason=outcome.reason)
        return ChallengeResult(ok=False, user=user, challenge=challenge,
                               failure_reason=outcome.reason)

    challenge.consumed_at = now
    if commit:
        await session.commit()
    return ChallengeResult(
        ok=True, user=user, challenge=challenge,
        used_recovery_code=outcome.used_recovery_code,
    )


@dataclass(frozen=True)
class _CodeOutcome:
    ok: bool
    used_recovery_code: bool = False
    reason: str = ""


async def _check_code(session: AsyncSession, user: User, code: str) -> _CodeOutcome:
    """TOTP first, then recovery codes. One shared failure path."""
    factor = await _active_factor(session, user.id)
    if factor is None:
        return _CodeOutcome(False, reason="no_factor")

    secret = _decrypt_seed(factor)
    step = totp.verify(
        secret,
        code,
        timestamp=_now().timestamp(),
        period=factor.period_seconds,
        digits=factor.digits,
        window=settings.mfa_totp_window,
        min_step=factor.last_timestep,
    )
    if step is not None:
        factor.last_timestep = step
        factor.last_used_at = _now()
        return _CodeOutcome(True)

    # Recovery codes are checked second so the common case stays cheap, and
    # through a conditional UPDATE so that two concurrent logins presenting the
    # same code cannot both succeed.
    consumed = await _consume_recovery_code(session, user, code)
    if consumed:
        return _CodeOutcome(True, used_recovery_code=True)
    return _CodeOutcome(False, reason="bad_code")


async def _consume_recovery_code(session: AsyncSession, user: User, code: str) -> bool:
    normalized = identity_tokens.normalize_recovery_code(code or "")
    if len(normalized) < 10:
        return False
    digest = identity_tokens.hash_token(normalized)
    result = await session.execute(
        update(MFARecoveryCode)
        .where(
            MFARecoveryCode.user_id == user.id,
            MFARecoveryCode.code_hash == digest,
            MFARecoveryCode.used_at.is_(None),
        )
        .values(used_at=_now())
    )
    return bool(result.rowcount)


async def verify_user_code(
    session: AsyncSession, user: User, *, code: str, commit: bool = False
) -> bool:
    """Verify a code for a *reauthentication* (no challenge token involved).

    Called by ``POST /api/identity/reauth``. Rate limited per user, because
    this endpoint is reachable by any signed-in session and is therefore the
    cheapest place to attack a second factor from inside an account that is
    already compromised.
    """
    ensure_enabled()
    if not await allow_identity_action(
        action="mfa_reauth",
        who=str(user.id),
        limit=settings.mfa_max_verifications,
        window=float(settings.mfa_verification_window_seconds),
    ):
        raise MFAChallengeLocked("Too many attempts. Try again later.")

    outcome = await _check_code(session, user, code)
    if not outcome.ok:
        await emit(
            session,
            AuditAction.MFA_FAILED,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            target_user_id=user.id,
            actor_email=user.email,
            detail={"purpose": ChallengePurpose.REAUTHENTICATE.value, "reason": outcome.reason},
            commit=False,
        )
        if commit:
            await session.commit()
        return False

    await emit(
        session,
        AuditAction.MFA_VERIFIED if not outcome.used_recovery_code else AuditAction.MFA_RECOVERY_CODE_USED,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        target_user_id=user.id,
        actor_email=user.email,
        detail={"purpose": ChallengePurpose.REAUTHENTICATE.value},
        commit=False,
    )
    if commit:
        await session.commit()
    return True


async def _register_failure(session: AsyncSession, challenge: MFAChallenge) -> None:
    challenge.failures += 1
    if challenge.failures >= settings.mfa_challenge_max_failures:
        challenge.locked_until = _now() + timedelta(minutes=settings.mfa_challenge_lockout_minutes)


async def _audit_failure(
    session: AsyncSession,
    user: User,
    challenge: MFAChallenge,
    *,
    reason: str,
) -> None:
    locked = (
        _naive(challenge.locked_until) is not None
        and _naive(challenge.locked_until) > _now()
    )
    await emit(
        session,
        AuditAction.MFA_CHALLENGE_LOCKED if locked else AuditAction.MFA_FAILED,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        target_user_id=user.id,
        actor_email=user.email,
        ip_address=challenge.ip_address,
        user_agent=challenge.user_agent,
        detail={
            "purpose": challenge.purpose,
            "reason": reason,
            "failures": challenge.failures,
            "challenge_id": str(challenge.id),
        },
        commit=False,
    )


async def recovery_code_used_event(session: AsyncSession, user: User, *, commit: bool = False) -> None:
    """Emit the recovery-code event for the login path.

    Split out because ``resolve_challenge`` returns its result to the route,
    and the route knows the request context (IP, user agent) that the
    post-login event should carry.
    """
    await emit(
        session,
        AuditAction.MFA_RECOVERY_CODE_USED,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        target_user_id=user.id,
        actor_email=user.email,
        detail={"purpose": ChallengePurpose.LOGIN.value},
        commit=commit,
    )


async def verified_event(session: AsyncSession, user: User, *, commit: bool = False) -> None:
    await emit(
        session,
        AuditAction.MFA_VERIFIED,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        target_user_id=user.id,
        actor_email=user.email,
        detail={"purpose": ChallengePurpose.LOGIN.value},
        commit=commit,
    )
