"""Service accounts: machine identities and their credentials.

A service account is a *principal*, not a token: it holds scopes, it has an
owner, it can be disabled, and it can be stopped in an emergency. The routes
here manage that life cycle plus the two kinds of secret it can carry — a
long-lived credential and an individually revocable named key.

Route table:

==============================================  ===============================
Route                                            Guard
==============================================  ===============================
GET    /api/service-accounts                     ``service_account:manage``
POST   /api/service-accounts                     ``+ fresh reauth``
GET    /api/service-accounts/{id}                ``service_account:manage``
PATCH  /api/service-accounts/{id}                ``+ fresh reauth``
POST   /api/service-accounts/{id}/enable         ``+ fresh reauth``
POST   /api/service-accounts/{id}/disable        ``+ fresh reauth``
POST   /api/service-accounts/{id}/emergency-disable
POST   /api/service-accounts/{id}/emergency-clear  **owner only** + reason
DELETE /api/service-accounts/{id}                ``+ fresh reauth``
GET    /api/service-accounts/{id}/credentials    ``service_account:manage``
POST   /api/service-accounts/{id}/credentials    ``+ fresh reauth``
POST   /api/service-accounts/{id}/credentials/{cid}/rotate
DELETE /api/service-accounts/{id}/credentials/{cid}
GET    /api/service-accounts/{id}/keys           ``service_account:manage``
POST   /api/service-accounts/{id}/keys           ``+ fresh reauth``
==============================================  ===============================

The emergency controls are deliberately asymmetric: tripping the stop is cheap
and any administrator can do it, while lifting it needs an **owner** and a
written reason — the action that puts a possibly-compromised credential back
into service is the one that deserves the signature. ``/enable`` keeps refusing
while a stop is in force, so the customer-facing toggle cannot undo an operator
decision by accident.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.api_key_routes import ApiKeyCreatedOut, ApiKeyIn, ApiKeyOut
from app.api.identity_errors import translate
from app.api.machine_guards import guard
from app.auth.dependencies import get_identity_context, require_permission
from app.auth.dependencies import TenantContext
from app.auth.identity import api_keys as key_service
from app.auth.identity import service_accounts as sa_service
from app.auth.identity.exceptions import IdentityError
from app.auth.identity.service import IdentityContext
from app.auth.permissions import Permission
from app.db.models import UserRole
from app.db.session import get_session

log = structlog.get_logger()
#: ``/api`` prefix, matching every other route module in the repository.
router = APIRouter(prefix="/api", tags=["identity"])


class ServiceAccountOut(BaseModel):
    id: str
    name: str
    description: str = ""
    scopes: list[str]
    scope_summary: str = ""
    enabled: bool
    emergency_disabled: bool = False
    disabled_reason: str = ""
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime | None = None
    owner_user_id: str | None = None
    credential_count: int = 0

    @classmethod
    def of(cls, row, *, credential_count: int = 0) -> ServiceAccountOut:
        scopes = [str(s) for s in (row.scopes or [])]
        return cls(
            id=str(row.id),
            name=row.name,
            description=row.description or "",
            scopes=scopes,
            scope_summary=sa_service.scope_summary(scopes),
            enabled=bool(row.enabled),
            emergency_disabled=bool(row.emergency_disabled),
            disabled_reason=row.disabled_reason or "",
            expires_at=row.expires_at,
            last_used_at=row.last_used_at,
            created_at=row.created_at,
            owner_user_id=str(row.created_by_user_id) if row.created_by_user_id else None,
            credential_count=credential_count,
        )


class ServiceAccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=300)
    scopes: list[str] = Field(default_factory=list)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ServiceAccountUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=300)
    scopes: list[str] | None = None
    owner_user_id: uuid.UUID | None = None
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class CredentialOut(BaseModel):
    id: str
    label: str = ""
    prefix: str
    created_at: datetime | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str = ""

    @classmethod
    def of(cls, row) -> CredentialOut:
        return cls(
            id=str(row.id),
            label=row.label or "",
            prefix=row.prefix,
            created_at=row.created_at,
            expires_at=row.expires_at,
            last_used_at=row.last_used_at,
            revoked_at=row.revoked_at,
            revoked_reason=getattr(row, "revoked_reason", "") or "",
        )


class CredentialCreatedOut(CredentialOut):
    secret: str
    warning: str = (
        "Copy this credential now. It is stored as a hash and cannot be shown again."
    )


class CredentialIn(BaseModel):
    label: str = Field(default="", max_length=120)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ToggleIn(BaseModel):
    reason: str = Field(default="", max_length=200)


class EmergencyClearIn(BaseModel):
    #: Why the stop is being lifted. Required, and deliberately not defaulted:
    #: "somebody re-enabled it" is not an answer an incident review accepts.
    reason: str = Field(min_length=8, max_length=200)


@router.get("/service-accounts", response_model=list[ServiceAccountOut])
async def list_service_accounts(
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
):
    rows = await sa_service.list_accounts(session, tenant_id=ctx.tenant_id)
    out = []
    for row in rows:
        credentials = await sa_service.list_credentials(session, account_id=row.id)
        out.append(ServiceAccountOut.of(row, credential_count=len(credentials)))
    return out


@router.post("/service-accounts", response_model=ServiceAccountOut, status_code=201)
async def create_service_account(
    payload: ServiceAccountIn,
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    """Create a machine identity. It gets no credential until one is issued."""
    await guard(session, ictx, "service_account.create")
    try:
        scopes = await sa_service.validate_scopes(
            session, actor=ictx.user, scopes=payload.scopes
        )
        account = await sa_service.create_account(
            session,
            tenant_id=ctx.tenant_id,
            actor=ctx.user,
            name=payload.name,
            description=payload.description,
            scopes=scopes,
            expires_in_days=payload.expires_in_days,
            commit=False,
        )
    except IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    await session.commit()
    return ServiceAccountOut.of(account)


async def _owned_account(session: AsyncSession, ctx: TenantContext, account_id: uuid.UUID):
    account = await sa_service.get_account(
        session, tenant_id=ctx.tenant_id, account_id=account_id
    )
    if account is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Not found"})
    return account


@router.get("/service-accounts/{account_id}", response_model=ServiceAccountOut)
async def get_service_account(
    account_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
):
    account = await _owned_account(session, ctx, account_id)
    credentials = await sa_service.list_credentials(session, account_id=account.id)
    return ServiceAccountOut.of(account, credential_count=len(credentials))


@router.patch("/service-accounts/{account_id}", response_model=ServiceAccountOut)
async def update_service_account(
    account_id: uuid.UUID,
    payload: ServiceAccountUpdateIn,
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    await guard(session, ictx, "service_account.update")
    account = await _owned_account(session, ctx, account_id)
    scopes = None
    if payload.scopes is not None:
        try:
            scopes = await sa_service.validate_scopes(
                session, actor=ictx.user, scopes=payload.scopes
            )
        except IdentityError as exc:
            raise translate(exc) from None
    try:
        await sa_service.update_account(
            session,
            account,
            actor=ctx.user,
            name=payload.name,
            description=payload.description,
            scopes=scopes,
            owner_user_id=payload.owner_user_id,
            expires_in_days=payload.expires_in_days,
            commit=True,
        )
    except IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    return ServiceAccountOut.of(account)


@router.post("/service-accounts/{account_id}/enable", response_model=ServiceAccountOut)
async def enable_service_account(
    account_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    await guard(session, ictx, "service_account.enable")
    account = await _owned_account(session, ctx, account_id)
    try:
        await sa_service.set_enabled(
            session, account, actor=ctx.user, enabled=True, commit=True
        )
    except IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    return ServiceAccountOut.of(account)


@router.post("/service-accounts/{account_id}/disable", response_model=ServiceAccountOut)
async def disable_service_account(
    account_id: uuid.UUID,
    payload: ToggleIn,
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    """The customer-facing stop switch. Reversible; credentials survive."""
    await guard(session, ictx, "service_account.disable")
    account = await _owned_account(session, ctx, account_id)
    await sa_service.set_enabled(
        session, account, actor=ctx.user, enabled=False, reason=payload.reason, commit=True
    )
    return ServiceAccountOut.of(account)


@router.post(
    "/service-accounts/{account_id}/emergency-disable", response_model=ServiceAccountOut
)
async def emergency_disable_service_account(
    account_id: uuid.UUID,
    payload: ToggleIn,
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    """The stop button. It is one-way at this level: clearing it needs an owner."""
    await guard(session, ictx, "service_account.emergency_disable")
    account = await _owned_account(session, ctx, account_id)
    await sa_service.set_enabled(
        session,
        account,
        actor=ctx.user,
        enabled=False,
        reason=payload.reason or "emergency_stop",
        emergency=True,
        commit=True,
    )
    return ServiceAccountOut.of(account)


@router.post("/service-accounts/{account_id}/emergency-clear", response_model=ServiceAccountOut)
async def clear_emergency_disable(
    account_id: uuid.UUID,
    payload: EmergencyClearIn,
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    """Lift an emergency stop.

    Tripping the stop is cheap and any administrator can do it; lifting it is
    the action that could put a compromised credential back into service, so it
    is restricted to an **owner** and requires a reason. ``POST
    /api/service-accounts/{id}/enable`` still refuses while the stop is in
    place, so the customer-facing toggle cannot undo an operator decision by
    accident.
    """
    if ctx.user.role is not UserRole.OWNER:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "owner_required",
                "message": "Only a tenant owner may lift an emergency disable.",
            },
        )
    await guard(session, ictx, "service_account.emergency_clear")
    account = await _owned_account(session, ctx, account_id)
    try:
        await sa_service.clear_emergency_disable(
            session, account, actor=ctx.user, reason=payload.reason, commit=True
        )
    except IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    return ServiceAccountOut.of(account)


@router.delete("/service-accounts/{account_id}", status_code=204)
async def delete_service_account(
    account_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    await guard(session, ictx, "service_account.delete")
    account = await _owned_account(session, ctx, account_id)
    await sa_service.delete_account(session, account, actor=ctx.user, commit=True)


@router.get("/service-accounts/{account_id}/credentials", response_model=list[CredentialOut])
async def list_credentials(
    account_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
    include_revoked: bool = Query(default=False),
):
    account = await _owned_account(session, ctx, account_id)
    rows = await sa_service.list_credentials(
        session, account_id=account.id, include_revoked=include_revoked
    )
    return [CredentialOut.of(row) for row in rows]


@router.post(
    "/service-accounts/{account_id}/credentials",
    response_model=CredentialCreatedOut,
    status_code=201,
)
async def issue_credential(
    account_id: uuid.UUID,
    payload: CredentialIn,
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    await guard(session, ictx, "service_account.credential.create")
    account = await _owned_account(session, ctx, account_id)
    if not account.enabled or account.emergency_disabled:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "service_account_disabled",
                "message": "Enable the service account before issuing credentials.",
            },
        )
    issued = await sa_service.issue_credential(
        session,
        account,
        actor=ctx.user,
        label=payload.label,
        expires_in_days=payload.expires_in_days,
        commit=True,
    )
    return CredentialCreatedOut(
        **CredentialOut.of(issued.credential).model_dump(), secret=issued.token
    )


@router.post(
    "/service-accounts/{account_id}/credentials/{credential_id}/rotate",
    response_model=CredentialCreatedOut,
)
async def rotate_credential(
    account_id: uuid.UUID,
    credential_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
    expires_in_days: int | None = Query(default=None, ge=1, le=3650),
):
    await guard(session, ictx, "service_account.credential.rotate")
    account = await _owned_account(session, ctx, account_id)
    credential = await sa_service.get_credential(
        session, account_id=account.id, credential_id=credential_id
    )
    if credential is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Not found"})
    issued = await sa_service.rotate_credential(
        session,
        account,
        credential,
        actor=ctx.user,
        expires_in_days=expires_in_days,
        commit=True,
    )
    return CredentialCreatedOut(
        **CredentialOut.of(issued.credential).model_dump(), secret=issued.token
    )


@router.delete("/service-accounts/{account_id}/credentials/{credential_id}", status_code=204)
async def revoke_credential(
    account_id: uuid.UUID,
    credential_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    await guard(session, ictx, "service_account.credential.revoke")
    account = await _owned_account(session, ctx, account_id)
    credential = await sa_service.get_credential(
        session, account_id=account.id, credential_id=credential_id
    )
    if credential is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Not found"})
    await sa_service.revoke_credential(
        session, credential, actor=ctx.user, reason="revoked_by_admin", commit=True
    )


@router.get("/service-accounts/{account_id}/keys", response_model=list[ApiKeyOut])
async def list_account_keys(
    account_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
):
    account = await _owned_account(session, ctx, account_id)
    rows = await sa_service.keys_for_account(session, account_id=account.id)
    return [ApiKeyOut.of(row) for row in rows]


@router.post("/service-accounts/{account_id}/keys", response_model=ApiKeyCreatedOut, status_code=201)
async def create_account_key(
    account_id: uuid.UUID,
    payload: ApiKeyIn,
    ctx: TenantContext = Depends(require_permission(Permission.SERVICE_ACCOUNT_MANAGE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    """A named, individually revocable token for the account.

    Equivalent in power to a credential and easier to hand out: an operator can
    give one to a script and revoke that one key without touching the daemon's
    long-lived credential.
    """
    await guard(session, ictx, "service_account.key.create")
    account = await _owned_account(session, ctx, account_id)
    if not account.enabled or account.emergency_disabled:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "service_account_disabled",
                "message": "Enable the service account before issuing keys.",
            },
        )
    try:
        scopes = await sa_service.validate_scopes(
            session, actor=ictx.user, scopes=payload.scopes
        )
    except IdentityError as exc:
        raise translate(exc) from None
    issued = await key_service.create_key(
        session,
        tenant_id=ctx.tenant_id,
        actor=ctx.user,
        name=payload.name,
        scopes=scopes,
        expires_in_days=payload.expires_in_days,
        service_account_id=account.id,
        commit=True,
    )
    return ApiKeyCreatedOut(**ApiKeyOut.of(issued.key).model_dump(), secret=issued.token)


__all__ = [
    "CredentialCreatedOut",
    "CredentialIn",
    "CredentialOut",
    "EmergencyClearIn",
    "ServiceAccountIn",
    "ServiceAccountOut",
    "ServiceAccountUpdateIn",
    "ToggleIn",
    "clear_emergency_disable",
    "create_account_key",
    "create_service_account",
    "delete_service_account",
    "disable_service_account",
    "emergency_disable_service_account",
    "enable_service_account",
    "get_service_account",
    "issue_credential",
    "list_account_keys",
    "list_credentials",
    "list_service_accounts",
    "revoke_credential",
    "rotate_credential",
    "router",
    "update_service_account",
]
