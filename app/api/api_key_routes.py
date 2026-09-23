"""API key lifecycle: mint, list, rotate, revoke.

Three rules hold for every route here:

* **A credential cannot exceed its maker.** Requested scopes are checked against
  the caller's own permissions (``policies.evaluate_scope_grant``); platform
  scopes are refused outright.
* **A secret is returned once, from the call that created it.** No endpoint ever
  returns a secret that already exists — rotation is the only way to get a new
  one, and it revokes the old one in the same transaction.
* **Revocation is immediate.** There is no cache to wait out: authentication
  reads the row on every request.

Route table:

======================================  =========================================
Route                                    Guard
======================================  =========================================
GET    /api/api-keys                     ``api_key:manage``
GET    /api/api-keys/scopes              ``api_key:manage``
POST   /api/api-keys                     ``api_key:manage`` + fresh reauth
DELETE /api/api-keys/{id}                ``api_key:manage`` + fresh reauth
POST   /api/api-keys/{id}/rotate         ``api_key:manage`` + fresh reauth
======================================  =========================================

Service-account routes live in :mod:`app.api.service_account_routes`; the two
used to share ``app/api/machine_routes.py``, which is kept as a composing shim
for callers that still import it.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.identity_errors import translate
from app.api.machine_guards import guard
from app.auth.dependencies import (
    TenantContext,
    _client_ip,
    get_identity_context,
    require_permission,
)
from app.auth.identity import api_keys as key_service
from app.auth.identity import service_accounts as sa_service
from app.auth.identity.exceptions import IdentityError
from app.auth.identity.service import IdentityContext
from app.auth.permissions import Permission
from app.db.session import get_session

log = structlog.get_logger()
#: ``/api`` prefix, matching every other route module in the repository.
router = APIRouter(prefix="/api", tags=["identity"])

INVALID_SCOPES_DETAIL = (
    "A scope must be a permission this account already holds. Platform-level "
    "permissions cannot be granted to machine credentials."
)


class ApiKeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str = ""
    owner_user_id: str | None = None
    service_account_id: str | None = None
    rotated_from_id: str | None = None

    @classmethod
    def of(cls, row) -> ApiKeyOut:
        return cls(
            id=str(row.id),
            name=row.name,
            prefix=row.prefix,
            scopes=[str(s) for s in (row.scopes or [])],
            created_at=row.created_at,
            expires_at=row.expires_at,
            last_used_at=row.last_used_at,
            revoked_at=row.revoked_at,
            revoked_reason=row.revoked_reason or "",
            owner_user_id=str(row.user_id) if row.user_id else None,
            service_account_id=str(row.service_account_id) if row.service_account_id else None,
            rotated_from_id=str(row.rotated_from_id) if row.rotated_from_id else None,
        )


class ApiKeyCreatedOut(ApiKeyOut):
    #: Present only in the response that created the key.
    secret: str
    warning: str = (
        "Copy this key now. It is stored as a hash and cannot be shown again."
    )


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=list)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    ctx: TenantContext = Depends(require_permission(Permission.API_KEY_MANAGE)),
    session: AsyncSession = Depends(get_session),
    include_revoked: bool = Query(default=False),
    mine_only: bool = Query(default=False),
):
    """The caller's own keys by default; the whole tenant with ``mine_only=false``.

    A tenant administrator needs to see every credential in their workspace —
    that is how a leaked key gets found — and the default keeps the common case
    (a developer checking their own keys) from listing somebody else's.
    """
    if mine_only:
        rows = await key_service.keys_for_principal(
            session, tenant_id=ctx.tenant_id, principal_user_id=ctx.user_id
        )
    else:
        rows = await key_service.list_keys(
            session, tenant_id=ctx.tenant_id, include_revoked=include_revoked
        )
    return [ApiKeyOut.of(row) for row in rows]


@router.get("/api-keys/scopes")
async def available_scopes(
    ctx: TenantContext = Depends(require_permission(Permission.API_KEY_MANAGE)),
):
    """Scopes this caller may grant, so a UI never offers an impossible one."""
    return {
        "grantable": sa_service.grantable_scopes(ctx.role),
        "all_scopes": key_service.known_scope_values(),
    }


@router.post("/api-keys", response_model=ApiKeyCreatedOut, status_code=201)
async def create_api_key(
    payload: ApiKeyIn,
    request: Request,
    ctx: TenantContext = Depends(require_permission(Permission.API_KEY_MANAGE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    """Create a key for the calling user. The secret is in this response only."""
    await guard(session, ictx, "api_key.create")

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
        owner_user_id=ctx.user_id,
        commit=False,
    )
    from app.auth.identity import events as identity_events
    from app.db.models import AuditAction

    await identity_events.emit(
        session,
        AuditAction.API_KEY_CREATED,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        ip_address=_client_ip(request),
        detail={
            "key_id": str(issued.key.id),
            "key_prefix": issued.key.prefix,
            "scopes": scopes,
            "expires_at": issued.key.expires_at.isoformat() if issued.key.expires_at else "",
        },
        commit=False,
    )
    await session.commit()
    return ApiKeyCreatedOut(**ApiKeyOut.of(issued.key).model_dump(), secret=issued.token)


async def _owned_key(session: AsyncSession, ctx: TenantContext, key_id: uuid.UUID):
    row = await key_service.get_key(session, tenant_id=ctx.tenant_id, key_id=key_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Not found"})
    return row


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(require_permission(Permission.API_KEY_MANAGE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    await guard(session, ictx, "api_key.revoke")
    row = await _owned_key(session, ctx, key_id)
    await key_service.revoke_key(
        session, row, actor=ctx.user, reason="revoked_by_admin", commit=True
    )


@router.post("/api-keys/{key_id}/rotate", response_model=ApiKeyCreatedOut)
async def rotate_api_key(
    key_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(require_permission(Permission.API_KEY_MANAGE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
    expires_in_days: int | None = Query(default=None, ge=1, le=3650),
):
    """Replace a key. The old one stops working in the same transaction."""
    await guard(session, ictx, "api_key.rotate")
    row = await _owned_key(session, ctx, key_id)
    issued = await key_service.rotate_key(
        session, row, actor=ctx.user, expires_in_days=expires_in_days, commit=True
    )
    return ApiKeyCreatedOut(**ApiKeyOut.of(issued.key).model_dump(), secret=issued.token)


__all__ = [
    "ApiKeyCreatedOut",
    "ApiKeyIn",
    "ApiKeyOut",
    "available_scopes",
    "create_api_key",
    "list_api_keys",
    "revoke_api_key",
    "rotate_api_key",
    "router",
]
