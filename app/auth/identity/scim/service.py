"""SCIM 2.0 provisioning: credentials, users, groups.

A SCIM client is an IdP acting *on behalf of* a tenant, and it needs an
authorization model of its own:

* **Its own credential.** A SCIM bearer token is a row in
  ``scim_credentials``, stored as a digest, scoped to ``scim:users`` and
  ``scim:groups`` and to one tenant. It is not a user's session, not an API key,
  and never a super-admin token — an IdP integration that leaks its token can
  provision users and nothing else.
* **Every path is tenant-scoped by construction.** The credential resolves to a
  tenant; every query filters on that tenant; a resource id from another tenant
  produces a 404 identical to a resource that does not exist.
* **Deprovisioning means deactivation, not deletion.** ``active: false``
  deactivates the user, revokes their sessions and bumps their token version, so
  removed employees lose access immediately and their history stays attributable.
  Deletion is available, and is refused for the last active owner.
* **Group membership is a role.** Members of a group mapped in the connection's
  group mapping get that role; the mapping is validated at configuration time,
  so SCIM cannot grant ownership or a role the customer never chose.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import password as pw
from app.auth.identity import tokens as identity_tokens
from app.auth.identity.exceptions import (
    SCIMConflict,
    SCIMError,
    SCIMForbidden,
    SCIMInvalidValue,
    SCIMNotFound,
    SCIMUnauthorized,
)
from app.auth.identity.models import (
    SCIM_SCOPE_GROUPS,
    SCIM_SCOPE_USERS,
    SCIM_SCOPES,
    SCIMCredential,
    SCIMGroupMapping,
    SSOConnection,
    UserEmail,
)
from app.auth.identity.sso import claims as claim_rules
from app.auth.identity.sso import service as sso_service
from app.core.config import settings
from app.db.models import AuditAction, Tenant, User, UserRole

log = structlog.get_logger()

#: Don't rewrite `last_used_at` on every request from a chatty IdP.
LAST_USED_WRITE_INTERVAL = timedelta(seconds=60)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


def ensure_enabled() -> None:
    if not settings.scim_enabled:
        raise SCIMForbidden("SCIM provisioning is disabled on this deployment.")


# ============================================================== credentials ===


@dataclass(frozen=True)
class IssuedSCIMCredential:
    credential: SCIMCredential
    token: str


def build_scim_token(row_id: uuid.UUID) -> tuple[str, str, str]:
    """``(token, display_prefix, sha256)`` — same shape as every other key."""
    secret = identity_tokens.new_secret(32)
    prefix = f"{settings.scim_token_prefix}_{row_id.hex[:8]}"
    token = f"{settings.scim_token_prefix}_{row_id.hex}_{secret}"
    return token, prefix, identity_tokens.hash_token(secret)


async def create_credential(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: User,
    label: str = "",
    connection_id: uuid.UUID | None = None,
    scopes: list[str] | None = None,
    expires_in_days: int | None = None,
    commit: bool = False,
) -> IssuedSCIMCredential:
    """Issue a SCIM bearer token. Shown once, then only its digest exists."""
    ensure_enabled()
    if connection_id is not None:
        connection = await session.get(SSOConnection, connection_id)
        if connection is None or connection.tenant_id != tenant_id:
            raise SCIMInvalidValue("That SSO connection does not belong to this workspace.")

    chosen = [s for s in (scopes or list(SCIM_SCOPES)) if s in SCIM_SCOPES]
    if not chosen:
        raise SCIMInvalidValue(
            "A SCIM credential must carry at least one of: " + ", ".join(SCIM_SCOPES)
        )

    row_id = uuid.uuid4()
    token, prefix, digest = build_scim_token(row_id)
    credential = SCIMCredential(
        id=row_id,
        tenant_id=tenant_id,
        connection_id=connection_id,
        label=label[:120],
        token_prefix=prefix,
        token_hash=digest,
        scopes=chosen,
        created_by_user_id=actor.id,
        expires_at=(
            _now() + timedelta(days=max(1, int(expires_in_days))) if expires_in_days else None
        ),
    )
    session.add(credential)
    await session.flush()

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SCIM_CREDENTIAL_CREATED,
        tenant_id=tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "credential_id": str(credential.id),
            "prefix": prefix,
            "scopes": chosen,
            "connection_id": str(connection_id) if connection_id else "",
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return IssuedSCIMCredential(credential=credential, token=token)


async def rotate_credential(
    session: AsyncSession,
    credential: SCIMCredential,
    *,
    actor: User,
    commit: bool = False,
) -> IssuedSCIMCredential:
    """Replace and revoke in one transaction — no window with two live tokens.

    The revocation is a conditional update (``WHERE revoked_at IS NULL``) rather
    than a write to an object read earlier: two administrators rotating the same
    credential at the same moment would otherwise both see it live, and the
    workspace would end up with two live credentials, one of them absent from the
    administrator's list. Whoever wins the update issues the replacement; the
    loser is told the credential is gone.
    """
    credential_id = credential.id
    tenant_id = credential.tenant_id
    connection_id = credential.connection_id
    label = credential.label
    scopes = list(credential.scopes or [])
    expires_at = credential.expires_at

    retired = await session.execute(
        update(SCIMCredential)
        .where(SCIMCredential.id == credential_id, SCIMCredential.revoked_at.is_(None))
        .values(revoked_at=_now(), revoked_by_user_id=actor.id)
        .execution_options(synchronize_session=False)
    )
    if retired.rowcount == 0:
        await session.rollback()
        raise SCIMNotFound("This credential has already been rotated or revoked.")

    if credential in session:
    # The compare-and-set deliberately bypasses the identity map, so the
    # caller's own copy has to be brought back in line with the row: otherwise
    # the next statement in *this* session would still see a live credential.
        await session.refresh(credential)

    row_id = uuid.uuid4()
    token, prefix, digest = build_scim_token(row_id)
    replacement = SCIMCredential(
        id=row_id,
        tenant_id=tenant_id,
        connection_id=connection_id,
        label=label,
        token_prefix=prefix,
        token_hash=digest,
        scopes=scopes,
        created_by_user_id=actor.id,
        expires_at=expires_at,
        rotated_from_id=credential_id,
    )
    session.add(replacement)
    await session.flush()

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SCIM_CREDENTIAL_ROTATED,
        tenant_id=tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "previous_credential_id": str(credential_id),
            "new_credential_id": str(row_id),
            "prefix": prefix,
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return IssuedSCIMCredential(credential=replacement, token=token)


async def revoke_credential(
    session: AsyncSession,
    credential: SCIMCredential,
    *,
    actor: User,
    commit: bool = False,
) -> None:
    """Revoke a credential. Idempotent: revoking a revoked credential is a no-op.

    The update is conditional, so two simultaneous revocations cannot both write
    an audit entry — the second finds nothing live to revoke and does nothing,
    which is what a retrying administrator expects.
    """
    credential_id = credential.id
    tenant_id = credential.tenant_id
    prefix = credential.token_prefix

    revoked = await session.execute(
        update(SCIMCredential)
        .where(SCIMCredential.id == credential_id, SCIMCredential.revoked_at.is_(None))
        .values(revoked_at=_now(), revoked_by_user_id=actor.id)
        .execution_options(synchronize_session=False)
    )
    if revoked.rowcount == 0:
        if credential in session:
    # The compare-and-set deliberately bypasses the identity map, so the
    # caller's own copy has to be brought back in line with the row: otherwise
    # the next statement in *this* session would still see a live credential.
            await session.refresh(credential)
        if commit:
            await session.commit()
        return

    if credential in session:
    # The compare-and-set deliberately bypasses the identity map, so the
    # caller's own copy has to be brought back in line with the row: otherwise
    # the next statement in *this* session would still see a live credential.
        await session.refresh(credential)

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SCIM_CREDENTIAL_REVOKED,
        tenant_id=tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={"credential_id": str(credential_id), "prefix": prefix},
        commit=False,
    )
    if commit:
        await session.commit()


async def list_credentials(
    session: AsyncSession, *, tenant_id: uuid.UUID, include_revoked: bool = False
) -> list[SCIMCredential]:
    query = select(SCIMCredential).where(SCIMCredential.tenant_id == tenant_id)
    if not include_revoked:
        query = query.where(SCIMCredential.revoked_at.is_(None))
    return list((await session.execute(query.order_by(SCIMCredential.created_at.desc()))).scalars().all())


async def get_credential(
    session: AsyncSession, *, tenant_id: uuid.UUID, credential_id: uuid.UUID
) -> SCIMCredential | None:
    row = await session.get(SCIMCredential, credential_id)
    if row is None or row.tenant_id != tenant_id:
        return None
    return row


@dataclass(frozen=True)
class SCIMPrincipal:
    """Who is calling the SCIM endpoint, and what they may do."""

    credential_id: uuid.UUID
    tenant_id: uuid.UUID
    connection_id: uuid.UUID | None
    scopes: frozenset[str]

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise SCIMForbidden(f"This credential does not carry the {scope} scope.")


async def authenticate(session: AsyncSession, token: str) -> SCIMPrincipal:
    """Resolve a SCIM bearer token to its tenant.

    Every failure is the same 401 with the same body, so the endpoint cannot be
    used to test whether a token exists.
    """
    ensure_enabled()
    parts = (token or "").split("_", 2)
    if len(parts) != 3 or parts[0] != settings.scim_token_prefix:
        raise SCIMUnauthorized()
    try:
        row_id = uuid.UUID(hex=parts[1])
    except ValueError:
        raise SCIMUnauthorized() from None

    credential = await session.get(SCIMCredential, row_id)
    if credential is None or not identity_tokens.matches_hash(credential.token_hash, parts[2]):
        raise SCIMUnauthorized()
    if credential.revoked_at is not None:
        raise SCIMUnauthorized()
    expiry = _naive(credential.expires_at)
    if expiry is not None and expiry <= _now():
        raise SCIMUnauthorized()

    tenant = await session.get(Tenant, credential.tenant_id)
    if tenant is None or not tenant.is_active:
        raise SCIMUnauthorized()

    # The workspace's own switch, checked per request rather than at issuance:
    # an administrator who switches provisioning off means "now", not "when the
    # credential next expires". Refused as a 403 with a reason, because the
    # credential is valid and telling the IdP to fix its token would send the
    # administrator in the wrong direction.
    from app.auth.identity import policies as identity_policies

    policy = await identity_policies.load_policy(session, credential.tenant_id)
    if not policy.scim_enabled:
        raise SCIMForbidden("SCIM provisioning is switched off for this workspace.")

    now = _now()
    previous = _naive(credential.last_used_at)
    if previous is None or now - previous >= LAST_USED_WRITE_INTERVAL:
        credential.last_used_at = now
        await session.commit()

    return SCIMPrincipal(
        credential_id=credential.id,
        tenant_id=credential.tenant_id,
        connection_id=credential.connection_id,
        scopes=frozenset(str(s) for s in (credential.scopes or [])),
    )


# ==================================================================== users ===


@dataclass
class UserQuery:
    filter_text: str = ""
    start_index: int = 1
    count: int | None = None
    attributes: list[str] = field(default_factory=list)


def _user_columns():
    """The filterable attributes of a SCIM user, mapped to real columns."""
    from app.auth.identity.models import IdentityMapping

    return {
        "id": User.id,
        "userName": User.email,
        "displayName": User.full_name,
        "active": User.is_active,
        "meta.lastModified": User.updated_at,
        "meta.created": User.created_at,
        "externalId": IdentityMapping.external_subject,
    }


async def list_users(
    session: AsyncSession, principal: SCIMPrincipal, query: UserQuery
) -> tuple[list[dict], int]:
    from app.auth.identity.scim import filter as scim_filter, schemas

    principal.require(SCIM_SCOPE_USERS)
    per_page = _page_size(query.count)

    statement = select(User).where(User.tenant_id == principal.tenant_id)
    expression = scim_filter.parse(query.filter_text, allowed=scim_filter.USER_ATTRIBUTES)
    if expression is not None:
        columns = {k: v for k, v in _user_columns().items() if k != "externalId"}
        statement = statement.where(scim_filter.compile_expression(expression, columns))

    total = (
        await session.execute(
            select(func.count()).select_from(statement.subquery())
        )
    ).scalar_one()
    rows = (
        await session.execute(
            statement.order_by(User.created_at.asc())
            .offset(max(0, query.start_index - 1))
            .limit(per_page)
        )
    ).scalars().all()

    base = f"{settings.public_base_url.rstrip('/')}/scim/v2/{principal.connection_id or 'default'}"
    external_ids = await _external_ids_for(session, principal, [row.id for row in rows])
    resources = [
        schemas.user_resource(
            user=row,
            base_url=base,
            external_id=external_ids.get(row.id, ""),
            email_verified=row.email_verified_at is not None,
        )
        for row in rows
    ]
    return resources, int(total)


def _page_size(count: int | None) -> int:
    if count is None:
        return settings.scim_default_page_size
    if count <= 0:
        return 0
    return min(int(count), settings.scim_max_page_size)


async def get_user(
    session: AsyncSession, principal: SCIMPrincipal, user_id: uuid.UUID
) -> dict:
    from app.auth.identity.scim import schemas

    principal.require(SCIM_SCOPE_USERS)
    user = await session.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant_id:
        raise SCIMNotFound("User not found")
    base = f"{settings.public_base_url.rstrip('/')}/scim/v2/{principal.connection_id or 'default'}"
    external_ids = await _external_ids_for(session, principal, [user.id])
    return schemas.user_resource(
        user=user,
        base_url=base,
        external_id=external_ids.get(user.id, ""),
        email_verified=user.email_verified_at is not None,
        groups=await _groups_for(session, principal, user.id),
    )


async def _external_ids_for(
    session: AsyncSession, principal: SCIMPrincipal, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """``{user_id: externalId}`` for the users in play.

    ``externalId`` is the IdP's own handle for a person, and a client that cannot
    read back what it wrote cannot reconcile a directory: it would create the
    same account again under a new address. The value lives on the identity
    mapping, which is also where SSO looks for it, so both paths agree by
    construction.
    """
    from app.auth.identity.models import IdentityMapping

    if not user_ids:
        return {}
    rows = (
        await session.execute(
            select(IdentityMapping).where(
                IdentityMapping.tenant_id == principal.tenant_id,
                IdentityMapping.user_id.in_(user_ids),
            )
        )
    ).scalars().all()
    return {row.user_id: row.external_subject for row in rows if row.external_subject}


async def _groups_for(session: AsyncSession, principal: SCIMPrincipal, user_id: uuid.UUID) -> list[dict]:
    rows = (
        await session.execute(
            select(SCIMGroupMapping).where(SCIMGroupMapping.tenant_id == principal.tenant_id)
        )
    ).scalars().all()
    return [
        {"value": str(row.id), "display": row.display_name}
        for row in rows
        if str(user_id) in (row.member_user_ids or [])
    ]


@dataclass(frozen=True)
class UserPayload:
    user_name: str
    external_id: str = ""
    display_name: str = ""
    active: bool | None = None
    email_verified: bool | None = None


def parse_user_payload(body: dict, *, required: bool) -> UserPayload:
    """Read a SCIM User payload into the fields we honour.

    ``userName`` is required on create (RFC 7643 §4.1.1) and is the address the
    account logs in with. Anything we do not support is ignored rather than
    rejected — an IdP sends a lot of attributes, and refusing a payload because
    it mentions ``nickName`` would make the integration unusable.
    """
    user_name = str(body.get("userName") or "").strip().lower()
    if required and not user_name:
        raise SCIMInvalidValue("userName is required")
    if user_name and ("@" not in user_name or len(user_name) > 320):
        # RFC 7643 allows any string; this product's accounts are addresses, and
        # a value that cannot be an address would create a user who can never
        # authenticate.
        raise SCIMInvalidValue("userName must be an email address")

    emails = body.get("emails")
    verified = None
    if isinstance(emails, list):
        for entry in emails:
            if isinstance(entry, dict) and entry.get("primary"):
                if "verified" in entry:
                    verified = bool(entry["verified"])
    if "verified" in body:
        verified = bool(body["verified"])

    display = str(body.get("displayName") or "").strip()
    if not display:
        name = body.get("name")
        if isinstance(name, dict):
            display = str(name.get("formatted") or "").strip()

    return UserPayload(
        user_name=user_name,
        external_id=str(body.get("externalId") or "").strip(),
        display_name=display,
        active=None if body.get("active") is None else bool(body.get("active")),
        email_verified=verified,
    )


async def create_user(
    session: AsyncSession, principal: SCIMPrincipal, payload: UserPayload
) -> dict:
    from app.auth.identity.scim import schemas

    principal.require(SCIM_SCOPE_USERS)
    existing = (
        await session.execute(
            select(User).where(
                User.email == payload.user_name, User.tenant_id == principal.tenant_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # RFC 7644 §3.3: creating a resource that already exists is a 409 with
        # scimType uniqueness. Idempotent clients handle this by fetching it.
        raise SCIMConflict("A user with that userName already exists")

    taken_elsewhere = (
        await session.execute(select(User).where(User.email == payload.user_name))
    ).scalar_one_or_none()
    if taken_elsewhere is not None:
        raise SCIMConflict(
            "That address belongs to a user in another workspace and cannot be "
            "provisioned here."
        )

    role = await _default_role(session, principal)
    user = User(
        tenant_id=principal.tenant_id,
        email=payload.user_name,
        full_name=(payload.display_name or payload.user_name.split("@", 1)[0])[:120],
        password_hash=pw.hash_password(identity_tokens.new_secret(32)),
        role=role,
        is_active=True if payload.active is None else bool(payload.active),
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        # Two syncs racing for the same address: the unique constraint is the
        # arbiter, and the loser must look like the ordinary duplicate case
        # rather than a 500. The transaction has to be discarded first, because
        # the insert is what failed.
        await session.rollback()
        raise SCIMConflict("A user with that userName already exists") from None

    if payload.email_verified:
        user.email_verified_at = _now()
    session.add(
        UserEmail(
            tenant_id=principal.tenant_id,
            user_id=user.id,
            email=payload.user_name,
            is_primary=True,
            verified_at=user.email_verified_at,
            source="scim",
        )
    )

    await _record_external_id(session, principal, user=user, external_id=payload.external_id)

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SCIM_USER_PROVISIONED,
        tenant_id=principal.tenant_id,
        actor_user_id=None,
        target_user_id=user.id,
        actor_email=payload.user_name,
        detail={
            "user_id": str(user.id),
            "credential_id": str(principal.credential_id),
            "role": user.role.value,
            "active": user.is_active,
        },
        commit=False,
    )
    await session.commit()

    base = f"{settings.public_base_url.rstrip('/')}/scim/v2/{principal.connection_id or 'default'}"
    return schemas.user_resource(user=user, base_url=base, external_id=payload.external_id)


async def _default_role(session: AsyncSession, principal: SCIMPrincipal) -> UserRole:
    """The role a SCIM-provisioned user gets.

    The connection's ``default_role`` when the credential names one (so SCIM and
    SSO agree), otherwise viewer. Never owner, and never more than the connection
    allows.
    """
    if principal.connection_id is not None:
        connection = await session.get(SSOConnection, principal.connection_id)
        if connection is not None and connection.tenant_id == principal.tenant_id:
            try:
                role = claim_rules._parse_role(connection.default_role)
            except Exception:  # noqa: BLE001
                role = UserRole.VIEWER
            return role
    return UserRole.VIEWER


async def _record_external_id(
    session: AsyncSession, principal: SCIMPrincipal, *, user: User, external_id: str
) -> None:
    """Keep ``externalId`` where SSO can find it again.

    Stored as the mapping's external subject so a user provisioned by SCIM and
    later signed in through SSO resolves to the same account.
    """
    if not external_id or principal.connection_id is None:
        return
    from app.auth.identity.models import IdentityMapping, MappingOrigin

    existing = (
        await session.execute(
            select(IdentityMapping).where(
                IdentityMapping.connection_id == principal.connection_id,
                IdentityMapping.external_subject == external_id[:255],
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    session.add(
        IdentityMapping(
            tenant_id=principal.tenant_id,
            connection_id=principal.connection_id,
            user_id=user.id,
            external_subject=external_id[:255],
            external_email=user.email[:320],
            display_name=user.full_name[:200],
            external_groups=[],
            mapped_role=user.role.value,
            created_via=MappingOrigin.SCIM.value,
        )
    )


async def replace_user(
    session: AsyncSession, principal: SCIMPrincipal, user_id: uuid.UUID, payload: UserPayload
) -> dict:
    """``PUT``: the payload is authoritative for the fields we hold."""
    from app.auth.identity.scim import schemas

    principal.require(SCIM_SCOPE_USERS)
    user = await session.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant_id:
        raise SCIMNotFound("User not found")

    if payload.user_name and payload.user_name != user.email:
        clash = (
            await session.execute(select(User).where(User.email == payload.user_name))
        ).scalar_one_or_none()
        if clash is not None:
            raise SCIMConflict("A user with that userName already exists")
        user.email = payload.user_name
    if payload.display_name:
        user.full_name = payload.display_name[:120]

    if payload.active is not None and payload.active != user.is_active:
        await set_active(session, principal, user, active=payload.active)
    if payload.external_id:
        await _record_external_id(session, principal, user=user, external_id=payload.external_id)

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SCIM_USER_UPDATED,
        tenant_id=principal.tenant_id,
        target_user_id=user.id,
        detail={"user_id": str(user.id), "operation": "put"},
        commit=False,
    )
    await session.commit()

    base = f"{settings.public_base_url.rstrip('/')}/scim/v2/{principal.connection_id or 'default'}"
    external_ids = await _external_ids_for(session, principal, [user.id])
    return schemas.user_resource(
        user=user,
        base_url=base,
        external_id=external_ids.get(user.id, "") or payload.external_id,
        email_verified=user.email_verified_at is not None,
    )


async def set_active(
    session: AsyncSession, principal: SCIMPrincipal, user: User, *, active: bool
) -> User:
    """Deactivate (or reactivate) a user, revoking their access immediately.

    Deactivation revokes every session and bumps ``token_version``, so an
    employee removed in the IdP is removed here within one request rather than at
    the next token expiry.
    """
    principal.require(SCIM_SCOPE_USERS)
    if user.tenant_id != principal.tenant_id:
        raise SCIMNotFound("User not found")

    if not active:
        from app.auth.identity import sessions as identity_sessions
        from app.auth.service import revoke_all_for_user

        active_owners = (
            await session.execute(
                select(func.count(User.id)).where(
                    User.tenant_id == user.tenant_id,
                    User.role == UserRole.OWNER,
                    User.is_active.is_(True),
                )
            )
        ).scalar_one()
        if user.role is UserRole.OWNER and user.is_active and active_owners <= 1:
            raise SCIMForbidden("The last active owner cannot be deprovisioned.")

        user.is_active = False
        user.token_version += 1
        await revoke_all_for_user(session, user.id, bump_version=False)
        await identity_sessions.revoke_all_for_user(
            session, user_id=user.id, reason="scim_deprovisioned", commit=False
        )
        await session.commit()

        from app.auth.identity.events import emit

        await emit(
            session,
            AuditAction.SCIM_USER_DEPROVISIONED,
            tenant_id=user.tenant_id,
            target_user_id=user.id,
            actor_email=user.email,
            detail={"user_id": str(user.id), "credential_id": str(principal.credential_id)},
            commit=False,
        )
        await session.commit()
    else:
        user.is_active = True
        await session.commit()

        from app.auth.identity.events import emit

        await emit(
            session,
            AuditAction.SCIM_USER_UPDATED,
            tenant_id=user.tenant_id,
            target_user_id=user.id,
            actor_email=user.email,
            detail={"user_id": str(user.id), "operation": "reactivate"},
            commit=False,
        )
        await session.commit()

    return user


async def delete_user(session: AsyncSession, principal: SCIMPrincipal, user_id: uuid.UUID) -> None:
    """Hard delete, which deprovisions first so nothing can be orphaned."""
    principal.require(SCIM_SCOPE_USERS)
    user = await session.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant_id:
        raise SCIMNotFound("User not found")

    if user.is_active:
        await set_active(session, principal, user, active=False)

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SCIM_USER_DEPROVISIONED,
        tenant_id=user.tenant_id,
        target_user_id=user.id,
        actor_email=user.email,
        detail={"user_id": str(user.id), "operation": "delete"},
        commit=False,
    )
    await session.delete(user)
    await session.commit()


# =================================================================== groups ===


async def list_groups(
    session: AsyncSession, principal: SCIMPrincipal, query: UserQuery
) -> tuple[list[dict], int]:
    from app.auth.identity.scim import filter as scim_filter, schemas

    principal.require(SCIM_SCOPE_GROUPS)
    per_page = _page_size(query.count)
    statement = select(SCIMGroupMapping).where(
        SCIMGroupMapping.tenant_id == principal.tenant_id
    )
    expression = scim_filter.parse(query.filter_text, allowed=scim_filter.GROUP_ATTRIBUTES)
    if expression is not None:
        columns = {
            "id": SCIMGroupMapping.id,
            "displayName": SCIMGroupMapping.display_name,
            "externalId": SCIMGroupMapping.external_id,
            "meta.lastModified": SCIMGroupMapping.updated_at,
            "meta.created": SCIMGroupMapping.created_at,
        }
        statement = statement.where(scim_filter.compile_expression(expression, columns))

    total = (await session.execute(select(func.count()).select_from(statement.subquery()))).scalar_one()
    rows = (
        await session.execute(
            statement.order_by(SCIMGroupMapping.created_at.asc())
            .offset(max(0, query.start_index - 1))
            .limit(per_page)
        )
    ).scalars().all()

    base = f"{settings.public_base_url.rstrip('/')}/scim/v2/{principal.connection_id or 'default'}"
    return [
        schemas.group_resource(group=row, base_url=base, mapped_role=row.mapped_role)
        for row in rows
    ], int(total)


async def get_group(session: AsyncSession, principal: SCIMPrincipal, group_id: uuid.UUID) -> dict:
    from app.auth.identity.scim import schemas

    principal.require(SCIM_SCOPE_GROUPS)
    group = await session.get(SCIMGroupMapping, group_id)
    if group is None or group.tenant_id != principal.tenant_id:
        raise SCIMNotFound("Group not found")
    base = f"{settings.public_base_url.rstrip('/')}/scim/v2/{principal.connection_id or 'default'}"
    return schemas.group_resource(
        group=group, base_url=base, members=await _members_for(session, group),
        mapped_role=group.mapped_role,
    )


async def _members_for(session: AsyncSession, group: SCIMGroupMapping) -> list[dict]:
    ids = [str(m) for m in (group.member_user_ids or [])]
    if not ids:
        return []
    rows = (
        await session.execute(
            select(User).where(User.tenant_id == group.tenant_id, User.id.in_(
                [uuid.UUID(i) for i in ids if _is_uuid(i)]
            ))
        )
    ).scalars().all()
    return [{"value": str(u.id), "display": u.full_name or u.email} for u in rows]


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError):
        return False
    return True


@dataclass(frozen=True)
class GroupPayload:
    display_name: str
    external_id: str = ""
    member_ids: list[uuid.UUID] = field(default_factory=list)


def parse_group_payload(body: dict, *, required: bool) -> GroupPayload:
    display = str(body.get("displayName") or "").strip()
    if required and not display:
        raise SCIMInvalidValue("displayName is required")
    members: list[uuid.UUID] = []
    raw = body.get("members")
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            value = str(entry.get("value") or "").strip()
            if not value:
                continue
            if not _is_uuid(value):
                raise SCIMInvalidValue(f"Group member {value!r} is not a valid user id.")
            members.append(uuid.UUID(value))
    return GroupPayload(
        display_name=display[:200],
        external_id=str(body.get("externalId") or "").strip()[:255],
        member_ids=members,
    )


async def _role_for_group(session: AsyncSession, principal: SCIMPrincipal, name: str) -> str:
    """The role this group grants, from the connection's group mapping."""
    if principal.connection_id is None:
        return ""
    connection = await session.get(SSOConnection, principal.connection_id)
    if connection is None or connection.tenant_id != principal.tenant_id:
        return ""
    mapping = dict(connection.group_mapping or {})
    return str(mapping.get(name, ""))[:32]


async def _sync_group_roles(
    session: AsyncSession, principal: SCIMPrincipal, group: SCIMGroupMapping
) -> None:
    """Apply a group's role to its members.

    Each affected member is raised to the **highest** role any of their groups
    grants, so a sync of one group can never demote someone below another group
    they are in — an IdP that syncs groups one at a time would otherwise knock a
    manager down to whatever the last-synced group maps to.

    Two things this deliberately does *not* do. It never lowers a role: a role
    that came from a claims mapping or from an administrator is not revoked by a
    directory sync; deprovisioning is what revokes access. And it never grants a
    role the connection's mapping does not name — an owner is left alone, and a
    role outside ``ASSIGNABLE_VIA_SSO`` is ignored rather than applied.
    """
    from app.auth import rbac

    targets = {uuid.UUID(str(m)) for m in (group.member_user_ids or []) if _is_uuid(str(m))}
    if not targets:
        return

    # Every group in the workspace that grants a role *and* names one of the
    # members in play. Limited to the tenant, so a group id from elsewhere can
    # never contribute a role here.
    rows = (
        await session.execute(
            select(SCIMGroupMapping).where(SCIMGroupMapping.tenant_id == principal.tenant_id)
        )
    ).scalars().all()

    granted: dict[uuid.UUID, UserRole] = {}
    for row in rows:
        if not row.mapped_role:
            continue
        try:
            candidate = UserRole(row.mapped_role)
        except ValueError:
            continue
        if candidate not in claim_rules.ASSIGNABLE_VIA_SSO:
            continue
        for member in row.member_user_ids or []:
            if not _is_uuid(str(member)):
                continue
            user_id = uuid.UUID(str(member))
            if user_id not in targets:
                continue
            best = granted.get(user_id)
            if best is None or rbac.role_level(candidate) > rbac.role_level(best):
                granted[user_id] = candidate

    if not granted:
        return

    members = (
        await session.execute(
            select(User).where(User.tenant_id == principal.tenant_id, User.id.in_(list(granted)))
        )
    ).scalars().all()
    for user in members:
        if user.role is UserRole.OWNER:
            continue
        target = granted[user.id]
        if rbac.role_level(target) > rbac.role_level(user.role):
            user.role = target


async def upsert_group(
    session: AsyncSession,
    principal: SCIMPrincipal,
    payload: GroupPayload,
    *,
    group_id: uuid.UUID | None = None,
) -> dict:
    """Create (``POST``) or replace (``PUT``) a group."""
    from app.auth.identity.scim import schemas

    principal.require(SCIM_SCOPE_GROUPS)
    if group_id is not None:
        group = await session.get(SCIMGroupMapping, group_id)
        if group is None or group.tenant_id != principal.tenant_id:
            raise SCIMNotFound("Group not found")
        created = False
    else:
        if principal.connection_id is None:
            raise SCIMInvalidValue(
                "SCIM credentials must be scoped to an SSO connection to manage groups."
            )
        existing = (
            await session.execute(
                select(SCIMGroupMapping).where(
                    SCIMGroupMapping.connection_id == principal.connection_id,
                    SCIMGroupMapping.display_name == payload.display_name,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            group = existing
            created = False
        else:
            group = SCIMGroupMapping(
                tenant_id=principal.tenant_id,
                connection_id=principal.connection_id,
                external_id=(payload.external_id or payload.display_name)[:255],
                display_name=payload.display_name,
                member_user_ids=[],
                mapped_role="",
            )
            session.add(group)
            await session.flush()
            created = True

    group.display_name = payload.display_name or group.display_name
    if payload.external_id:
        group.external_id = payload.external_id
    group.member_user_ids = [str(m) for m in payload.member_ids]
    group.mapped_role = await _role_for_group(session, principal, group.display_name)
    group.last_sync_at = _now()
    await _sync_group_roles(session, principal, group)

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SCIM_GROUP_CREATED if created else AuditAction.SCIM_GROUP_UPDATED,
        tenant_id=principal.tenant_id,
        detail={
            "group_id": str(group.id),
            "display_name": group.display_name,
            "members": len(group.member_user_ids or []),
            "mapped_role": group.mapped_role,
            "credential_id": str(principal.credential_id),
        },
        commit=False,
    )
    await session.commit()

    base = f"{settings.public_base_url.rstrip('/')}/scim/v2/{principal.connection_id or 'default'}"
    return schemas.group_resource(
        group=group, base_url=base, members=await _members_for(session, group),
        mapped_role=group.mapped_role,
    )


async def delete_group(session: AsyncSession, principal: SCIMPrincipal, group_id: uuid.UUID) -> None:
    """Remove a group. Members keep their role: membership removal through SCIM
    is a provisioning action, and silently demoting people because a group was
    renamed would be an unwelcome surprise."""
    principal.require(SCIM_SCOPE_GROUPS)
    group = await session.get(SCIMGroupMapping, group_id)
    if group is None or group.tenant_id != principal.tenant_id:
        raise SCIMNotFound("Group not found")

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SCIM_GROUP_DELETED,
        tenant_id=principal.tenant_id,
        detail={"group_id": str(group.id), "display_name": group.display_name},
        commit=False,
    )
    await session.delete(group)
    await session.commit()


async def patch_group(
    session: AsyncSession,
    principal: SCIMPrincipal,
    group_id: uuid.UUID,
    operations: list[dict],
) -> dict:
    """Apply RFC 7644 §3.5.2 PATCH operations to a group's membership."""
    principal.require(SCIM_SCOPE_GROUPS)
    group = await session.get(SCIMGroupMapping, group_id)
    if group is None or group.tenant_id != principal.tenant_id:
        raise SCIMNotFound("Group not found")

    members = list(group.member_user_ids or [])
    for operation in operations:
        op = str(operation.get("op") or "").lower()
        path = str(operation.get("path") or "")
        value = operation.get("value")
        if op in ("add", "replace") and path.lower() in ("members", ""):
            for entry in value or []:
                if isinstance(entry, dict) and entry.get("value"):
                    identifier = str(entry["value"])
                    if not _is_uuid(identifier):
                        raise SCIMInvalidValue(f"Group member {identifier!r} is not a valid user id.")
                    if identifier not in members:
                        members.append(identifier)
            if op == "replace":
                group.display_name = str(
                    operation.get("displayName") or group.display_name
                )[:200]
        elif op in ("remove", "delete") and path.lower() in ("members", ""):
            targets = set()
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict) and entry.get("value"):
                        targets.add(str(entry["value"]))
            elif isinstance(value, str):
                targets.add(value)
            elif path.count("[") and "=" in path:
                candidate = path.split("=", 1)[1].strip().strip('"')
                targets.add(candidate)
            members = [m for m in members if m not in targets]
        elif op == "replace" and path.lower() == "displayname":
            group.display_name = str(value)[:200]
        else:
            raise SCIMInvalidValue(f"Unsupported PATCH operation: {op} {path}")

    group.member_user_ids = members
    group.mapped_role = await _role_for_group(session, principal, group.display_name)
    group.last_sync_at = _now()
    await _sync_group_roles(session, principal, group)

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SCIM_GROUP_UPDATED,
        tenant_id=principal.tenant_id,
        detail={
            "group_id": str(group.id),
            "members": len(members),
            "operation": "patch",
            "mapped_role": group.mapped_role,
        },
        commit=False,
    )
    await session.commit()

    from app.auth.identity.scim import schemas

    base = f"{settings.public_base_url.rstrip('/')}/scim/v2/{principal.connection_id or 'default'}"
    return schemas.group_resource(
        group=group, base_url=base, members=await _members_for(session, group),
        mapped_role=group.mapped_role,
    )


async def patch_user(
    session: AsyncSession,
    principal: SCIMPrincipal,
    user_id: uuid.UUID,
    operations: list[dict],
) -> dict:
    """RFC 7644 §3.5.2 PATCH for a user: ``active``, names, ``externalId``."""
    from app.auth.identity.scim import schemas

    principal.require(SCIM_SCOPE_USERS)
    user = await session.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant_id:
        raise SCIMNotFound("User not found")

    for operation in operations:
        op = str(operation.get("op") or "").lower()
        path = str(operation.get("path") or "")
        value = operation.get("value")

        if isinstance(value, dict) and not path:
            for key, item in value.items():
                if str(key).lower() == "externalid":
                    await _record_external_id(
                        session, principal, user=user, external_id=str(item or "")
                    )
                    continue
                _apply_user_field(user, str(key), item)
            continue
        if path.lower() == "externalid":
            await _record_external_id(
                session, principal, user=user, external_id=str(value or "")
            )
            continue
        if op == "remove":
            raise SCIMInvalidValue(
                "Removing user attributes is not supported; deactivate the user instead."
            )
        if not path:
            raise SCIMInvalidValue("A PATCH operation must name a path.")
        _apply_user_field(user, path, value)

    if not user.is_active:
        await set_active(session, principal, user, active=False)

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.SCIM_USER_UPDATED,
        tenant_id=principal.tenant_id,
        target_user_id=user.id,
        detail={"user_id": str(user.id), "operation": "patch"},
        commit=False,
    )
    await session.commit()

    base = f"{settings.public_base_url.rstrip('/')}/scim/v2/{principal.connection_id or 'default'}"
    external_ids = await _external_ids_for(session, principal, [user.id])
    return schemas.user_resource(
        user=user,
        base_url=base,
        external_id=external_ids.get(user.id, ""),
        email_verified=user.email_verified_at is not None,
        groups=await _groups_for(session, principal, user.id),
    )


def _apply_user_field(user: User, path: str, value) -> None:
    """One PATCH path → one column. Unknown paths are refused, not ignored."""
    lowered = path.lower()
    if lowered in ("active",):
        user.is_active = bool(value)
    elif lowered in ("displayname", "name.formatted"):
        user.full_name = str(value or "")[:120]
    elif lowered == "username":
        candidate = str(value or "").strip().lower()
        if "@" not in candidate:
            raise SCIMInvalidValue("userName must be an email address")
        user.email = candidate[:320]
    elif lowered in ("emails", "emails.value"):
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict) and entry.get("primary") and entry.get("value"):
                    user.email = str(entry["value"]).strip().lower()[:320]
        elif value:
            user.email = str(value).strip().lower()[:320]
    elif lowered == "externalid":
        # Handled by the caller, which has the session needed to write it onto
        # the identity mapping; reaching here means a PATCH that carried a path
        # this function cannot honour on its own.
        raise SCIMInvalidValue("Patching externalId requires a connection-scoped update.")
    else:
        raise SCIMInvalidValue(f"Patching {path!r} is not supported.")


async def user_count(session: AsyncSession, *, tenant_id: uuid.UUID) -> int:
    return (
        await session.execute(select(func.count(User.id)).where(User.tenant_id == tenant_id))
    ).scalar_one()


def error_status(exc: SCIMError) -> int:
    return exc.status or 400


def scim_base_url(connection_id: uuid.UUID | None) -> str:
    return sso_service.base_url() + f"/scim/v2/{connection_id or 'default'}"
