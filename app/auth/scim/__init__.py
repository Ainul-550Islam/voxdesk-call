"""SCIM 2.0 provisioning.

The implementation is :mod:`app.auth.identity.scim`; this package names it by
concern so the routes and the tests read the way the RFC does:

============  =================================================================
Module        Concern
============  =================================================================
``models``    the resource payloads and the credential/group-mapping rows
``users``     ``/Users`` operations
``groups``    ``/Groups`` operations
``schemas``   resource documents, list envelopes and error bodies
``auth``      the dedicated, hashed, rotatable provisioning credential
``service``   the orchestration every one of the above is built from
``exceptions``the typed errors, and the RFC 7644 §3.12 code each one carries
============  =================================================================

SCIM traffic never authenticates with a user's JWT. The credential is
``vdscim_<prefix>_<secret>``, stored as a digest, scoped to one tenant and
optionally to one connection, and it cannot call the product API: the SCIM
principal is resolved by ``authenticate`` and nothing else accepts it.
"""
from __future__ import annotations

from app.auth.scim import auth, exceptions, groups, models, schemas, service, users

__all__ = ["auth", "exceptions", "groups", "models", "schemas", "service", "users"]
