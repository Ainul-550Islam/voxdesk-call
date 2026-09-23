"""
Authentication and team-management business logic.

Kept out of the route layer so the rules -- lockout, rotation, last-owner
protection, escalation blocking -- can be tested directly and cannot be
bypassed by a second route that forgets one of them.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import jwt as jwt_utils
from app.auth import password as pw
from app.auth.rbac import can_assign_role, can_manage_user
from app.auth.identity.models import UserSession
from app.core.config import settings
from app.db.models import AuditAction, AuditLog, RefreshToken, Tenant, User, UserRole

log = structlog.get_logger()


class AuthError(Exception):
    """Authentication failed. The message is deliberately generic."""


class PermissionDenied(Exception):
    pass


class ConflictError(Exception):
    pass


def _now() -> datetime:
    return _utcnow()


def _naive_utc(value: datetime | None) -> datetime | None:
    """Columns are written naive in places; compare consistently."""
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


# ------------------------------------------------------------------- email ---

async def _idle_minutes_for(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    from app.auth.identity.policies import load_policy

    policy = await load_policy(session, tenant_id)
    return policy.session_idle_minutes


def _utcnow() -> datetime:
    """Naive UTC, matching every existing timestamp column in this schema."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_email(raw: str) -> str:
    """
    Lowercased, Unicode-normalised, deliverability not checked (that would
    make login depend on DNS). Raises ValueError on a malformed address.
    """
    try:
        result = validate_email(raw, check_deliverability=False)
    except EmailNotValidError as exc:
        raise ValueError(str(exc)) from exc
    return result.normalized.lower()


# ------------------------------------------------------------------- audit ---

async def record_audit(
    session: AsyncSession,
    *,
    action: AuditAction,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    target_user_id: uuid.UUID | None = None,
    actor_email: str = "",
    ip_address: str = "",
    user_agent: str = "",
    detail: dict | None = None,
    commit: bool = True,
) -> AuditLog:
    """`detail` must never contain a password, token or API key."""
    entry = AuditLog(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        target_user_id=target_user_id,
        action=action,
        actor_email=actor_email[:320],
        ip_address=ip_address[:64],
        user_agent=user_agent[:300],
        detail=detail or {},
    )
    session.add(entry)
    if commit:
        await session.commit()
    return entry


# ---------------------------------------------------------------- lockout ---

def is_locked(user: User) -> bool:
    locked_until = _naive_utc(user.locked_until)
    return locked_until is not None and locked_until > _utcnow()


async def _register_failure(session: AsyncSession, user: User) -> None:
    user.failed_login_count += 1
    if user.failed_login_count >= settings.max_failed_logins:
        user.locked_until = _utcnow() + timedelta(minutes=settings.lockout_minutes)
        user.failed_login_count = 0
        log.warning("auth.account_locked", user_id=str(user.id))
    await session.commit()


# --------------------------------------------------------------- authenticate ---

async def authenticate(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    ip_address: str = "",
    user_agent: str = "",
) -> User:
    """
    Returns the User or raises AuthError.

    Every failure path raises the SAME message and spends comparable CPU, so a
    caller cannot distinguish "no such account" from "wrong password" and use
    the endpoint to enumerate registered addresses.
    """
    generic = AuthError("Invalid email or password.")

    try:
        normalized = normalize_email(email)
    except ValueError:
        pw.verify_dummy()
        raise generic from None

    user = (
        await session.execute(select(User).where(User.email == normalized))
    ).scalar_one_or_none()

    if user is None:
        pw.verify_dummy()
        await record_audit(
            session, action=AuditAction.LOGIN_FAILURE, actor_email=normalized,
            ip_address=ip_address, user_agent=user_agent,
            detail={"reason": "no_such_user"},
        )
        raise generic

    if is_locked(user):
        pw.verify_dummy()
        await record_audit(
            session, action=AuditAction.LOGIN_FAILURE, tenant_id=user.tenant_id,
            actor_user_id=user.id, actor_email=normalized, ip_address=ip_address,
            user_agent=user_agent, detail={"reason": "locked"},
        )
        raise generic

    if not pw.verify_password(password, user.password_hash):
        await record_audit(
            session, action=AuditAction.LOGIN_FAILURE, tenant_id=user.tenant_id,
            actor_user_id=user.id, actor_email=normalized, ip_address=ip_address,
            user_agent=user_agent, detail={"reason": "bad_password"}, commit=False,
        )
        await _register_failure(session, user)
        raise generic

    # Correct password but disabled: still a generic failure to the caller.
    if not user.is_active:
        await record_audit(
            session, action=AuditAction.LOGIN_FAILURE, tenant_id=user.tenant_id,
            actor_user_id=user.id, actor_email=normalized, ip_address=ip_address,
            user_agent=user_agent, detail={"reason": "inactive"},
        )
        raise generic

    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        await record_audit(
            session, action=AuditAction.LOGIN_FAILURE, tenant_id=user.tenant_id,
            actor_user_id=user.id, actor_email=normalized, ip_address=ip_address,
            user_agent=user_agent, detail={"reason": "tenant_inactive"},
        )
        raise generic

    # STEP 18: tenant and domain policy. Evaluated *after* the password check,
    # so a refusal can never be used to learn whether an address exists -- by
    # the time it can happen, the caller has already proved they own the
    # account. The reason is recorded for the operator, never returned.
    refusal = await _login_policy_refusal(session, user)
    if refusal is not None:
        await record_audit(
            session, action=AuditAction.LOGIN_FAILURE, tenant_id=user.tenant_id,
            actor_user_id=user.id, actor_email=normalized, ip_address=ip_address,
            user_agent=user_agent, detail={"reason": refusal},
        )
        raise generic

    # Transparent upgrade if the cost factor was raised since signup.
    if pw.needs_rehash(user.password_hash):
        user.password_hash = pw.hash_password(password)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = _utcnow()
    await record_audit(
        session, action=AuditAction.LOGIN_SUCCESS, tenant_id=user.tenant_id,
        actor_user_id=user.id, actor_email=normalized, ip_address=ip_address,
        user_agent=user_agent, commit=False,
    )
    await session.commit()
    return user


async def _login_policy_refusal(session: AsyncSession, user: User) -> str | None:
    """The reason a password login may not proceed, or ``None``.

    Returns a short reason code rather than raising, because the caller must
    translate *every* outcome into the same generic 401. A tenant that requires
    SSO, one that has switched password login off, one whose domains demand a
    federated login, and one that restricts login to listed email domains all
    land here.
    """
    from app.auth.identity import policies

    policy = await policies.load_policy(session, user.tenant_id)
    domain = await policies.load_domain_evaluation(session, user.email)
    evaluation = policies.evaluate_login_policy(
        policy, email=user.email, role=user.role, domain_policy=domain
    )
    if evaluation.allowed:
        return None
    return evaluation.reason or "policy_denied"


# ------------------------------------------------------------------ tokens ---

@dataclass(frozen=True)
class LoginRequirements:
    """What a caller must still satisfy after proving their password.

    Computed *after* a successful password check, so it can never be used to
    learn anything about an address that has not already authenticated.
    """

    mfa_required: bool
    mfa_available: bool
    enrollment_required: bool
    sso_required: bool
    idle_minutes: int
    session_max_active: int


async def post_login_requirements(session: AsyncSession, user: User) -> LoginRequirements:
    """Whether this user must present a second factor, and whether they can.

    ``POST /auth/login`` calls this immediately after ``authenticate``. The
    interesting case is ``mfa_required and not mfa_available``: the tenant
    requires a second factor and the user has never enrolled one. Refusing the
    login would lock them out of the only page where they could fix it, so the
    login succeeds with ``enrollment_required`` set and the dashboard sends
    them to enrollment. What the requirement *does* gate is everything
    privileged: ``identity.service.assert_privileged`` still demands a fresh
    proof of presence, which for this user is their password.
    """
    from app.auth.identity import mfa as identity_mfa
    from app.auth.identity import policies

    policy = await policies.load_policy(session, user.tenant_id)
    domain = await policies.load_domain_evaluation(session, user.email)
    evaluation = policies.evaluate_login_policy(
        policy,
        email=user.email,
        role=user.role,
        mfa_available=True,
        user_mfa_override=user.mfa_required,
        domain_policy=domain,
    )
    has_factor = await identity_mfa.has_active_factor(session, user_id=user.id)
    return LoginRequirements(
        mfa_required=evaluation.mfa_required,
        mfa_available=has_factor,
        enrollment_required=evaluation.mfa_required and not has_factor,
        sso_required=evaluation.sso_required,
        idle_minutes=policy.session_idle_minutes,
        session_max_active=policy.session_max_active,
    )


async def issue_tokens(
    session: AsyncSession,
    user: User,
    *,
    ip_address: str = "",
    user_agent: str = "",
    auth_method: str = "password",
    mfa_verified: bool = False,
    password_confirmed: bool = True,
    session_row: "object | None" = None,
    sso_connection_id: uuid.UUID | None = None,
) -> dict:
    """Mint an access/refresh pair plus the session they belong to.

    The defaults reproduce the pre-STEP-18 behaviour exactly -- a password login
    that opens a session and records that the password was just checked -- and
    the return value only *adds* keys, so an existing caller sees no change.
    """
    if session_row is None:
        from app.auth.identity.policies import AuthMethod
        from app.auth.identity.service import ensure_session

        session_row, _label = await ensure_session(
            session,
            user,
            auth_method=AuthMethod(auth_method),
            mfa_verified=mfa_verified,
            password_confirmed=password_confirmed and auth_method == "password",
            ip_address=ip_address,
            user_agent=user_agent,
            sso_connection_id=sso_connection_id,
            commit=False,
        )

    access, expires_in = jwt_utils.create_access_token(
        user_id=user.id, tenant_id=user.tenant_id,
        role=user.role.value, token_version=user.token_version,
        session_id=getattr(session_row, "id", None),
        auth_method=auth_method,
        mfa_verified=mfa_verified,
    )
    plaintext, digest = jwt_utils.generate_refresh_token()
    session.add(RefreshToken(
        user_id=user.id,
        token_hash=digest,
        expires_at=jwt_utils.refresh_expiry().replace(tzinfo=None),
        user_agent=user_agent[:300],
        ip_address=ip_address[:64],
        session_id=getattr(session_row, "id", None),
    ))
    await session.commit()
    return {
        "access_token": access,
        "refresh_token": plaintext,
        "token_type": "bearer",
        "expires_in": expires_in,
        "session_id": str(getattr(session_row, "id", "")) or None,
    }


async def rotate_refresh_token(
    session: AsyncSession, plaintext: str, *, ip_address: str = "", user_agent: str = ""
) -> dict:
    """
    Single-use rotation with reuse detection.

    Presenting an already-used token means the token leaked, so every live
    refresh token for that user is revoked and the access tokens are
    invalidated by bumping token_version.
    """
    generic = AuthError("Invalid refresh token.")
    digest = jwt_utils.hash_refresh_token(plaintext)

    record = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == digest))
    ).scalar_one_or_none()
    if record is None:
        raise generic

    user = await session.get(User, record.user_id)

    if record.used_at is not None or record.revoked_at is not None:
        log.warning("auth.refresh_reuse_detected", user_id=str(record.user_id))
        await revoke_all_for_user(
            session,
            record.user_id,
            bump_version=True,
            identity_reason="refresh_reuse_detected",
        )
        if user is not None:
            await record_audit(
                session, action=AuditAction.AUTHZ_DENIED, tenant_id=user.tenant_id,
                actor_user_id=user.id, actor_email=user.email, ip_address=ip_address,
                detail={"reason": "refresh_token_reuse"},
            )
        raise generic

    if _naive_utc(record.expires_at) <= _utcnow():
        raise generic

    if user is None or not user.is_active:
        raise generic

    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise generic

    # STEP 18: a refresh token issued for a session dies with that session, so
    # "sign out this device" really signs it out rather than waiting for the
    # token to expire. Rows created before sessions existed carry no
    # session_id and keep rotating exactly as they used to.
    session_row = None
    if record.session_id is not None:
        from app.auth.identity import sessions as identity_sessions

        session_row = await session.get(UserSession, record.session_id)
        if session_row is None or not session_row.is_live(_utcnow()):
            log.info(
                "auth.refresh_for_dead_session",
                user_id=str(user.id),
                session_id=str(record.session_id),
            )
            raise generic
        record.used_at = _utcnow()
        await identity_sessions.touch(
            session,
            session_row,
            ip_address=ip_address,
            user_agent=user_agent,
            idle_minutes=await _idle_minutes_for(session, user.tenant_id),
        )
    else:
        record.used_at = _utcnow()

    tokens = await issue_tokens(
        session,
        user,
        ip_address=ip_address,
        user_agent=user_agent,
        auth_method=session_row.auth_method if session_row else "password",
        mfa_verified=bool(session_row.mfa_verified) if session_row else False,
        password_confirmed=False,
        session_row=session_row,
    )

    replacement = (
        await session.execute(
            select(RefreshToken)
            .where(RefreshToken.token_hash == jwt_utils.hash_refresh_token(
                tokens["refresh_token"]
            ))
        )
    ).scalar_one_or_none()
    if replacement is not None:
        record.replaced_by = replacement.id

    await record_audit(
        session, action=AuditAction.TOKEN_REFRESH, tenant_id=user.tenant_id,
        actor_user_id=user.id, actor_email=user.email, ip_address=ip_address,
        commit=False,
    )
    await session.commit()
    return tokens


async def revoke_refresh_token(session: AsyncSession, plaintext: str) -> bool:
    digest = jwt_utils.hash_refresh_token(plaintext)
    record = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == digest))
    ).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        return False
    record.revoked_at = _utcnow()
    await session.commit()
    return True


async def revoke_all_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    bump_version: bool = False,
    identity_reason: str | None = None,
) -> int:
    """Revoke every outstanding refresh token for a user.

    ``bump_version`` additionally invalidates the access tokens already handed
    out (the JWT carries the version it was minted with).

    ``identity_reason`` closes the third hole: the ``user_sessions`` rows. A
    caller that passes one gets that user's live sessions revoked too, so "all
    credentials are dead" is true in the session list and not only in the token
    tables. It is opt-in rather than automatic because several callers (logout,
    password reset, MFA change) revoke sessions themselves with a more specific
    reason and an explicit ``keep_session_id``; revoking here first would
    overwrite theirs.
    """
    rows = (
        await session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
            )
        )
    ).scalars().all()
    now = _utcnow()
    for row in rows:
        row.revoked_at = now
    if bump_version:
        user = await session.get(User, user_id)
        if user is not None:
            user.token_version += 1
    if identity_reason:
        from app.auth.identity import sessions as identity_sessions

        await identity_sessions.revoke_all_for_user(
            session, user_id=user_id, reason=identity_reason, commit=False
        )
    await session.commit()
    return len(rows)


# --------------------------------------------------------- team management ---

async def count_active_owners(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count(User.id)).where(
                User.tenant_id == tenant_id,
                User.role == UserRole.OWNER,
                User.is_active.is_(True),
            )
        )
    ).scalar_one()


async def create_user(
    session: AsyncSession,
    *,
    actor: User,
    email: str,
    password: str,
    role: UserRole,
    full_name: str = "",
    tenant_id: uuid.UUID | None = None,
) -> User:
    """
    The new user is always created inside the ACTOR's tenant. `tenant_id` is
    accepted only so a caller can be explicit, and a mismatch is rejected --
    it can never be used to plant a user in someone else's tenant.
    """
    if tenant_id is not None and tenant_id != actor.tenant_id:
        raise PermissionDenied("Cannot create a user in another tenant.")

    if not can_assign_role(actor.role, role):
        raise PermissionDenied(
            f"A {actor.role.value} cannot grant the {role.value} role."
        )

    normalized = normalize_email(email)
    pw.validate_policy(password, email=normalized)

    existing = (
        await session.execute(select(User).where(User.email == normalized))
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("That email address is already registered.")

    user = User(
        tenant_id=actor.tenant_id,
        email=normalized,
        full_name=full_name.strip()[:200],
        password_hash=pw.hash_password(password),
        role=role,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    await record_audit(
        session, action=AuditAction.USER_CREATED, tenant_id=actor.tenant_id,
        actor_user_id=actor.id, target_user_id=user.id, actor_email=actor.email,
        detail={"role": role.value}, commit=False,
    )
    await session.commit()
    await session.refresh(user)
    return user


async def change_role(
    session: AsyncSession, *, actor: User, target: User, new_role: UserRole
) -> User:
    if target.tenant_id != actor.tenant_id:
        raise PermissionDenied("Cross-tenant modification is not allowed.")
    if target.id == actor.id:
        raise PermissionDenied("You cannot change your own role.")
    if not can_manage_user(actor.role, target.role):
        raise PermissionDenied("You cannot modify a user at or above your level.")
    if not can_assign_role(actor.role, new_role):
        raise PermissionDenied(f"You cannot grant the {new_role.value} role.")

    # Demoting the last owner would lock the tenant out of its own settings.
    if target.role is UserRole.OWNER and new_role is not UserRole.OWNER:
        if await count_active_owners(session, target.tenant_id) <= 1:
            raise ConflictError("A tenant must always have at least one active owner.")

    previous = target.role
    target.role = new_role
    target.token_version += 1          # old access tokens stop working now
    await revoke_all_for_user(session, target.id, identity_reason="role_changed")
    await record_audit(
        session, action=AuditAction.ROLE_CHANGED, tenant_id=actor.tenant_id,
        actor_user_id=actor.id, target_user_id=target.id, actor_email=actor.email,
        detail={"from": previous.value, "to": new_role.value}, commit=False,
    )
    await session.commit()
    await session.refresh(target)
    return target


async def set_active(
    session: AsyncSession, *, actor: User, target: User, active: bool
) -> User:
    if target.tenant_id != actor.tenant_id:
        raise PermissionDenied("Cross-tenant modification is not allowed.")
    if target.id == actor.id and not active:
        raise PermissionDenied("You cannot deactivate yourself.")
    if not can_manage_user(actor.role, target.role):
        raise PermissionDenied("You cannot modify a user at or above your level.")

    if not active and target.role is UserRole.OWNER:
        if await count_active_owners(session, target.tenant_id) <= 1:
            raise ConflictError("A tenant must always have at least one active owner.")

    target.is_active = active
    target.token_version += 1
    if not active:
        await revoke_all_for_user(
            session, target.id, identity_reason="user_deactivated"
        )
    await record_audit(
        session,
        action=AuditAction.USER_DEACTIVATED if not active else AuditAction.USER_REACTIVATED,
        tenant_id=actor.tenant_id, actor_user_id=actor.id, target_user_id=target.id,
        actor_email=actor.email, commit=False,
    )
    await session.commit()
    await session.refresh(target)
    return target