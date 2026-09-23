"""SCIM errors and the RFC 7644 status/code each one becomes.

Implementation: :mod:`app.auth.identity.exceptions`.

===========================  ==================================================
Error                        Response
===========================  ==================================================
``SCIMUnauthorized``         401 — one uniform body for every credential failure
``SCIMForbidden``            403 — authenticated, but the scope is not granted
``SCIMNotFound``             404 — including "belongs to another tenant"
``SCIMConflict``             409 — ``scimType: uniqueness``
``SCIMInvalidValue``         400 — ``scimType: invalidValue`` or
                             ``invalidFilter`` / ``invalidPath``
``SCIMError``                400 — the base, for anything protocol-shaped
===========================  ==================================================

The routes turn these into bodies with :func:`app.auth.identity.scim.schemas.
error_response`, so a provider always receives a parseable SCIM error document
rather than FastAPI's default ``{"detail": ...}``.
"""
from __future__ import annotations

from app.auth.identity.exceptions import (
    IdentityError,
    SCIMConflict,
    SCIMError,
    SCIMForbidden,
    SCIMInvalidValue,
    SCIMNotFound,
    SCIMUnauthorized,
)

__all__ = [
    "IdentityError",
    "SCIMConflict",
    "SCIMError",
    "SCIMForbidden",
    "SCIMInvalidValue",
    "SCIMNotFound",
    "SCIMUnauthorized",
]
