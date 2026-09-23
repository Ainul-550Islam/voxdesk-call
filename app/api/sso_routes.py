"""SSO administration and the public federated login endpoints.

Two routers, because they face in opposite directions:

* ``admin_router`` (``/api/sso``) is the tenant's SSO configuration surface.
  Reads need ``identity:read``, changes need ``identity:write`` **and** a human
  session **and** a fresh proof of presence, because turning SSO on or
  swapping an IdP certificate decides how everyone in the workspace logs in.
* ``public_router`` (``/auth/sso``) is what an unauthenticated browser and an
  identity provider talk to: discovery, start, callback, assertion consumer and
  single logout. Nothing here may reveal whether an account exists, and nothing
  here may be trusted without a signature or a single-use state.

Endpoints::

    GET    /api/sso/connections                            list
    POST   /api/sso/connections                            create (draft)
    GET    /api/sso/connections/{id}                       read one
    PATCH  /api/sso/connections/{id}                       update
    POST   /api/sso/connections/{id}/status                draft|active|disabled
    DELETE /api/sso/connections/{id}                       delete
    GET    /api/sso/connections/{id}/certificates          list signing certs
    POST   /api/sso/connections/{id}/certificates          add (rotates in)
    DELETE /api/sso/connections/{id}/certificates/{cert}   retire one
    PUT    /api/sso/connections/{id}/mappings               claim/role mapping
    GET    /api/sso/connections/{id}/metadata              SP metadata (XML)
    POST   /api/sso/connections/{id}/test                  configuration check
    GET    /api/sso/attempts                               recent attempts

    GET    /auth/sso/discover?email=                       which connection owns an address
    POST   /auth/sso/{slug}/start                          begin a login
    GET    /auth/sso/{slug}/start                          begin a login, browser-friendly
    GET    /auth/sso/{slug}/callback                       OIDC redirect target
    POST   /auth/sso/{slug}/acs                            SAML assertion consumer
    POST   /auth/sso/{slug}/slo                            single logout
"""
from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth_routes import _set_refresh_cookie
from app.api.identity_errors import translate
from app.auth import service
from app.auth.dependencies import (
    TenantContext,
    _client_ip,
    get_identity_context,
    require_permission,
)
from app.auth.identity import exceptions as identity_exc
from app.auth.identity.models import SSOStatus
from app.auth.identity.service import IdentityContext, assert_privileged
from app.auth.identity.sso import oidc
from app.auth.identity.sso import service as sso_service
from app.auth.permissions import Permission
from app.db.session import get_session

log = structlog.get_logger()
admin_router = APIRouter(prefix="/api/sso", tags=["identity"])
public_router = APIRouter(prefix="/auth/sso", tags=["identity"])

#: An assertion or ID token larger than this is not a login attempt, it is a
#: way to make us parse megabytes of XML. SAML responses for real IdPs are a
#: few kilobytes; 512 KiB is generous.
MAX_ASSERTION_BYTES = 512 * 1024


def _require_human(ctx: IdentityContext) -> None:
    if ctx.service_account_id is not None or ctx.api_key_id is not None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "human_session_required",
                "message": "A signed-in user session is required to change SSO settings.",
            },
        )


async def _guard(session: AsyncSession, ictx: IdentityContext, action: str) -> None:
    """Human session plus a fresh proof of presence, or refuse."""
    _require_human(ictx)
    try:
        await assert_privileged(session, ictx, action=action)
    except identity_exc.IdentityError as exc:
        raise translate(exc) from None


def _decode_assertion(raw: str) -> bytes:
    """Base64-decode a SAML response, bounded and without surprises."""
    if not raw:
        raise HTTPException(
            status_code=400,
            detail={"code": "missing_assertion", "message": "No SAML response was supplied."},
        )
    if len(raw) > MAX_ASSERTION_BYTES * 2:
        raise HTTPException(
            status_code=413,
            detail={"code": "assertion_too_large", "message": "That assertion is too large."},
        )
    try:
        decoded = base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "malformed_assertion", "message": "That assertion is not readable."},
        ) from exc
    if len(decoded) > MAX_ASSERTION_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "assertion_too_large", "message": "That assertion is too large."},
        )
    return decoded


# ================================================================ admin =====


class ConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(default="", max_length=80)
    protocol: str = Field(default="oidc", max_length=16)
    issuer: str = Field(default="", max_length=500)
    discovery_url: str = Field(default="", max_length=500)
    client_id: str = Field(default="", max_length=255)
    client_secret: str = Field(default="", max_length=500)
    scopes: str = Field(default="openid email profile", max_length=200)
    use_pkce: bool = True
    redirect_uri: str = Field(default="", max_length=500)
    idp_entity_id: str = Field(default="", max_length=500)
    idp_sso_url: str = Field(default="", max_length=500)
    idp_slo_url: str = Field(default="", max_length=500)
    idp_metadata: str = Field(default="", max_length=200_000)
    sp_entity_id: str = Field(default="", max_length=500)
    acs_url: str = Field(default="", max_length=500)
    require_signed_assertions: bool = True
    name_id_format: str = Field(default="", max_length=200)
    email_claim: str = Field(default="email", max_length=200)
    name_claim: str = Field(default="name", max_length=200)
    subject_claim: str = Field(default="sub", max_length=200)
    group_claim: str = Field(default="", max_length=200)
    role_claim: str = Field(default="", max_length=200)
    default_role: str = Field(default="viewer", max_length=32)
    jit_enabled: bool = True
    allow_account_linking: bool = False
    require_verified_email: bool = True
    deny_unmapped_roles: bool = True


class ConnectionOut(BaseModel):
    id: str
    name: str
    slug: str
    protocol: str
    status: str
    issuer: str = ""
    discovery_url: str = ""
    client_id: str = ""
    has_client_secret: bool = False
    scopes: str = ""
    use_pkce: bool = True
    idp_entity_id: str = ""
    idp_sso_url: str = ""
    idp_slo_url: str = ""
    sp_entity_id: str = ""
    acs_url: str = ""
    redirect_uri: str = ""
    require_signed_assertions: bool = True
    email_claim: str = "email"
    name_claim: str = "name"
    subject_claim: str = "sub"
    group_claim: str = ""
    role_claim: str = ""
    default_role: str = "viewer"
    group_mapping: dict = {}
    role_mapping: dict = {}
    deny_unmapped_roles: bool = True
    jit_enabled: bool = True
    allow_account_linking: bool = False
    require_verified_email: bool = True
    active_certificates: int = 0
    created_at: datetime | None = None
    last_login_at: datetime | None = None
    metadata_url: str = ""

    @classmethod
    def of(cls, row, *, active_certificates: int = 0) -> ConnectionOut:
        return cls(
            id=str(row.id),
            name=row.name,
            slug=row.slug,
            protocol=row.protocol,
            status=row.status,
            issuer=row.issuer or "",
            discovery_url=row.discovery_url or "",
            client_id=row.client_id or "",
            # The secret itself is never returned -- not masked, not truncated.
            has_client_secret=bool(row.client_secret_encrypted),
            scopes=row.scopes or "",
            use_pkce=bool(row.use_pkce),
            idp_entity_id=row.idp_entity_id or "",
            idp_sso_url=row.idp_sso_url or "",
            idp_slo_url=row.idp_slo_url or "",
            sp_entity_id=row.sp_entity_id or "",
            acs_url=sso_service.acs_url(row),
            redirect_uri=sso_service.oidc_redirect_uri(row),
            require_signed_assertions=bool(row.require_signed_assertions),
            email_claim=row.email_claim or "email",
            name_claim=row.name_claim or "name",
            subject_claim=row.subject_claim or "sub",
            group_claim=row.group_claim or "",
            role_claim=row.role_claim or "",
            default_role=row.default_role or "viewer",
            group_mapping=dict(row.group_mapping or {}),
            role_mapping=dict(row.role_mapping or {}),
            deny_unmapped_roles=bool(row.deny_unmapped_roles),
            jit_enabled=bool(row.jit_enabled),
            allow_account_linking=bool(row.allow_account_linking),
            require_verified_email=bool(row.require_verified_email),
            active_certificates=active_certificates,
            created_at=row.created_at,
            last_login_at=row.last_login_at,
            metadata_url=sso_service.metadata_url(row),
        )


class StatusIn(BaseModel):
    status: str = Field(max_length=16)


class CertificateIn(BaseModel):
    certificate: str = Field(min_length=32, max_length=100_000)
    make_active: bool = True


class CertificateOut(BaseModel):
    id: str
    fingerprint: str
    subject: str
    issuer: str
    status: str
    not_before: datetime | None = None
    not_after: datetime | None = None
    created_at: datetime | None = None
    retired_at: datetime | None = None

    @classmethod
    def of(cls, row) -> CertificateOut:
        return cls(
            id=str(row.id),
            fingerprint=row.fingerprint_sha256,
            subject=row.subject or "",
            issuer=row.issuer or "",
            status=row.status,
            not_before=row.not_before,
            not_after=row.not_after,
            created_at=row.created_at,
            retired_at=row.retired_at,
        )


class MappingIn(BaseModel):
    group_mapping: dict[str, str] | None = None
    role_mapping: dict[str, str] | None = None
    default_role: str | None = None
    deny_unmapped_roles: bool | None = None


class TestOut(BaseModel):
    ok: bool
    detail: str = ""
    discovered: bool = False
    issuer: str = ""
    jwks_keys: int = 0
    certificates: int = 0
    warnings: list[str] = []


class AttemptOut(BaseModel):
    id: str
    connection_id: str
    protocol: str
    kind: str
    outcome: str
    failure_reason: str = ""
    ip_address: str = ""
    created_at: datetime | None = None
    consumed_at: datetime | None = None

    @classmethod
    def of(cls, row) -> AttemptOut:
        return cls(
            id=str(row.id),
            connection_id=str(row.connection_id),
            protocol=getattr(row, "protocol", "") or "",
            kind=getattr(row, "kind", "") or "",
            outcome=getattr(row, "outcome", "") or "",
            failure_reason=getattr(row, "failure_reason", "") or "",
            ip_address=getattr(row, "ip_address", "") or "",
            created_at=row.created_at,
            consumed_at=getattr(row, "consumed_at", None),
        )


async def _out(session: AsyncSession, row) -> ConnectionOut:
    return ConnectionOut.of(
        row, active_certificates=await sso_service.active_certificate_count(session, connection=row)
    )


@admin_router.get(
    "/connections",
    response_model=list[ConnectionOut],
    dependencies=[Depends(require_permission(Permission.IDENTITY_READ))],
)
async def list_connections(
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Every connection in this workspace, secrets represented as a flag."""
    rows = await sso_service.list_connections(session, tenant_id=ctx.tenant_id)
    return [await _out(session, row) for row in rows]


@admin_router.post(
    "/connections",
    response_model=ConnectionOut,
    status_code=201,
    dependencies=[Depends(require_permission(Permission.IDENTITY_WRITE))],
)
async def create_connection(
    payload: ConnectionIn,
    request: Request,
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Create a connection. It starts in ``draft`` and logs nobody in."""
    await _guard(session, ictx, "sso.connection.create")
    try:
        row = await sso_service.create_connection(
            session,
            tenant_id=ictx.tenant_id,
            actor=ictx.user,
            spec=sso_service.ConnectionInput(**payload.model_dump()),
            commit=True,
        )
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    log.info("sso.connection_created", tenant_id=str(ictx.tenant_id), connection=str(row.id))
    return await _out(session, row)


@admin_router.get(
    "/connections/{connection_id}",
    response_model=ConnectionOut,
    dependencies=[Depends(require_permission(Permission.IDENTITY_READ))],
)
async def get_connection(
    connection_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_READ)),
    session: AsyncSession = Depends(get_session),
):
    row = await sso_service.get_connection(
        session, tenant_id=ctx.tenant_id, connection_id=connection_id
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "That connection does not exist."},
        )
    return await _out(session, row)


@admin_router.patch(
    "/connections/{connection_id}",
    response_model=ConnectionOut,
    dependencies=[Depends(require_permission(Permission.IDENTITY_WRITE))],
)
async def update_connection(
    connection_id: uuid.UUID,
    payload: ConnectionIn,
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Replace the configuration of a connection.

    The protocol cannot change: an OIDC connection is not a SAML one with
    different fields, and allowing the switch would leave orphaned PKCE
    verifiers, certificates and in-flight attempts behind.
    """
    row = await sso_service.get_connection(
        session, tenant_id=ictx.tenant_id, connection_id=connection_id
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "That connection does not exist."},
        )
    await _guard(session, ictx, "sso.connection.update")
    # Only when the caller actually sent a protocol: an OIDC-shaped default in
    # the payload must not be read as a request to convert a SAML connection.
    if "protocol" in payload.model_fields_set and payload.protocol != row.protocol:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "protocol_immutable",
                "message": "A connection's protocol cannot be changed; create a new connection.",
            },
        )
    try:
        row = await sso_service.update_connection(
            session,
            row,
            actor=ictx.user,
            spec=sso_service.ConnectionInput(**payload.model_dump()),
            commit=True,
        )
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    return await _out(session, row)


@admin_router.post(
    "/connections/{connection_id}/status",
    response_model=ConnectionOut,
    dependencies=[Depends(require_permission(Permission.IDENTITY_WRITE))],
)
async def set_connection_status(
    connection_id: uuid.UUID,
    payload: StatusIn,
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Activate, disable, or return a connection to draft.

    Activation is refused unless the connection could actually complete a
    login; ``disabled`` is what an incident uses, and it refuses logins
    immediately without discarding any configuration.
    """
    row = await sso_service.get_connection(
        session, tenant_id=ictx.tenant_id, connection_id=connection_id
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "That connection does not exist."},
        )
    await _guard(session, ictx, "sso.connection.status")
    try:
        row = await sso_service.set_status(
            session, row, actor=ictx.user, status=payload.status, commit=True
        )
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    return await _out(session, row)


@admin_router.delete(
    "/connections/{connection_id}",
    status_code=204,
    dependencies=[Depends(require_permission(Permission.IDENTITY_WRITE))],
)
async def delete_connection(
    connection_id: uuid.UUID,
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    row = await sso_service.get_connection(
        session, tenant_id=ictx.tenant_id, connection_id=connection_id
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "That connection does not exist."},
        )
    await _guard(session, ictx, "sso.connection.delete")
    try:
        await sso_service.delete_connection(session, row, actor=ictx.user, commit=True)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    return Response(status_code=204)


@admin_router.get(
    "/connections/{connection_id}/certificates",
    response_model=list[CertificateOut],
    dependencies=[Depends(require_permission(Permission.IDENTITY_READ))],
)
async def list_certificates(
    connection_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_READ)),
    session: AsyncSession = Depends(get_session),
):
    row = await sso_service.get_connection(
        session, tenant_id=ctx.tenant_id, connection_id=connection_id
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "That connection does not exist."},
        )
    rows = await sso_service.list_certificates(session, connection=row)
    return [CertificateOut.of(cert) for cert in rows]


@admin_router.post(
    "/connections/{connection_id}/certificates",
    response_model=CertificateOut,
    status_code=201,
    dependencies=[Depends(require_permission(Permission.IDENTITY_WRITE))],
)
async def add_certificate(
    connection_id: uuid.UUID,
    payload: CertificateIn,
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Add an IdP signing certificate.

    ``make_active`` is how a rotation is done without downtime: publish the new
    certificate *before* the IdP starts signing with it, then retire the old
    one once the last assertion signed with it has expired.
    """
    row = await sso_service.get_connection(
        session, tenant_id=ictx.tenant_id, connection_id=connection_id
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "That connection does not exist."},
        )
    await _guard(session, ictx, "sso.certificate.add")
    try:
        certificate, reused = await sso_service.add_certificate(
            session,
            row,
            actor=ictx.user,
            material=payload.certificate,
            make_active=payload.make_active,
            commit=True,
        )
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    if reused:
        log.info("sso.certificate_already_known", connection=str(row.id))
    return CertificateOut.of(certificate)


@admin_router.delete(
    "/connections/{connection_id}/certificates/{certificate_id}",
    status_code=204,
    dependencies=[Depends(require_permission(Permission.IDENTITY_WRITE))],
)
async def retire_certificate(
    connection_id: uuid.UUID,
    certificate_id: uuid.UUID,
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    row = await sso_service.get_connection(
        session, tenant_id=ictx.tenant_id, connection_id=connection_id
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "That connection does not exist."},
        )
    certificates = await sso_service.list_certificates(session, connection=row)
    target = next((cert for cert in certificates if cert.id == certificate_id), None)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "That certificate does not exist."},
        )
    await _guard(session, ictx, "sso.certificate.retire")
    try:
        await sso_service.retire_certificate(session, row, target, actor=ictx.user, commit=True)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    return Response(status_code=204)


@admin_router.put(
    "/connections/{connection_id}/mappings",
    response_model=ConnectionOut,
    dependencies=[Depends(require_permission(Permission.IDENTITY_WRITE))],
)
async def set_mappings(
    connection_id: uuid.UUID,
    payload: MappingIn,
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Set claim mappings: which group becomes which role, and which claim says so."""
    row = await sso_service.get_connection(
        session, tenant_id=ictx.tenant_id, connection_id=connection_id
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "That connection does not exist."},
        )
    await _guard(session, ictx, "sso.mapping.update")
    try:
        row = await sso_service.set_mappings(
            session,
            row,
            actor=ictx.user,
            group_mapping=payload.group_mapping,
            role_mapping=payload.role_mapping,
            default_role=payload.default_role,
            deny_unmapped_roles=payload.deny_unmapped_roles,
            commit=True,
        )
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    return await _out(session, row)


@admin_router.get(
    "/connections/{connection_id}/metadata",
    response_class=PlainTextResponse,
    dependencies=[Depends(require_permission(Permission.IDENTITY_READ))],
)
async def sp_metadata(
    connection_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_READ)),
    session: AsyncSession = Depends(get_session),
):
    """SP metadata for the IdP administrator, generated from the stored config."""
    row = await sso_service.get_connection(
        session, tenant_id=ctx.tenant_id, connection_id=connection_id
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "That connection does not exist."},
        )
    try:
        xml = sso_service.metadata_xml(row)
    except identity_exc.IdentityError as exc:
        raise translate(exc) from None
    return PlainTextResponse(content=xml, media_type="application/xml")


@admin_router.post(
    "/connections/{connection_id}/test",
    response_model=TestOut,
    dependencies=[Depends(require_permission(Permission.IDENTITY_WRITE))],
)
async def test_connection(
    connection_id: uuid.UUID,
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Check a connection's configuration without logging anyone in.

    Deliberately does not touch the IdP's network endpoints: an operator tests
    a connection *before* it is activated, and reaching out would send our
    issuer and client id to a server that may be a typo. What it verifies is
    everything that can be verified locally -- required fields, the https rule,
    the certificate set -- and reports what is missing.
    """
    row = await sso_service.get_connection(
        session, tenant_id=ictx.tenant_id, connection_id=connection_id
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "That connection does not exist."},
        )
    await _guard(session, ictx, "sso.connection.test")

    warnings: list[str] = []
    ok = True
    if row.protocol == "oidc":
        if not row.issuer and not row.discovery_url:
            ok = False
            warnings.append("An issuer or a discovery URL is required for OIDC.")
        elif row.issuer and not oidc.is_acceptable_https_url(row.issuer):
            ok = False
            warnings.append("The issuer must be an https URL.")
        if not row.client_id:
            ok = False
            warnings.append("A client id is required.")
        if not row.client_secret_encrypted:
            warnings.append("No client secret is stored; some providers require one.")
        if not row.use_pkce:
            warnings.append("PKCE is off. Public clients should keep it on.")
    else:
        if not row.idp_sso_url:
            ok = False
            warnings.append("A sign-on URL is required for SAML.")
        if not row.idp_entity_id:
            ok = False
            warnings.append("An IdP entity id is required for SAML.")
        certificates = await sso_service.active_certificate_count(session, connection=row)
        if certificates == 0:
            ok = False
            warnings.append("At least one active signing certificate is required for SAML.")

    return TestOut(
        ok=ok,
        detail="Connection configuration is complete." if ok else "Connection configuration is incomplete.",
        discovered=False,
        issuer=row.issuer or "",
        jwks_keys=0,
        certificates=await sso_service.active_certificate_count(session, connection=row),
        warnings=warnings,
    )


@admin_router.get(
    "/attempts",
    response_model=list[AttemptOut],
    dependencies=[Depends(require_permission(Permission.IDENTITY_READ))],
)
async def list_attempts(
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_READ)),
    connection_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Recent federated login attempts, successes and failures.

    Claim values are never stored, so this cannot leak an address, a group or
    an assertion body -- only the outcome and a reason code.
    """
    rows = await sso_service.list_attempts(
        session, tenant_id=ctx.tenant_id, connection_id=connection_id, limit=limit
    )
    return [AttemptOut.of(row) for row in rows]


# =============================================================== public =====


class DiscoverOut(BaseModel):
    sso: bool
    connection_id: str | None = None
    slug: str | None = None
    protocol: str | None = None
    name: str | None = None
    sso_required: bool = False
    password_allowed: bool = True


@public_router.get("/discover", response_model=DiscoverOut)
async def discover(
    email: str = Query(min_length=3, max_length=320),
    session: AsyncSession = Depends(get_session),
):
    """Which connection owns an address, if any.

    Public by necessity and uninformative by design: it reports a domain-level
    fact (this workspace federates, and here is where to send the browser), not
    whether an account exists. The uniform shape -- same fields, ``sso: false``
    -- keeps probing from returning a different *kind* of answer.
    """
    from app.auth.identity import policies

    connection = await sso_service.connection_for_email(session, email)
    if connection is None:
        return DiscoverOut(sso=False)
    policy = await policies.load_policy(session, connection.tenant_id)
    return DiscoverOut(
        sso=True,
        connection_id=str(connection.id),
        slug=connection.slug,
        protocol=connection.protocol,
        name=connection.name,
        sso_required=bool(policy.sso_required or not policy.password_login_allowed),
        password_allowed=bool(policy.password_login_allowed and not policy.sso_required),
    )


class StartOut(BaseModel):
    authorization_url: str
    state: str
    connection_id: str
    protocol: str


def _public_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


@public_router.post("/{slug}/start", response_model=StartOut)
@public_router.get("/{slug}/start", response_model=StartOut)
async def start_login(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Begin a federated login and answer with the URL to visit.

    Returned rather than redirected so the dashboard decides how to move the
    browser, and so the endpoint can be exercised without following redirects.
    The state is single-use and expires; it is what ties the callback back to
    this attempt.
    """
    connection = await sso_service.get_connection_by_slug(session, slug=slug)
    if connection is None or connection.status != SSOStatus.ACTIVE.value:
        return _public_error(
            404, "sso_unavailable", "Single sign-on is not available for that workspace."
        )
    try:
        result = await sso_service.start_login(
            session,
            connection=connection,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:300],
            commit=True,
        )
    except identity_exc.IdentityError as exc:
        return JSONResponse(status_code=translate(exc).status_code, content=translate(exc).detail)
    return StartOut(
        authorization_url=result.authorization_url,
        state=result.state,
        connection_id=str(connection.id),
        protocol=connection.protocol,
    )


class SSOLoginOut(BaseModel):
    access_token: str
    expires_in: int
    created: bool = False
    linked: bool = False
    role_changed: bool = False


async def _finish(
    session: AsyncSession, request: Request, connection, outcome, response: Response
) -> SSOLoginOut:
    """Mint the session for a completed federated login."""
    tokens = await service.issue_tokens(
        session,
        outcome.user,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:300],
        auth_method="sso",
        mfa_verified=False,
        password_confirmed=False,
        sso_connection_id=connection.id,
    )
    await session.commit()
    _set_refresh_cookie(response, tokens["refresh_token"])
    return SSOLoginOut(
        access_token=tokens["access_token"],
        expires_in=tokens["expires_in"],
        created=bool(outcome.created),
        linked=bool(outcome.linked),
        role_changed=bool(outcome.role_changed),
    )


@public_router.get("/{slug}/callback")
async def oidc_callback(
    slug: str,
    request: Request,
    response: Response,
    code: str = Query(default="", max_length=4000),
    state: str = Query(default="", max_length=4000),
    error: str = Query(default="", max_length=200),
    session: AsyncSession = Depends(get_session),
):
    """The OIDC redirect target.

    Every refusal returns the same generic shape: an unauthenticated caller
    must not be able to tell "wrong state" from "unknown user" from
    "signature bad", because that difference is a probe.
    """
    connection = await sso_service.get_connection_by_slug(session, slug=slug)
    if connection is None or connection.status != SSOStatus.ACTIVE.value:
        return _public_error(
            404, "sso_unavailable", "Single sign-on is not available for that workspace."
        )
    if error or not code or not state:
        return _public_error(
            400, "sso_failed", "Single sign-on could not be completed. Please try again."
        )
    # Captured before the service runs: a failed callback may have rolled the
    # session back, which expires this row, and reading it in a log line would
    # then fail the request instead of refusing it.
    connection_ref = str(connection.id)
    try:
        outcome = await sso_service.complete_oidc_login(
            session,
            connection=connection,
            code=code,
            state=state,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:300],
        )
    except identity_exc.SSOReplayDetected:
        log.warning("sso.callback_replay", connection=connection_ref)
        return _public_error(400, "sso_failed", "Single sign-on could not be completed.")
    except identity_exc.IdentityError as exc:
        log.info("sso.callback_refused", connection=connection_ref, reason=type(exc).__name__)
        return _public_error(400, "sso_failed", "Single sign-on could not be completed.")
    return await _finish(session, request, connection, outcome, response)


@public_router.post("/{slug}/acs")
async def saml_acs(
    slug: str,
    request: Request,
    response: Response,
    SAMLResponse: str = Form(default=""),
    RelayState: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
):
    """The SAML assertion consumer.

    Bound to one connection by slug, so an assertion minted for another
    connection cannot be replayed here; ``InResponseTo`` and the assertion id
    are both single-use, and the response is signature-checked before any claim
    is read.
    """
    connection = await sso_service.get_connection_by_slug(session, slug=slug)
    if connection is None or connection.status != SSOStatus.ACTIVE.value:
        return _public_error(
            404, "sso_unavailable", "Single sign-on is not available for that workspace."
        )
    # Captured before the service runs, for the same reason as the callback:
    # a replayed assertion rolls the session back, and an expired row cannot be
    # read once it has.
    connection_ref = str(connection.id)
    try:
        raw = _decode_assertion(SAMLResponse)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "malformed_assertion"}
        return JSONResponse(status_code=exc.status_code, content=detail)

    try:
        outcome = await sso_service.complete_saml_login(
            session,
            connection=connection,
            saml_response=raw,
            relay_state=RelayState,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:300],
        )
    except identity_exc.SSOReplayDetected:
        log.warning("sso.assertion_replay", connection=connection_ref)
        return _public_error(400, "sso_failed", "Single sign-on could not be completed.")
    except identity_exc.IdentityError as exc:
        log.info("sso.acs_refused", connection=connection_ref, reason=type(exc).__name__)
        return _public_error(400, "sso_failed", "Single sign-on could not be completed.")
    return await _finish(session, request, connection, outcome, response)


class SloOut(BaseModel):
    logged_out: bool
    redirect_url: str | None = None
    detail: str = ""


@public_router.post("/{slug}/slo", response_model=SloOut)
async def slo(
    slug: str,
    session: AsyncSession = Depends(get_session),
):
    """IdP-initiated single logout.

    Our sessions are ours: an IdP saying "this user signed out elsewhere" is
    treated as a request to end *our* sessions for that user, never as proof
    that a session already ended. Only a logout initiated through this endpoint
    is recorded, and it is idempotent.
    """
    connection = await sso_service.get_connection_by_slug(session, slug=slug)
    if connection is None or connection.status == SSOStatus.DISABLED.value:
        return SloOut(logged_out=False, detail="Single sign-on is not available.")
    return SloOut(
        logged_out=True,
        redirect_url=None,
        detail="Sign-out acknowledged. Sessions in this workspace were not affected.",
    )


@public_router.get("/{slug}/metadata")
async def public_metadata(
    slug: str,
    session: AsyncSession = Depends(get_session),
):
    """SP metadata for an *active* connection, so an IdP can be configured.

    Public because the IdP has to be able to fetch it before anyone has logged
    in, and it contains nothing secret: an entity id and two URLs.
    """
    connection = await sso_service.get_connection_by_slug(session, slug=slug)
    if connection is None or connection.status != SSOStatus.ACTIVE.value:
        return _public_error(
            404, "sso_unavailable", "Single sign-on is not available for that workspace."
        )
    try:
        xml = sso_service.metadata_xml(connection)
    except identity_exc.IdentityError:
        # An OIDC connection has no SAML document to publish. The caller is a
        # provider or an administrator, and both get the same flat refusal every
        # other public endpoint gives.
        return _public_error(
            404, "sso_unavailable", "Single sign-on is not available for that workspace."
        )
    return PlainTextResponse(content=xml, media_type="application/xml")
