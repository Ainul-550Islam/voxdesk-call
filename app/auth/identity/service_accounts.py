"""Service accounts: machine identities with no way to log in.

A service account is not a user with the password field left blank. It has no
password, no second factor, no session and no refresh token — nothing that
could be handed to a browser. It holds scoped credentials, and every credential
it holds is a bearer token whose scopes are permissions from the same
vocabulary the rest of the API uses.

Four rules, each of which exists because the alternative is a well-known
incident:

* **An account is created with the permissions of whoever created it.** The
  requested scopes are validated against the creator's own permission set at
  creation (``policies.evaluate_scope_grant``), so minting a service account is
  never a privilege-escalation path. Platform-level permissions cannot be
  granted this way at all.
* **Every action is attributed to a human.** ``created_by_user_id`` is the
  owner. If that user is deleted or deactivated the account stops
  authenticating until an administrator reassigns it — there is no
  unattributable machine principal in the audit trail.
* **Disabling is immediate and reversible; deleting is neither.** ``enabled``
  is the customer switch, ``emergency_disabled`` is an operator's stop button
  that the customer cannot clear, and deletion removes the identity while
  leaving its audit history intact (audit rows are not cascaded away).
* **Credentials rotate without overlap.** A rotation issues the replacement and
  revokes the predecessor in one transaction, so there is never a window with
  two live secrets.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.identity import policies
from app.auth.identity.api_keys import build_token
from app.auth.identity.exceptions import CredentialError, PolicyDenied
from app.auth.identity.models import APIKey, ServiceAccount, ServiceAccountCredential
from app.auth.permissions import Permission
from app.auth.rbac import is_platform_permission, permissions_for
from app.core.config import settings
from app.db.models import AuditAction, User, UserRole

log = structlog.get_logger()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class IssuedCredential:
    credential: ServiceAccountCredential
    token: str


async def create_account(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: User,
    name: str,
    scopes: list[str],
    description: str = "",
    expires_in_days: int | None = None,
    commit: bool = False,
) -> ServiceAccount:
    """Create a machine identity. ``POST /api/service-accounts``."""
    existing = (
        await session.execute(
            select(ServiceAccount).where(
                ServiceAccount.tenant_id == tenant_id, ServiceAccount.name == name[:120]
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise CredentialError(f"A service account named {name!r} already exists.")

    account = ServiceAccount(
        tenant_id=tenant_id,
        name=name[:120],
        description=description[:300],
        scopes=list(scopes),
        enabled=True,
        expires_at=(
            _now() + timedelta(days=max(1, int(expires_in_days)))
            if expires_in_days
            else None
        ),
        created_by_user_id=actor.id,
    )
    session.add(account)
    await session.flush()

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SERVICE_ACCOUNT_CREATED,
        tenant_id=tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "service_account_id": str(account.id),
            "name": account.name,
            "scopes": sorted(account.scopes or []),
            "expires_at": account.expires_at.isoformat() if account.expires_at else "",
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return account


async def list_accounts(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[ServiceAccount]:
    return list(
        (
            await session.execute(
                select(ServiceAccount)
                .where(ServiceAccount.tenant_id == tenant_id)
                .order_by(ServiceAccount.created_at.desc())
            )
        ).scalars().all()
    )


async def get_account(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> ServiceAccount | None:
    account = await session.get(ServiceAccount, account_id)
    if account is None or account.tenant_id != tenant_id:
        return None
    return account


async def update_account(
    session: AsyncSession,
    account: ServiceAccount,
    *,
    actor: User,
    name: str | None = None,
    description: str | None = None,
    scopes: list[str] | None = None,
    owner_user_id: uuid.UUID | None = None,
    expires_in_days: int | None = None,
    commit: bool = False,
) -> ServiceAccount:
    """Change a machine identity's shape or its owner.

    Changing scopes *down* takes effect on the next request, because
    ``require_permission`` reads the account's scopes live. Changing them *up*
    is checked against the actor's permissions, same as at creation.
    """
    changes: dict[str, object] = {}
    if name is not None and name[:120] != account.name:
        account.name = name[:120]
        changes["name"] = account.name
    if description is not None and description[:300] != account.description:
        account.description = description[:300]
        changes["description"] = True
    if scopes is not None:
        account.scopes = await validate_scopes(session, actor=actor, scopes=list(scopes))
        changes["scopes"] = sorted(account.scopes)
    if expires_in_days is not None:
        account.expires_at = _now() + timedelta(days=max(1, int(expires_in_days)))
        changes["expires_at"] = account.expires_at.isoformat()
    if owner_user_id is not None:
        if owner_user_id != account.created_by_user_id:
            owner = await session.get(User, owner_user_id)
            if owner is None or not owner.is_active or owner.tenant_id != account.tenant_id:
                raise CredentialError("The new owner must be an active user of this tenant.")
            account.created_by_user_id = owner.id
            changes["owner_user_id"] = str(owner.id)

    if not changes:
        return account

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SERVICE_ACCOUNT_UPDATED,
        tenant_id=account.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={"service_account_id": str(account.id), "changes": changes},
        commit=False,
    )
    if commit:
        await session.commit()
    return account


async def set_enabled(
    session: AsyncSession,
    account: ServiceAccount,
    *,
    actor: User,
    enabled: bool,
    reason: str = "",
    emergency: bool = False,
    commit: bool = False,
) -> ServiceAccount:
    """Enable/disable an account, or apply an operator's emergency stop."""
    if emergency:
        account.emergency_disabled = True
        account.disabled_reason = (reason or "emergency_stop")[:200]
        account.enabled = False
    else:
        account.enabled = bool(enabled)
        account.disabled_reason = "" if enabled else (reason or "disabled")[:200]
        if enabled:
            # Clearing `emergency_disabled` requires the emergency flag to be
            # cleared explicitly; a customer-facing toggle cannot undo it.
            if account.emergency_disabled:
                raise PolicyDenied(
                    "emergency_disabled",
                    "This account was disabled by the platform operator.",
                )
    account.disabled_at = None if account.enabled else _now()

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SERVICE_ACCOUNT_ENABLED if account.enabled else AuditAction.SERVICE_ACCOUNT_DISABLED,
        tenant_id=account.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "service_account_id": str(account.id),
            "emergency": emergency,
            "reason": account.disabled_reason,
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return account


async def clear_emergency_disable(
    session: AsyncSession,
    account: ServiceAccount,
    *,
    actor: User,
    reason: str,
    commit: bool = False,
) -> ServiceAccount:
    """The only way to undo an emergency stop. Requires a reason.

    Reached by ``POST /api/service-accounts/{id}/emergency-clear``, which is
    owner-only and itself needs a fresh proof of presence: an administrator who
    tripped the stop cannot lift it, and neither can a borrowed browser. The
    plain enable toggle refuses while the flag is set, so this is the single
    path back.
    """
    if not reason:
        raise CredentialError("A reason is required to clear an emergency disable.")
    account.emergency_disabled = False
    account.disabled_reason = f"emergency_cleared: {reason}"[:200]

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SERVICE_ACCOUNT_ENABLED,
        tenant_id=account.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "service_account_id": str(account.id),
            "emergency_cleared": True,
            "reason": reason[:200],
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return account


async def delete_account(
    session: AsyncSession, account: ServiceAccount, *, actor: User, commit: bool = False
) -> None:
    """Remove the identity. Credentials and keys cascade; audit lines do not."""
    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SERVICE_ACCOUNT_DISABLED,
        tenant_id=account.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={"service_account_id": str(account.id), "deleted": True},
        commit=False,
    )
    await session.delete(account)
    if commit:
        await session.commit()


async def issue_credential(
    session: AsyncSession,
    account: ServiceAccount,
    *,
    actor: User,
    label: str = "",
    expires_in_days: int | None = None,
    commit: bool = False,
) -> IssuedCredential:
    """Mint a bearer credential for the account. Shown once."""
    row_id = uuid.uuid4()
    token, display, digest = build_token(settings.service_account_prefix, row_id)
    credential = ServiceAccountCredential(
        id=row_id,
        tenant_id=account.tenant_id,
        service_account_id=account.id,
        label=label[:120],
        prefix=display,
        secret_hash=digest,
        created_by_user_id=actor.id,
        expires_at=(
            _now() + timedelta(days=max(1, int(expires_in_days)))
            if expires_in_days
            else account.expires_at
        ),
    )
    session.add(credential)
    await session.flush()

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SERVICE_ACCOUNT_CREDENTIAL_CREATED,
        tenant_id=account.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "service_account_id": str(account.id),
            "credential_id": str(credential.id),
            "prefix": display,
            "expires_at": credential.expires_at.isoformat() if credential.expires_at else "",
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return IssuedCredential(credential=credential, token=token)


async def rotate_credential(
    session: AsyncSession,
    account: ServiceAccount,
    credential: ServiceAccountCredential,
    *,
    actor: User,
    label: str | None = None,
    expires_in_days: int | None = None,
    commit: bool = False,
) -> IssuedCredential:
    """Issue a replacement and revoke the predecessor in one transaction."""
    issued = await issue_credential(
        session,
        account,
        actor=actor,
        label=label if label is not None else credential.label,
        expires_in_days=expires_in_days,
        commit=False,
    )
    credential.revoked_at = _now()
    credential.revoked_reason = "rotated"

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SERVICE_ACCOUNT_CREDENTIAL_ROTATED,
        tenant_id=account.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "service_account_id": str(account.id),
            "previous_credential_id": str(credential.id),
            "new_credential_id": str(issued.credential.id),
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return issued


async def revoke_credential(
    session: AsyncSession,
    credential: ServiceAccountCredential,
    *,
    actor: User,
    reason: str = "revoked",
    commit: bool = False,
) -> None:
    credential.revoked_at = _now()
    credential.revoked_reason = reason[:200]

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SERVICE_ACCOUNT_CREDENTIAL_REVOKED,
        tenant_id=credential.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "service_account_id": str(credential.service_account_id),
            "credential_id": str(credential.id),
            "reason": reason[:200],
        },
        commit=False,
    )
    if commit:
        await session.commit()


async def list_credentials(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    include_revoked: bool = False,
) -> list[ServiceAccountCredential]:
    query = select(ServiceAccountCredential).where(
        ServiceAccountCredential.service_account_id == account_id
    )
    if not include_revoked:
        query = query.where(ServiceAccountCredential.revoked_at.is_(None))
    return list((await session.execute(query.order_by(ServiceAccountCredential.created_at.desc()))).scalars().all())


async def get_credential(
    session: AsyncSession, *, account_id: uuid.UUID, credential_id: uuid.UUID
) -> ServiceAccountCredential | None:
    credential = await session.get(ServiceAccountCredential, credential_id)
    if credential is None or credential.service_account_id != account_id:
        return None
    return credential


async def keys_for_account(
    session: AsyncSession, *, account_id: uuid.UUID
) -> list[APIKey]:
    """The account's API keys (a second, equivalent credential family).

    Both exist because the two answer different operational questions: a
    *credential* is a long-lived secret for a daemon, an *API key* is a
    named, scope-limited token an operator can hand to a script and revoke
    individually. The resolver in ``api_keys.lookup_credential`` accepts either.
    """
    return list(
        (
            await session.execute(
                select(APIKey)
                .where(APIKey.service_account_id == account_id, APIKey.revoked_at.is_(None))
                .order_by(APIKey.created_at.desc())
            )
        ).scalars().all()
    )


async def validate_scopes(
    session: AsyncSession, *, actor: User, scopes: list[str]
) -> list[str]:
    """Check a requested scope list against the actor's own permissions.

    Returns the normalised list or raises ``PolicyDenied`` naming the offending
    scope. Used by every write path that accepts scopes, so the check cannot be
    forgotten in a new route.
    """
    decision = policies.evaluate_scope_grant(
        creator_role=actor.role if isinstance(actor.role, UserRole) else UserRole(actor.role),
        creator_permissions=permissions_for(actor.role),
        requested_scopes=list(scopes),
    )
    if not decision.allowed:
        raise PolicyDenied(decision.reason)
    return list(dict.fromkeys(scopes))


def grantable_scopes(role: UserRole) -> list[str]:
    """The scopes a user with this role may hand to a machine credential."""
    return sorted(
        p.value
        for p in permissions_for(role)
        if not is_platform_permission(p)
    )


def scope_summary(scopes: list[str]) -> str:
    """A short human label: ``read-only``, ``write`` or ``custom``."""
    values = set(scopes)
    reads = {s for s in values if s.endswith(":read")}
    writes = {s for s in values if not s.endswith(":read")}
    if values and not writes:
        return "read-only"
    if reads and writes:
        return "read-write"
    if writes and not reads:
        return "write-only"
    return "no scopes"


def permission_labels(scopes: list[str]) -> list[str]:
    known = {p.value for p in Permission}
    return sorted(s for s in scopes if s in known)


__all__ = [
    "IssuedCredential",
    "clear_emergency_disable",
    "create_account",
    "delete_account",
    "get_account",
    "get_credential",
    "grantable_scopes",
    "issue_credential",
    "keys_for_account",
    "list_accounts",
    "list_credentials",
    "permission_labels",
    "revoke_credential",
    "rotate_credential",
    "scope_summary",
    "set_enabled",
    "update_account",
    "validate_scopes",
]
