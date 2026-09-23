"""
Reusable security dependencies.

Route handlers never parse a token, never read a role string, and never take
a tenant id from the client. They declare what they need:

    @router.get("/tenants/{tenant_id}/leads")
    async def list_leads(
        ctx: TenantContext = Depends(require_permission(Permission.LEAD_READ)),
    ): ...

`ctx.tenant_id` comes from the signed JWT. When a route also carries
`{tenant_id}` in its path, `tenant_path_guard` compares the two and rejects a
mismatch, so an authenticated user cannot reach another tenant by editing the
URL.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import Depends, HTTPException, Path, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.identity import tokens
from app.auth.identity.models import UserSession
from app.auth.identity.principals import AuthenticatedPrincipal
from app.auth.identity.service import IdentityContext, load_identity_context
from app.auth.jwt import TokenError, decode_access_token
from app.auth.permissions import Permission
from app.auth.rbac import has_permission, role_level
from app.auth.service import record_audit
from app.core.config import settings
from app.db.models import AuditAction, Tenant, User, UserRole
from app.db.session import get_session

log = structlog.get_logger()

# auto_error=False so a missing header produces our own 401 with a
# WWW-Authenticate hint rather than FastAPI's default 403.
_bearer = HTTPBearer(auto_error=False)

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def _forbidden(detail: str = "Insufficient permissions") -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


@dataclass(frozen=True)
class TenantContext:
    """Everything a protected handler is allowed to trust.

    ``user`` is always a real ``users`` row. For a machine credential it is the
    human the credential is attributed to — its owner, or the service account's
    creator — so tenant isolation and the audit trail keep working without a
    phantom account. The machine's own limits are the ``scopes`` field, and
    ``is_machine`` is what a route checks when it must be a person.
    """
    user: User
    tenant: Tenant
    auth_method: str = "password"
    session_id: uuid.UUID | None = None
    scopes: frozenset[str] | None = None
    api_key_id: uuid.UUID | None = None
    service_account_id: uuid.UUID | None = None
    credential_name: str = ""

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.id

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def role(self) -> UserRole:
        return self.user.role

    @property
    def is_machine(self) -> bool:
        return self.auth_method in ("api_key", "service_account", "scim")

    def can(self, permission: Permission) -> bool:
        """Role AND scope. The scope half is what makes a read-only key read-only."""
        if not has_permission(self.user.role, permission):
            return False
        if self.scopes is None:
            return True
        return permission.value in self.scopes

    def describe_actor(self) -> str:
        if self.is_machine:
            return self.credential_name or self.auth_method
        return self.user.email


# ------------------------------------------------------------ current user ---

_SESSION_ENDED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Session ended",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    if credentials is None or not credentials.credentials:
        raise _UNAUTHENTICATED
    if (credentials.scheme or "").lower() != "bearer":
        raise _UNAUTHENTICATED

    presented = credentials.credentials

    # STEP 18: machine credentials travel in the same header as a JWT. The
    # shape of the value decides which authenticator runs, so a key is never
    # parsed as a JWT and a JWT is never looked up as a key.
    kind = tokens.split_prefix(
        presented, (settings.api_key_prefix, settings.service_account_prefix)
    )
    if kind:
        from app.auth.identity import api_keys
        from app.auth.identity.exceptions import IdentityError

        try:
            principal = await api_keys.authenticate_credential(session, presented)
        except IdentityError as exc:
            log.warning(
                "auth.machine_credential_rejected",
                reason=getattr(exc, "code", type(exc).__name__),
                path=request.url.path,
            )
            raise _UNAUTHENTICATED from None
        request.state.principal = principal
        request.state.user_id = str(principal.actor.id)
        request.state.tenant_id = str(principal.tenant_id)
        return principal.actor

    try:
        claims = decode_access_token(presented)
    except TokenError:
        raise _UNAUTHENTICATED from None

    user = await session.get(User, claims.user_id)
    if user is None or not user.is_active:
        raise _UNAUTHENTICATED

    # The tenant is re-read from the row, not taken from the token, so a
    # user moved between tenants cannot keep using an old token.
    if user.tenant_id != claims.tenant_id:
        log.warning("auth.tenant_claim_mismatch", user_id=str(user.id))
        raise _UNAUTHENTICATED

    # Deactivation, role change and refresh-reuse all bump this.
    if user.token_version != claims.token_version:
        raise _UNAUTHENTICATED

    # A token minted for a browser session dies with that session: "sign out
    # this device" and "sign out everywhere else" must not leave a valid access
    # token behind for the rest of its lifetime. Tokens minted without a session
    # (service-to-service callers, and every token issued before this feature)
    # carry no `sid` and skip the check, which is what keeps them working.
    if claims.session_id is not None:
        row = await session.get(UserSession, claims.session_id)
        if (
            row is None
            or row.user_id != user.id
            or row.tenant_id != user.tenant_id
            or not row.is_live(datetime.now(timezone.utc).replace(tzinfo=None))
        ):
            log.info(
                "auth.session_not_live",
                user_id=str(user.id),
                session_id=str(claims.session_id),
            )
            raise _SESSION_ENDED

    request.state.user_id = str(user.id)
    request.state.tenant_id = str(user.tenant_id)
    # The UUID itself, not its text: ``TenantContext.session_id`` is typed as
    # uuid.UUID and every consumer compares or looks it up as one. Stringifying
    # here is how a "current session" marker silently stops matching.
    request.state.session_id = claims.session_id
    return user


async def get_current_tenant(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Tenant:
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise _forbidden("Tenant is inactive")
    return tenant


async def get_context(
    request: Request,
    user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
) -> TenantContext:
    principal: AuthenticatedPrincipal | None = getattr(request.state, "principal", None)
    if principal is not None:
        return TenantContext(
            user=user,
            tenant=tenant,
            auth_method=principal.kind.value,
            scopes=principal.scopes,
            api_key_id=principal.api_key_id,
            service_account_id=principal.service_account_id,
            credential_name=principal.display_name,
        )
    return TenantContext(
        user=user,
        tenant=tenant,
        session_id=getattr(request.state, "session_id", None) or None,
    )


async def get_identity_context(
    request: Request,
    ctx: TenantContext = Depends(get_context),
    session: AsyncSession = Depends(get_session),
) -> IdentityContext:
    """The richer context identity routes use: policy, session, factor state.

    Deliberately a *separate* dependency rather than extra fields on
    ``TenantContext``: adding two more queries to every request in the product
    to serve a handful of identity endpoints would be a tax on all of it.
    """
    from app.auth.identity.policies import AuthMethod

    try:
        method = AuthMethod(ctx.auth_method)
    except ValueError:
        method = AuthMethod.PASSWORD
    return await load_identity_context(
        session,
        user=ctx.user,
        tenant=ctx.tenant,
        session_id=ctx.session_id,
        auth_method=method,
        scopes=ctx.scopes,
        service_account_id=ctx.service_account_id,
        api_key_id=ctx.api_key_id,
    )


async def require_human_session(
    ctx: TenantContext = Depends(get_context),
) -> TenantContext:
    """Refuse machine credentials outright.

    Some things only a person may do: enroll a second factor, change a
    password, read another session's device list. A service account holding
    every scope still cannot pass this gate, because the check is on the
    principal's *kind*, not on a permission someone could grant by mistake.
    """
    if ctx.is_machine:
        raise _forbidden("This endpoint requires a signed-in user session")
    return ctx


# ------------------------------------------------------------- authorization ---

def require_permission(
    *permissions: Permission, require_all: bool = True
) -> Callable[..., Awaitable[TenantContext]]:
    """
    Dependency factory. Default is AND; pass require_all=False for OR.

    A denial is written to the audit log so repeated probing is visible.
    """
    async def _dependency(
        request: Request,
        ctx: TenantContext = Depends(get_context),
        session: AsyncSession = Depends(get_session),
    ) -> TenantContext:
        # Role AND scope. A machine credential can only ever narrow its owner's
        # permissions, never widen them, because both halves must pass.
        checks = [ctx.can(p) for p in permissions]
        ok = all(checks) if require_all else any(checks)
        if not ok:
            missing = [
                p.value for p, granted in zip(permissions, checks) if not granted
            ]
            await record_audit(
                session, action=AuditAction.AUTHZ_DENIED, tenant_id=ctx.tenant_id,
                actor_user_id=ctx.user_id, actor_email=ctx.user.email,
                ip_address=_client_ip(request),
                detail={
                    "missing": missing,
                    "path": request.url.path,
                    "auth_method": ctx.auth_method,
                    "principal": ctx.describe_actor(),
                },
            )
            raise _forbidden(f"Requires permission: {', '.join(missing)}")
        return ctx

    return _dependency


def require_role(*roles: UserRole) -> Callable[..., Awaitable[TenantContext]]:
    """Prefer require_permission. Use this only for genuinely role-shaped rules."""
    allowed = set(roles)

    async def _dependency(ctx: TenantContext = Depends(get_context)) -> TenantContext:
        if ctx.role not in allowed:
            raise _forbidden(
                f"Requires role: {', '.join(sorted(r.value for r in allowed))}"
            )
        return ctx

    return _dependency


def require_min_role(minimum: UserRole) -> Callable[..., Awaitable[TenantContext]]:
    threshold = role_level(minimum)

    async def _dependency(ctx: TenantContext = Depends(get_context)) -> TenantContext:
        if ctx.is_machine:
            # A machine credential carries scopes, not a role. Comparing a
            # synthetic role here would be a guess; refusing is the honest
            # answer, and every endpoint that needs a role has a permission
            # equivalent the credential can be granted instead.
            raise _forbidden("This endpoint requires a signed-in user session")
        if role_level(ctx.role) < threshold:
            raise _forbidden(f"Requires at least the {minimum.value} role")
        return ctx

    return _dependency


# ---------------------------------------------------------- tenant isolation ---

async def tenant_path_guard(
    tenant_id: uuid.UUID = Path(...),
    ctx: TenantContext = Depends(get_context),
) -> TenantContext:
    """
    For routes that keep `{tenant_id}` in the URL.

    Returns 404 rather than 403 on a mismatch: confirming that some other
    tenant id exists is itself an information leak.
    """
    if tenant_id != ctx.tenant_id:
        log.warning(
            "authz.cross_tenant_path_blocked",
            user_id=str(ctx.user_id), requested=str(tenant_id),
        )
        raise HTTPException(status_code=404, detail="Not found")
    return ctx


def scoped_permission(
    *permissions: Permission,
) -> Callable[..., Awaitable[TenantContext]]:
    """
    The combination used by almost every route: the path tenant must match the
    token tenant AND the caller must hold the permission.
    """
    permission_dep = require_permission(*permissions)

    async def _dependency(
        tenant_id: uuid.UUID = Path(...),
        ctx: TenantContext = Depends(permission_dep),
    ) -> TenantContext:
        if tenant_id != ctx.tenant_id:
            log.warning(
                "authz.cross_tenant_path_blocked",
                user_id=str(ctx.user_id), requested=str(tenant_id),
            )
            raise HTTPException(status_code=404, detail="Not found")
        return ctx

    return _dependency


async def get_owned(
    session: AsyncSession,
    model: Any,
    object_id: uuid.UUID,
    ctx: TenantContext,
    *,
    tenant_field: str = "tenant_id",
) -> Any:
    """
    Fetch a row by id and prove it belongs to the caller's tenant.

    Always 404 on both "missing" and "belongs to someone else", so object ids
    cannot be probed for existence across tenants.
    """
    obj = await session.get(model, object_id)
    if obj is None or getattr(obj, tenant_field, None) != ctx.tenant_id:
        raise HTTPException(status_code=404, detail="Not found")
    return obj


def tenant_filter(model: Any, ctx: TenantContext):
    """Mandatory WHERE clause for every list query."""
    return model.tenant_id == ctx.tenant_id


# ------------------------------------------------------------------ helpers ---

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


async def get_platform_admin(
    ctx: TenantContext = Depends(get_context),
) -> TenantContext:
    """
    Guard for operator-only actions such as creating a whole new tenant.

    There is no platform-superuser concept yet, so this denies everyone and
    those endpoints are served by an out-of-band script instead. Documented as
    a known limitation rather than left as an open endpoint.
    """
    raise _forbidden("Platform administration is not available through this API")