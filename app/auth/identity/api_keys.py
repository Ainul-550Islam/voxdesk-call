"""API keys: scoped machine credentials that are shown once and stored hashed.

A key is a bearer token shaped ``vdk_<row-id-hex>_<secret>``. The middle part
is the primary key of the row, which makes lookup an exact, indexed get instead
of a scan over hashes; the secret is the only part that is secret. Only the
SHA-256 of the secret is stored, so the plaintext exists in exactly two places:
the response to the call that created it, and the operator who copied it.

Two rules make a leaked key survivable:

* **Scopes are permissions.** ``scopes`` is a list of ``Permission`` values, so
  "read-only key" is not a policy the code must remember to enforce — a key
  holding ``lead:read`` cannot satisfy a route asking for ``lead:write``,
  because ``require_permission`` refuses on the missing scope. There is no
  wildcard and no ``"*"``.
* **A key cannot exceed its owner.** At creation the requested scopes are
  checked against the creator's own permissions (``evaluate_scope_grant``), and
  at every request the effective permission is the *intersection* of the
  owner's current role permissions and the key's scopes. Demoting the owner
  therefore demotes their keys immediately, without having to find and revoke
  them.

Revocation is immediate and permanent: the row stays (so the audit trail still
names it) with ``revoked_at`` set, and ``authenticate_credential`` refuses
anything revoked, expired, or belonging to a disabled owner, tenant, or service
account.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.identity import events as identity_events
from app.auth.identity import policies
from app.auth.identity import tokens as identity_tokens
from app.auth.identity.exceptions import (
    CredentialError,
    PolicyDenied,
)
from app.auth.identity.models import APIKey, ServiceAccount, ServiceAccountCredential
from app.auth.identity.principals import AuthenticatedPrincipal, PrincipalKind
from app.auth.permissions import Permission
from app.core.config import settings
from app.db.models import AuditAction, Tenant, User

log = structlog.get_logger()

#: Don't write `last_used_at` more often than this. It is operational data, not
#: a ledger: a key used a thousand times a minute should not produce a thousand
#: updates, and "last used 12:03" answers every real question.
LAST_USED_WRITE_INTERVAL = timedelta(seconds=60)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


def build_token(prefix: str, row_id: uuid.UUID) -> tuple[str, str, str]:
    """Mint a credential. Returns ``(token, display_prefix, secret_hash)``.

    The prefix stored on the row — ``vdk_1a2b3c4d`` — is what a human uses to
    tell two keys apart in the list view, and it is deliberately not unique on
    its own (two keys can share eight hex characters; the row id is the
    identity).
    """
    secret = identity_tokens.new_secret(32)
    token = f"{prefix}_{row_id.hex}_{secret}"
    display = f"{prefix}_{row_id.hex[:8]}"
    return token, display, identity_tokens.hash_token(secret)


def split_token(token: str) -> tuple[str, uuid.UUID, str] | None:
    """Inverse of :func:`build_token`. ``None`` if the shape is wrong."""
    parts = (token or "").split("_", 2)
    if len(parts) != 3:
        return None
    prefix, raw_id, secret = parts
    if not prefix or not secret:
        return None
    try:
        row_id = uuid.UUID(hex=raw_id)
    except ValueError:
        return None
    return prefix, row_id, secret


@dataclass(frozen=True)
class IssuedKey:
    key: APIKey
    token: str


async def create_key(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: User,
    name: str,
    scopes: list[str],
    expires_in_days: int | None = None,
    owner_user_id: uuid.UUID | None = None,
    service_account_id: uuid.UUID | None = None,
    commit: bool = False,
) -> IssuedKey:
    """Create a key. The plaintext is returned once and never stored.

    Called by ``POST /api/api-keys`` (human-owned) and
    ``POST /api/service-accounts/{id}/keys``.
    """
    # Enforcement lives in the service, not only in the route: a key may never
    # carry a scope its creator does not hold, so a future call site cannot
    # forget the check and silently mint privilege.
    from app.auth.identity.service_accounts import validate_scopes

    scopes = await validate_scopes(session, actor=actor, scopes=list(scopes))

    row_id = uuid.uuid4()
    token, display, digest = build_token(settings.api_key_prefix, row_id)
    expires_at = (
        _now() + timedelta(days=max(1, int(expires_in_days))) if expires_in_days else None
    )
    key = APIKey(
        id=row_id,
        tenant_id=tenant_id,
        user_id=owner_user_id,
        service_account_id=service_account_id,
        name=name[:120],
        prefix=display,
        secret_hash=digest,
        scopes=list(scopes),
        created_by_user_id=actor.id,
        expires_at=expires_at,
    )
    session.add(key)
    await session.flush()
    if commit:
        await session.commit()
    return IssuedKey(key=key, token=token)


async def list_keys(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID | None = None,
    service_account_id: uuid.UUID | None = None,
    include_revoked: bool = False,
) -> list[APIKey]:
    query = select(APIKey).where(APIKey.tenant_id == tenant_id)
    if owner_user_id is not None:
        query = query.where(APIKey.user_id == owner_user_id)
    if service_account_id is not None:
        query = query.where(APIKey.service_account_id == service_account_id)
    if not include_revoked:
        query = query.where(APIKey.revoked_at.is_(None))
    return list((await session.execute(query.order_by(APIKey.created_at.desc()))).scalars().all())


async def get_key(session: AsyncSession, *, tenant_id: uuid.UUID, key_id: uuid.UUID) -> APIKey | None:
    key = await session.get(APIKey, key_id)
    if key is None or key.tenant_id != tenant_id:
        return None
    return key


async def revoke_key(
    session: AsyncSession,
    key: APIKey,
    *,
    actor: User,
    reason: str = "revoked_by_admin",
    commit: bool = False,
) -> APIKey:
    """Revoke a key. Idempotent: revoking a revoked key writes nothing.

    The update is conditional so that a retrying administrator (or a double
    click) cannot produce a second revocation entry in the audit trail — the
    trail is evidence, and two entries for one event makes it evidence of
    something that did not happen.
    """
    key_id = key.id
    tenant_id = key.tenant_id
    prefix = key.prefix
    owner_user_id = str(key.user_id) if key.user_id else ""
    service_account_id = str(key.service_account_id) if key.service_account_id else ""

    revoked = await session.execute(
        update(APIKey)
        .where(APIKey.id == key_id, APIKey.revoked_at.is_(None))
        .values(revoked_at=_now(), revoked_reason=reason[:200])
        .execution_options(synchronize_session=False)
    )
    if revoked.rowcount == 0:
        if key in session:
    # The compare-and-set deliberately bypasses the identity map, so the
    # caller's own copy has to be brought back in line with the row: otherwise
    # the next statement in *this* session would still see a live credential.
            await session.refresh(key)
        if commit:
            await session.commit()
        return key

    if key in session:
    # The compare-and-set deliberately bypasses the identity map, so the
    # caller's own copy has to be brought back in line with the row: otherwise
    # the next statement in *this* session would still see a live credential.
        await session.refresh(key)

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.API_KEY_REVOKED,
        tenant_id=tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "key_id": str(key_id),
            "key_prefix": prefix,
            "reason": reason[:200],
            "owner_user_id": owner_user_id,
            "service_account_id": service_account_id,
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return key


async def rotate_key(
    session: AsyncSession,
    key: APIKey,
    *,
    actor: User,
    expires_in_days: int | None = None,
    commit: bool = False,
) -> IssuedKey:
    """Issue a replacement and revoke the old one in the same transaction.

    Rotation is deliberately *not* "two keys valid at once" — that window is
    where leaked credentials hide. The old key stops working the moment the new
    one exists, and ``rotated_from_id`` preserves the lineage for the audit
    trail.

    Called by ``POST /api/api-keys/{id}/rotate``.
    """
    row_id = uuid.uuid4()
    token, display, digest = build_token(settings.api_key_prefix, row_id)
    replacement = APIKey(
        id=row_id,
        tenant_id=key.tenant_id,
        user_id=key.user_id,
        service_account_id=key.service_account_id,
        name=key.name,
        prefix=display,
        secret_hash=digest,
        scopes=list(key.scopes or []),
        created_by_user_id=actor.id,
        expires_at=(
            _now() + timedelta(days=max(1, int(expires_in_days)))
            if expires_in_days
            else key.expires_at
        ),
        rotated_from_id=key.id,
    )
    session.add(replacement)
    key.revoked_at = _now()
    key.revoked_reason = "rotated"
    await session.flush()

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.API_KEY_ROTATED,
        tenant_id=key.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "previous_key_id": str(key.id),
            "new_key_id": str(replacement.id),
            "key_prefix": replacement.prefix,
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return IssuedKey(key=replacement, token=token)


# ------------------------------------------------------------- verification ---


@dataclass(frozen=True)
class CredentialLookup:
    """What the token resolved to, before any policy was applied."""

    kind: PrincipalKind
    tenant_id: uuid.UUID
    scopes: frozenset[str]
    api_key: APIKey | None = None
    service_account: ServiceAccount | None = None
    service_account_credential: ServiceAccountCredential | None = None
    actor_user_id: uuid.UUID | None = None
    display_name: str = ""
    detail: str = ""


async def _touch_last_used(session: AsyncSession, row) -> None:
    now = _now()
    previous = _naive(getattr(row, "last_used_at", None))
    if previous is None or now - previous >= LAST_USED_WRITE_INTERVAL:
        row.last_used_at = now
async def _reject_credential(
    session: AsyncSession,
    *,
    reason: str,
    tenant_id: uuid.UUID | None,
    kind: PrincipalKind,
    credential: str = "",
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """Record a refused credential, then the caller refuses it.

    The refusal itself stays uniform (``Invalid credential``) so a caller learns
    nothing from it. The audit row is where the *operator* learns that a
    credential which should be dead is still being presented -- which is the
    difference between an integration that is misconfigured and a key somebody
    else still holds. Only a token that resolved to a real row reaches this
    function, so the trail cannot be flooded by guessing.
    """
    await identity_events.emit(
        session,
        AuditAction.CREDENTIAL_AUTH_REJECTED,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        # ``prefix`` and not ``credential``: the scrubber treats a key by that
        # name as a secret and would redact the whole field, and the prefix is
        # the part of a key that is safe to record -- that is what a prefix is
        # for.
        detail={"kind": kind.value, "reason": reason, "prefix": credential},
        commit=True,
    )


async def lookup_credential(session: AsyncSession, token: str) -> CredentialLookup:
    """Resolve a machine token to its row, or raise ``CredentialError``.

    Every failure raises the *same* exception with the same message, so a caller
    cannot tell a malformed token from a revoked key from an unknown service
    account.
    """
    parsed = split_token(token)
    if parsed is None:
        raise CredentialError("Invalid credential")
    prefix, row_id, secret = parsed

    is_api_key = prefix == settings.api_key_prefix
    is_service_credential = prefix == settings.service_account_prefix
    if not (is_api_key or is_service_credential):
        raise CredentialError("Invalid credential")

    if is_api_key:
        key = await session.get(APIKey, row_id)
        if key is None or not identity_tokens.matches_hash(key.secret_hash, secret):
            raise CredentialError("Invalid credential")
        if key.revoked_at is not None:
            await _reject_credential(
                session,
                reason="api_key_revoked",
                tenant_id=key.tenant_id,
                kind=PrincipalKind.API_KEY,
                credential=key.prefix,
                actor_user_id=key.user_id,
            )
            raise CredentialError("Invalid credential")
        expires_at = _naive(key.expires_at)
        if expires_at is not None and expires_at <= _now():
            await _reject_credential(
                session,
                reason="api_key_expired",
                tenant_id=key.tenant_id,
                kind=PrincipalKind.API_KEY,
                credential=key.prefix,
                actor_user_id=key.user_id,
            )
            raise CredentialError("Invalid credential")

        kind = PrincipalKind.API_KEY
        display = key.name
        service_account = None
        actor_user_id = key.user_id
        if key.service_account_id is not None:
            service_account = await session.get(ServiceAccount, key.service_account_id)
            kind = PrincipalKind.SERVICE_ACCOUNT
            actor_user_id = None
        await _touch_last_used(session, key)
        return CredentialLookup(
            kind=kind,
            tenant_id=key.tenant_id,
            scopes=frozenset(str(s) for s in (key.scopes or [])),
            api_key=key,
            service_account=service_account,
            actor_user_id=actor_user_id,
            display_name=display,
            detail=key.prefix,
        )

    credential = await session.get(ServiceAccountCredential, row_id)
    if credential is None or not identity_tokens.matches_hash(credential.secret_hash, secret):
        raise CredentialError("Invalid credential")
    if credential.revoked_at is not None:
        await _reject_credential(
            session,
            reason="service_account_credential_revoked",
            tenant_id=credential.tenant_id,
            kind=PrincipalKind.SERVICE_ACCOUNT,
            credential=credential.prefix,
            actor_user_id=credential.created_by_user_id,
        )
        raise CredentialError("Invalid credential")
    expires_at = _naive(credential.expires_at)
    if expires_at is not None and expires_at <= _now():
        await _reject_credential(
            session,
            reason="service_account_credential_expired",
            tenant_id=credential.tenant_id,
            kind=PrincipalKind.SERVICE_ACCOUNT,
            credential=credential.prefix,
            actor_user_id=credential.created_by_user_id,
        )
        raise CredentialError("Invalid credential")

    account = await session.get(ServiceAccount, credential.service_account_id)
    if account is None or not account.enabled or account.emergency_disabled:
        await _reject_credential(
            session,
            reason="service_account_disabled",
            tenant_id=credential.tenant_id,
            kind=PrincipalKind.SERVICE_ACCOUNT,
            credential=credential.prefix,
            actor_user_id=credential.created_by_user_id,
        )
        raise CredentialError("Invalid credential")
    account_expiry = _naive(account.expires_at)
    if account_expiry is not None and account_expiry <= _now():
        await _reject_credential(
            session,
            reason="service_account_expired",
            tenant_id=credential.tenant_id,
            kind=PrincipalKind.SERVICE_ACCOUNT,
            credential=credential.prefix,
            actor_user_id=credential.created_by_user_id,
        )
        raise CredentialError("Invalid credential")

    await _touch_last_used(session, credential)
    return CredentialLookup(
        kind=PrincipalKind.SERVICE_ACCOUNT,
        tenant_id=account.tenant_id,
        scopes=frozenset(str(s) for s in (account.scopes or [])),
        service_account=account,
        service_account_credential=credential,
        actor_user_id=account.created_by_user_id,
        display_name=account.name,
        detail=credential.prefix,
    )


async def principal_from_lookup(
    session: AsyncSession, lookup: CredentialLookup, *, commit: bool = True
) -> AuthenticatedPrincipal:
    """Turn a resolved credential into a principal, applying tenant policy.

    Called by ``app.auth.dependencies.get_current_user`` for every request that
    presents a machine token. The tenant and the owner are re-read here rather
    than trusted from the token, exactly as they are for a JWT.
    """
    tenant = await session.get(Tenant, lookup.tenant_id)
    if tenant is None or not tenant.is_active:
        await _reject_credential(
            session,
            reason="tenant_inactive" if tenant is not None else "tenant_missing",
            tenant_id=tenant.id if tenant is not None else None,
            kind=lookup.kind,
            credential=lookup.detail,
            actor_user_id=lookup.actor_user_id,
        )
        raise CredentialError("Invalid credential")

    policy = await policies.load_policy(session, tenant.id)
    kind = "service_account" if lookup.kind is PrincipalKind.SERVICE_ACCOUNT else "api_key"
    decision = policies.evaluate_api_credential(policy, kind=kind)
    if not decision.allowed:
        # A tenant that switched a credential family off loses it immediately,
        # at the next request, rather than at the next expiration.
        await _reject_credential(
            session,
            reason=decision.reason,
            tenant_id=tenant.id,
            kind=lookup.kind,
            credential=lookup.detail,
            actor_user_id=lookup.actor_user_id,
        )
        raise PolicyDenied(decision.reason)

    if lookup.service_account is not None:
        account = lookup.service_account
        if not account.enabled or account.emergency_disabled:
            await _reject_credential(
                session,
                reason="service_account_disabled",
                tenant_id=tenant.id,
                kind=lookup.kind,
                credential=lookup.detail,
                actor_user_id=account.created_by_user_id,
            )
            raise CredentialError("Invalid credential")
        account_expiry = _naive(account.expires_at)
        if account_expiry is not None and account_expiry <= _now():
            await _reject_credential(
                session,
                reason="service_account_expired",
                tenant_id=tenant.id,
                kind=lookup.kind,
                credential=lookup.detail,
                actor_user_id=account.created_by_user_id,
            )
            raise CredentialError("Invalid credential")
        owner_id = account.created_by_user_id
    else:
        owner_id = lookup.actor_user_id

    if owner_id is None:
        # A service account whose creator has been deleted has no human to
        # attribute actions to and no account to re-check. Refusing is the only
        # safe answer: an operator reassigns the owner or disables the account.
        await _reject_credential(
            session,
            reason="credential_owner_missing",
            tenant_id=tenant.id,
            kind=lookup.kind,
            credential=lookup.detail,
        )
        raise CredentialError(
            "This credential has no owner. Reassign it to an active user."
        )
    owner = await session.get(User, owner_id)
    if owner is None or not owner.is_active or owner.tenant_id != tenant.id:
        await _reject_credential(
            session,
            reason="credential_owner_inactive",
            tenant_id=tenant.id,
            kind=lookup.kind,
            credential=lookup.detail,
            actor_user_id=owner_id,
        )
        raise CredentialError("Invalid credential")

    if commit:
        await session.commit()

    return AuthenticatedPrincipal(
        kind=lookup.kind,
        actor=owner,
        tenant_id=tenant.id,
        scopes=lookup.scopes,
        api_key_id=lookup.api_key.id if lookup.api_key else None,
        service_account_id=(
            lookup.service_account.id
            if lookup.service_account
            else (lookup.api_key.service_account_id if lookup.api_key else None)
        ),
        credential_id=(
            lookup.service_account_credential.id
            if lookup.service_account_credential
            else (lookup.api_key.id if lookup.api_key else None)
        ),
        display_name=lookup.display_name,
        metadata={"prefix": lookup.detail},
    )


async def authenticate_credential(
    session: AsyncSession, token: str
) -> AuthenticatedPrincipal:
    """Full path: resolve, apply policy, return a principal."""
    lookup = await lookup_credential(session, token)
    return await principal_from_lookup(session, lookup)


def scopes_from_permissions(permissions: list[Permission] | list[str]) -> list[str]:
    """Normalise a scope list. Accepts strings or ``Permission`` members."""
    out: list[str] = []
    for item in permissions:
        value = item.value if isinstance(item, Permission) else str(item)
        if value not in out:
            out.append(value)
    return out


def describe_scopes(scopes: list[str]) -> list[str]:
    """Filter a scope list to known permissions, for display."""
    return sorted({s for s in scopes if s in {p.value for p in Permission}})


async def keys_for_principal(
    session: AsyncSession, *, tenant_id: uuid.UUID, principal_user_id: uuid.UUID
) -> list[APIKey]:
    """The caller's own keys (not their service accounts')."""
    return await list_keys(session, tenant_id=tenant_id, owner_user_id=principal_user_id)


async def service_account_keys(
    session: AsyncSession, *, tenant_id: uuid.UUID, service_account_id: uuid.UUID
) -> list[APIKey]:
    return await list_keys(
        session, tenant_id=tenant_id, service_account_id=service_account_id
    )


def matches_scope(scope: str, permission: Permission) -> bool:
    return scope == permission.value


def known_scope_values() -> list[str]:
    return sorted(p.value for p in Permission)


__all__ = [
    "CredentialError",
    "CredentialLookup",
    "IssuedKey",
    "LAST_USED_WRITE_INTERVAL",
    "authenticate_credential",
    "build_token",
    "create_key",
    "describe_scopes",
    "get_key",
    "keys_for_principal",
    "known_scope_values",
    "list_keys",
    "lookup_credential",
    "matches_scope",
    "principal_from_lookup",
    "revoke_key",
    "rotate_key",
    "scopes_from_permissions",
    "service_account_keys",
    "split_token",
]
