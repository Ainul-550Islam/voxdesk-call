"""SCIM 2.0 endpoints.

Two groups again:

* ``/api/scim/credentials`` — administrator-facing management of the bearer
  tokens an IdP uses. Guarded by ``identity:write`` and a fresh reauthentication.
* ``/scim/v2/{connection_id}/…`` — the protocol surface itself, authenticated by
  a SCIM credential and nothing else. Never by a user JWT: an integration that
  runs unattended must not need a human's token, and a human's token must never
  be usable as a provisioning credential.

The protocol surface answers with SCIM error documents (RFC 7644 §3.12) rather
than this product's error shape, because the caller is an identity provider and
not our dashboard. Status codes follow the RFC exactly: 201 with a ``Location``
header on create, 409 with ``scimType: uniqueness`` on a duplicate, 404 on a
resource in another tenant (identical to a resource that does not exist), and
200 with an empty ``ListResponse`` when a filter matches nothing.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.identity_errors import translate
from app.auth.dependencies import (
    TenantContext,
    get_identity_context,
    require_permission,
)
from app.auth.identity import exceptions as identity_exc
from app.auth.identity import scim as scim_pkg  # noqa: F401  (package import side effect)
from app.auth.identity.scim import schemas
from app.auth.identity.scim import service as scim_service
from app.auth.identity.service import IdentityContext, assert_privileged
from app.auth.permissions import Permission
from app.db.session import get_session

log = structlog.get_logger()
admin_router = APIRouter(prefix="/api/scim", tags=["identity"])
scim_router = APIRouter(prefix="/scim/v2", tags=["scim"])

SCIM_MEDIA_TYPE = "application/scim+json"


# ========================================================== credential admin ===


class CredentialIn(BaseModel):
    label: str = Field(default="", max_length=120)
    connection_id: uuid.UUID | None = None
    scopes: list[str] = Field(default_factory=list)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class CredentialOut(BaseModel):
    id: str
    label: str = ""
    prefix: str
    scopes: list[str]
    connection_id: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    @classmethod
    def of(cls, row) -> CredentialOut:
        return cls(
            id=str(row.id),
            label=row.label or "",
            prefix=row.token_prefix,
            scopes=[str(s) for s in (row.scopes or [])],
            connection_id=str(row.connection_id) if row.connection_id else None,
            created_at=row.created_at,
            expires_at=row.expires_at,
            last_used_at=row.last_used_at,
            revoked_at=row.revoked_at,
        )


class CredentialCreatedOut(CredentialOut):
    token: str
    warning: str = (
        "Copy this token now: it is stored as a hash and cannot be shown again. "
        "It grants user and group provisioning for this workspace only."
    )


def _require_human(ctx: IdentityContext) -> None:
    if ctx.is_machine:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "human_session_required",
                "message": "A signed-in user session is required to manage SCIM credentials.",
            },
        )


async def _guard(session: AsyncSession, ictx: IdentityContext, action: str) -> None:
    _require_human(ictx)
    try:
        await assert_privileged(session, ictx, action=action)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None


def _credential_error(exc: identity_exc.SCIMError) -> HTTPException:
    """A SCIM-domain error raised by an administrative route.

    The SCIM endpoints answer with an RFC 7644 error document; these routes are
    part of the product's own API, so they answer in the product's shape. The
    status is the one the SCIM error already carries — a credential that is gone
    is a 404, not a 500, which is what a retrying administrator needs to see.
    """
    status_code = exc.status or 400
    return HTTPException(
        status_code=status_code,
        detail={
            "code": "not_found" if status_code == 404 else (exc.scim_type or exc.code),
            "message": str(exc) or "The credential could not be updated.",
        },
    )


@admin_router.get("/credentials", response_model=list[CredentialOut])
async def list_scim_credentials(
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_READ)),
    session: AsyncSession = Depends(get_session),
    include_revoked: bool = Query(default=False),
):
    rows = await scim_service.list_credentials(
        session, tenant_id=ctx.tenant_id, include_revoked=include_revoked
    )
    return [CredentialOut.of(row) for row in rows]


@admin_router.post("/credentials", response_model=CredentialCreatedOut, status_code=201)
async def create_scim_credential(
    payload: CredentialIn,
    request: Request,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_WRITE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    """Issue a SCIM token. Scoped to provisioning only, never to administration."""
    await _guard(session, ictx, "scim.credential.create")
    try:
        issued = await scim_service.create_credential(
            session,
            tenant_id=ctx.tenant_id,
            actor=ctx.user,
            label=payload.label,
            connection_id=payload.connection_id,
            scopes=payload.scopes or None,
            expires_in_days=payload.expires_in_days,
            commit=True,
        )
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    return CredentialCreatedOut(**CredentialOut.of(issued.credential).model_dump(), token=issued.token)


@admin_router.post("/credentials/{credential_id}/rotate", response_model=CredentialCreatedOut)
async def rotate_scim_credential(
    credential_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_WRITE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    await _guard(session, ictx, "scim.credential.rotate")
    credential = await scim_service.get_credential(
        session, tenant_id=ctx.tenant_id, credential_id=credential_id
    )
    if credential is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Not found"})
    try:
        issued = await scim_service.rotate_credential(
            session, credential, actor=ctx.user, commit=True
        )
    except identity_exc.SCIMError as exc:
        await session.rollback()
        raise _credential_error(exc) from None
    return CredentialCreatedOut(**CredentialOut.of(issued.credential).model_dump(), token=issued.token)


@admin_router.delete("/credentials/{credential_id}", status_code=204)
async def revoke_scim_credential(
    credential_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_WRITE)),
    session: AsyncSession = Depends(get_session),
    ictx: IdentityContext = Depends(get_identity_context),
):
    await _guard(session, ictx, "scim.credential.revoke")
    credential = await scim_service.get_credential(
        session, tenant_id=ctx.tenant_id, credential_id=credential_id
    )
    if credential is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Not found"})
    try:
        await scim_service.revoke_credential(session, credential, actor=ctx.user, commit=True)
    except identity_exc.SCIMError as exc:
        await session.rollback()
        raise _credential_error(exc) from None


# ============================================================ protocol surface ===


def _scim_error(exc: identity_exc.SCIMError) -> JSONResponse:
    """The RFC 7644 error document, plus the challenge a 401 owes the caller.

    RFC 7235 requires a 401 to say how to authenticate; an identity provider that
    cannot see the scheme has nothing to put in its configuration screen and
    reports the integration as broken.
    """
    status = exc.status or 400
    headers: dict[str, str] = {}
    if status == 401:
        headers["WWW-Authenticate"] = 'Bearer realm="scim", error="invalid_token"'
    return JSONResponse(
        status_code=status,
        content=schemas.error_response(
            detail=str(exc), status=status, scim_type=exc.scim_type
        ),
        media_type=SCIM_MEDIA_TYPE,
        headers=headers or None,
    )


async def _principal(
    session: AsyncSession,
    authorization: str | None,
    connection_id: str,
) -> scim_service.SCIMPrincipal:
    """Authenticate a SCIM caller, and bind it to the connection in the path.

    The path's connection is *checked against* the credential rather than
    trusted: a credential scoped to one connection cannot provision through
    another one's URL, which is what stops a leaked token from reaching across a
    tenant's connections.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise identity_exc.SCIMUnauthorized()
    token = authorization.split(" ", 1)[1].strip()
    principal = await scim_service.authenticate(session, token)

    if connection_id and connection_id != "default":
        try:
            wanted = uuid.UUID(connection_id)
        except ValueError:
            raise identity_exc.SCIMNotFound("Unknown provisioning endpoint") from None
        if principal.connection_id is not None and principal.connection_id != wanted:
            raise identity_exc.SCIMNotFound("Unknown provisioning endpoint")
    if connection_id == "default" and principal.connection_id is not None:
        raise identity_exc.SCIMNotFound("Unknown provisioning endpoint")
    return principal


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/") + request.url.path


@scim_router.get("/{connection_id}/ServiceProviderConfig")
async def service_provider_config(
    connection_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    try:
        await _principal(session, authorization, connection_id)
    except identity_exc.SCIMError as exc:
        return _scim_error(exc)
    return JSONResponse(
        schemas.service_provider_config(base_url=str(request.base_url).rstrip("/")
                                        + f"/scim/v2/{connection_id}"),
        media_type=SCIM_MEDIA_TYPE,
    )


@scim_router.get("/{connection_id}/ResourceTypes")
async def resource_types(
    connection_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    try:
        await _principal(session, authorization, connection_id)
    except identity_exc.SCIMError as exc:
        return _scim_error(exc)
    base = str(request.base_url).rstrip("/") + f"/scim/v2/{connection_id}"
    return JSONResponse(
        schemas.list_response(
            schemas.resource_types(base_url=base), total=2, start_index=1, items_per_page=2
        ),
        media_type=SCIM_MEDIA_TYPE,
    )


@scim_router.get("/{connection_id}/Schemas")
async def schema_documents(
    connection_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    try:
        await _principal(session, authorization, connection_id)
    except identity_exc.SCIMError as exc:
        return _scim_error(exc)
    base = str(request.base_url).rstrip("/") + f"/scim/v2/{connection_id}"
    documents = schemas.schemas(base_url=base)
    return JSONResponse(
        schemas.list_response(
            documents, total=len(documents), start_index=1, items_per_page=len(documents)
        ),
        media_type=SCIM_MEDIA_TYPE,
    )


# --------------------------------------------------------------------- Users ---


@scim_router.get("/{connection_id}/Users")
async def list_users(
    connection_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    filter: str = Query(default=""),
    startIndex: int = Query(default=1, ge=1),
    count: int | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
):
    try:
        principal = await _principal(session, authorization, connection_id)
        query = scim_service.UserQuery(filter_text=filter, start_index=startIndex, count=count)
        resources, total = await scim_service.list_users(session, principal, query)
    except identity_exc.SCIMError as exc:
        return _scim_error(exc)
    return JSONResponse(
        schemas.list_response(
            resources,
            total=total,
            start_index=startIndex,
            items_per_page=len(resources),
        ),
        media_type=SCIM_MEDIA_TYPE,
    )


@scim_router.post("/{connection_id}/Users", status_code=201)
async def create_user(
    connection_id: str,
    request: Request,
    body: dict = Body(...),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    try:
        principal = await _principal(session, authorization, connection_id)
        payload = scim_service.parse_user_payload(body, required=True)
        resource = await scim_service.create_user(session, principal, payload)
    except identity_exc.SCIMError as exc:
        await session.rollback()
        return _scim_error(exc)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        return _scim_error(identity_exc.SCIMInvalidValue(str(exc)))
    base = str(request.base_url).rstrip("/") + f"/scim/v2/{connection_id}"
    return JSONResponse(
        resource,
        status_code=201,
        media_type=SCIM_MEDIA_TYPE,
        headers={"Location": f"{base}/Users/{resource['id']}"},
    )


@scim_router.get("/{connection_id}/Users/{user_id}")
async def get_user(
    connection_id: str,
    user_id: str,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    try:
        principal = await _principal(session, authorization, connection_id)
        resource = await scim_service.get_user(session, principal, schemas.parse_uuid(user_id))
    except identity_exc.SCIMError as exc:
        return _scim_error(exc)
    return JSONResponse(resource, media_type=SCIM_MEDIA_TYPE)


@scim_router.put("/{connection_id}/Users/{user_id}")
async def replace_user(
    connection_id: str,
    user_id: str,
    body: dict = Body(...),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    try:
        principal = await _principal(session, authorization, connection_id)
        payload = scim_service.parse_user_payload(body, required=False)
        resource = await scim_service.replace_user(
            session, principal, schemas.parse_uuid(user_id), payload
        )
    except identity_exc.SCIMError as exc:
        await session.rollback()
        return _scim_error(exc)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        return _scim_error(identity_exc.SCIMInvalidValue(str(exc)))
    return JSONResponse(resource, media_type=SCIM_MEDIA_TYPE)


@scim_router.patch("/{connection_id}/Users/{user_id}")
async def patch_user(
    connection_id: str,
    user_id: str,
    body: dict = Body(...),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    operations = body.get("Operations") if isinstance(body, dict) else None
    if not isinstance(operations, list) or not operations:
        return _scim_error(identity_exc.SCIMInvalidValue("A PATCH body needs Operations."))
    try:
        principal = await _principal(session, authorization, connection_id)
        resource = await scim_service.patch_user(
            session, principal, schemas.parse_uuid(user_id), operations
        )
    except identity_exc.SCIMError as exc:
        await session.rollback()
        return _scim_error(exc)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        return _scim_error(identity_exc.SCIMInvalidValue(str(exc)))
    return JSONResponse(resource, media_type=SCIM_MEDIA_TYPE)


@scim_router.delete("/{connection_id}/Users/{user_id}", status_code=204)
async def delete_user(
    connection_id: str,
    user_id: str,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    try:
        principal = await _principal(session, authorization, connection_id)
        await scim_service.delete_user(session, principal, schemas.parse_uuid(user_id))
    except identity_exc.SCIMError as exc:
        await session.rollback()
        return _scim_error(exc)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        return _scim_error(identity_exc.SCIMInvalidValue(str(exc)))
    return Response(status_code=204)


# -------------------------------------------------------------------- Groups ---


@scim_router.get("/{connection_id}/Groups")
async def list_groups(
    connection_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    filter: str = Query(default=""),
    startIndex: int = Query(default=1, ge=1),
    count: int | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
):
    try:
        principal = await _principal(session, authorization, connection_id)
        query = scim_service.UserQuery(filter_text=filter, start_index=startIndex, count=count)
        resources, total = await scim_service.list_groups(session, principal, query)
    except identity_exc.SCIMError as exc:
        return _scim_error(exc)
    return JSONResponse(
        schemas.list_response(
            resources, total=total, start_index=startIndex, items_per_page=len(resources)
        ),
        media_type=SCIM_MEDIA_TYPE,
    )


@scim_router.post("/{connection_id}/Groups", status_code=201)
async def create_group(
    connection_id: str,
    request: Request,
    body: dict = Body(...),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    try:
        principal = await _principal(session, authorization, connection_id)
        payload = scim_service.parse_group_payload(body, required=True)
        resource = await scim_service.upsert_group(session, principal, payload)
    except identity_exc.SCIMError as exc:
        await session.rollback()
        return _scim_error(exc)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        return _scim_error(identity_exc.SCIMInvalidValue(str(exc)))
    base = str(request.base_url).rstrip("/") + f"/scim/v2/{connection_id}"
    return JSONResponse(
        resource,
        status_code=201,
        media_type=SCIM_MEDIA_TYPE,
        headers={"Location": f"{base}/Groups/{resource['id']}"},
    )


@scim_router.get("/{connection_id}/Groups/{group_id}")
async def get_group(
    connection_id: str,
    group_id: str,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    try:
        principal = await _principal(session, authorization, connection_id)
        resource = await scim_service.get_group(session, principal, schemas.parse_uuid(group_id))
    except identity_exc.SCIMError as exc:
        return _scim_error(exc)
    return JSONResponse(resource, media_type=SCIM_MEDIA_TYPE)


@scim_router.put("/{connection_id}/Groups/{group_id}")
async def replace_group(
    connection_id: str,
    group_id: str,
    body: dict = Body(...),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    try:
        principal = await _principal(session, authorization, connection_id)
        payload = scim_service.parse_group_payload(body, required=False)
        resource = await scim_service.upsert_group(
            session, principal, payload, group_id=schemas.parse_uuid(group_id)
        )
    except identity_exc.SCIMError as exc:
        await session.rollback()
        return _scim_error(exc)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        return _scim_error(identity_exc.SCIMInvalidValue(str(exc)))
    return JSONResponse(resource, media_type=SCIM_MEDIA_TYPE)


@scim_router.patch("/{connection_id}/Groups/{group_id}")
async def patch_group(
    connection_id: str,
    group_id: str,
    body: dict = Body(...),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    operations = body.get("Operations") if isinstance(body, dict) else None
    if not isinstance(operations, list) or not operations:
        return _scim_error(identity_exc.SCIMInvalidValue("A PATCH body needs Operations."))
    try:
        principal = await _principal(session, authorization, connection_id)
        resource = await scim_service.patch_group(
            session, principal, schemas.parse_uuid(group_id), operations
        )
    except identity_exc.SCIMError as exc:
        await session.rollback()
        return _scim_error(exc)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        return _scim_error(identity_exc.SCIMInvalidValue(str(exc)))
    return JSONResponse(resource, media_type=SCIM_MEDIA_TYPE)


@scim_router.delete("/{connection_id}/Groups/{group_id}", status_code=204)
async def delete_group(
    connection_id: str,
    group_id: str,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    try:
        principal = await _principal(session, authorization, connection_id)
        await scim_service.delete_group(session, principal, schemas.parse_uuid(group_id))
    except identity_exc.SCIMError as exc:
        await session.rollback()
        return _scim_error(exc)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        return _scim_error(identity_exc.SCIMInvalidValue(str(exc)))
    return Response(status_code=204)
