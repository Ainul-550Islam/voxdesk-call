"""Machine credentials — one composed router, for callers that import this name.

The routing was split so each file matches what it does:

* :mod:`app.api.api_key_routes` — ``/api/api-keys``
* :mod:`app.api.service_account_routes` — ``/api/service-accounts``

``app.main`` registers those two routers directly, which is the canonical
wiring. This module stays because it is the historical import path: it composes
the same two routers into one object, so ``from app.api.machine_routes import
router`` keeps working, and it re-exports the request/response models so
``machine_routes.ApiKeyOut`` (and friends) keep resolving.

Do **not** register both this composed router and the two sub-routers in the
same application: they contain the same paths, and the second registration is
the one FastAPI would report as a duplicate operation id.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.api_key_routes import (
    INVALID_SCOPES_DETAIL,
    ApiKeyCreatedOut,
    ApiKeyIn,
    ApiKeyOut,
    available_scopes,
    create_api_key,
    list_api_keys,
    revoke_api_key,
    rotate_api_key,
)
from app.api.api_key_routes import router as api_key_router
from app.api.machine_guards import guard, require_human
from app.api.service_account_routes import (
    CredentialCreatedOut,
    CredentialIn,
    CredentialOut,
    EmergencyClearIn,
    ServiceAccountIn,
    ServiceAccountOut,
    ServiceAccountUpdateIn,
    ToggleIn,
    clear_emergency_disable,
    create_account_key,
    create_service_account,
    delete_service_account,
    disable_service_account,
    emergency_disable_service_account,
    enable_service_account,
    get_service_account,
    issue_credential,
    list_account_keys,
    list_credentials,
    list_service_accounts,
    revoke_credential,
    rotate_credential,
    update_service_account,
)
from app.api.service_account_routes import router as service_account_router

#: Composed, unprefixed: the two sub-routers already carry ``/api``.
router = APIRouter(tags=["identity"])
router.include_router(api_key_router)
router.include_router(service_account_router)

__all__ = [
    "INVALID_SCOPES_DETAIL",
    "ApiKeyCreatedOut",
    "ApiKeyIn",
    "ApiKeyOut",
    "CredentialCreatedOut",
    "CredentialIn",
    "CredentialOut",
    "EmergencyClearIn",
    "ServiceAccountIn",
    "ServiceAccountOut",
    "ServiceAccountUpdateIn",
    "ToggleIn",
    "api_key_router",
    "available_scopes",
    "clear_emergency_disable",
    "create_account_key",
    "create_api_key",
    "create_service_account",
    "delete_service_account",
    "disable_service_account",
    "emergency_disable_service_account",
    "enable_service_account",
    "get_service_account",
    "guard",
    "issue_credential",
    "list_account_keys",
    "list_api_keys",
    "list_credentials",
    "list_service_accounts",
    "require_human",
    "revoke_api_key",
    "revoke_credential",
    "rotate_api_key",
    "rotate_credential",
    "router",
    "service_account_router",
    "update_service_account",
]
