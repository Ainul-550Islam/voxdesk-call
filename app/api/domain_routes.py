"""Enterprise domain claiming and enforcement.

The invariant every handler here preserves: **a domain that is not verified
imposes nothing**. Enforcement changes are refused while ``verified_at`` is
null, and the policy evaluator ignores an unverified domain's settings even if
they were written directly into the table. That is what makes a typo in a domain
name a failed verification instead of a workforce locked out of its accounts.

Route table:

======================================  ===========================================
GET    /api/domains                     identity:read
POST   /api/domains                     identity:write + fresh reauth
GET    /api/domains/{id}                identity:read
POST   /api/domains/{id}/challenge      identity:write + fresh reauth
POST   /api/domains/{id}/verify         identity:write (+ fresh reauth for the
                                        transition into enforcement)
PATCH  /api/domains/{id}                identity:write + fresh reauth
DELETE /api/domains/{id}                identity:write + fresh reauth
======================================  ===========================================
"""
from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.identity_errors import translate
from app.auth.dependencies import (
    TenantContext,
    get_identity_context,
    require_permission,
)
from app.auth.identity import domains as domain_service
from app.auth.identity.exceptions import IdentityError
from app.auth.identity.service import IdentityContext, assert_privileged
from app.auth.permissions import Permission
from app.db.session import get_session

log = structlog.get_logger()
router = APIRouter(prefix="/api/domains", tags=["identity"])


def _require_human(ctx: IdentityContext) -> None:
    if ctx.is_machine:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "human_session_required",
                "message": "A signed-in user session is required to manage domains.",
            },
        )


async def _guard(session: AsyncSession, ictx: IdentityContext, action: str) -> None:
    _require_human(ictx)
    try:
        await assert_privileged(session, ictx, action=action)
    except IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None


class DomainOut(BaseModel):
    id: str
    domain: str
    verified: bool
    verified_at: datetime | None = None
    enforcement: str = "off"
    block_password_login: bool = False
    sso_connection_id: str | None = None
    failed_attempts: int = 0
    created_at: datetime | None = None
    #: The TXT record to publish, when a challenge is open. The *value* is only
    #: ever returned by the call that created the challenge.
    verification_record_name: str = ""
    verification_status: str = ""

    @classmethod
    def of(cls, row, *, record_name: str = "", status: str = "") -> DomainOut:
        return cls(
            id=str(row.id),
            domain=row.domain,
            verified=row.verified_at is not None,
            verified_at=row.verified_at,
            enforcement=row.enforcement or "off",
            block_password_login=bool(row.block_password_login),
            sso_connection_id=str(row.sso_connection_id) if row.sso_connection_id else None,
            failed_attempts=int(row.failed_attempts or 0),
            created_at=row.created_at,
            verification_record_name=record_name,
            verification_status=status,
        )


class DomainCreatedOut(DomainOut):
    #: Shown once, at creation, and never again: only its digest is stored.
    verification_record_value: str


class DomainIn(BaseModel):
    domain: str = Field(min_length=4, max_length=253)


class DomainUpdateIn(BaseModel):
    enforcement: str | None = None
    block_password_login: bool | None = None
    sso_connection_id: uuid.UUID | None = None


class VerifyOut(BaseModel):
    verified: bool
    status: str
    detail: str
    record_name: str
    records_seen: int


@router.get("", response_model=list[DomainOut])
async def list_domains(
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_READ)),
    session: AsyncSession = Depends(get_session),
):
    rows = await domain_service.list_domains(session, tenant_id=ctx.tenant_id)
    out = []
    for row in rows:
        challenge = await domain_service.pending_challenge(session, domain_id=row.id)
        out.append(
            DomainOut.of(
                row,
                record_name=challenge.record_name if challenge else "",
                status=challenge.status if challenge else "",
            )
        )
    return out


@router.post("", response_model=DomainCreatedOut, status_code=201)
async def add_domain(
    payload: DomainIn,
    request: Request,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_WRITE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    """Claim a domain and open a verification challenge."""
    await _guard(session, ictx, "domain.add")
    try:
        issue = await domain_service.add_domain(
            session, tenant_id=ctx.tenant_id, actor=ctx.user,
            domain=payload.domain, commit=False,
        )
    except IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    await session.commit()
    row = await domain_service.get_domain(
        session, tenant_id=ctx.tenant_id, domain_id=issue.verification.domain_id
    )
    if row is None:  # pragma: no cover - the row was just inserted in this request
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Not found"})
    return DomainCreatedOut(
        **DomainOut.of(row, record_name=issue.record_name, status=issue.verification.status).model_dump(),
        verification_record_value=issue.record_value,
    )


async def _owned(session: AsyncSession, ctx: TenantContext, domain_id: uuid.UUID):
    row = await domain_service.get_domain(session, tenant_id=ctx.tenant_id, domain_id=domain_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Not found"})
    return row


@router.get("/{domain_id}", response_model=DomainOut)
async def get_domain(
    domain_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_READ)),
    session: AsyncSession = Depends(get_session),
):
    row = await _owned(session, ctx, domain_id)
    challenge = await domain_service.pending_challenge(session, domain_id=row.id)
    return DomainOut.of(
        row,
        record_name=challenge.record_name if challenge else "",
        status=challenge.status if challenge else "",
    )


@router.post("/{domain_id}/challenge", response_model=DomainCreatedOut)
async def issue_challenge(
    domain_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_WRITE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    """Re-issue a challenge, invalidating the previous token."""
    await _guard(session, ictx, "domain.challenge")
    row = await _owned(session, ctx, domain_id)
    issue = await domain_service.issue_challenge(session, row, actor=ctx.user, commit=True)
    return DomainCreatedOut(
        **DomainOut.of(row, record_name=issue.record_name, status=issue.verification.status).model_dump(),
        verification_record_value=issue.record_value,
    )


@router.post("/{domain_id}/verify", response_model=VerifyOut)
async def verify_domain(
    domain_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_WRITE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    """Look up the TXT record and settle the challenge."""
    await _guard(session, ictx, "domain.verify")
    row = await _owned(session, ctx, domain_id)
    challenge = await domain_service.pending_challenge(session, domain_id=row.id)
    try:
        outcome = await domain_service.check_challenge(
            session, row, actor=ctx.user, commit=True
        )
    except IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    return VerifyOut(
        verified=outcome.verified,
        status=outcome.status,
        detail=outcome.detail,
        record_name=challenge.record_name if challenge else "",
        records_seen=len(outcome.records_seen),
    )


@router.patch("/{domain_id}", response_model=DomainOut)
async def update_domain(
    domain_id: uuid.UUID,
    payload: DomainUpdateIn,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_WRITE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    """Change enforcement. Refused for an unverified domain."""
    await _guard(session, ictx, "domain.update")
    row = await _owned(session, ctx, domain_id)
    try:
        await domain_service.set_enforcement(
            session,
            row,
            actor=ctx.user,
            enforcement=payload.enforcement if payload.enforcement is not None else row.enforcement,
            block_password_login=payload.block_password_login,
            sso_connection_id=payload.sso_connection_id,
            commit=True,
        )
    except IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    return DomainOut.of(row)


@router.delete("/{domain_id}", status_code=204)
async def remove_domain(
    domain_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_WRITE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    await _guard(session, ictx, "domain.remove")
    row = await _owned(session, ctx, domain_id)
    await domain_service.remove_domain(session, row, actor=ctx.user, commit=True)
