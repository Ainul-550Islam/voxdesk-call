"""Who is calling: the one structure every authenticator returns.

There are four ways to reach this API — a human's JWT, a human's API key, a
service account's credential, and a SCIM bearer — and the request pipeline
downstream must not care which one it was. It cares about three things: whose
tenant this is, which permissions the caller may exercise, and whether the
caller is a person (because some endpoints may only be used by one).

``AuthenticatedPrincipal`` is that answer. It is *not* a stand-in for a user
row: ``actor`` is always a real ``users`` row — for a service account it is the
human who created it, which is what makes the tenant binding and the audit
trail work without inventing a phantom account — and the machine-only facts
live in their own fields. Routes that must refuse machines check
``is_machine``; nothing else needs to know.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field

from app.db.models import User


class PrincipalKind(str, enum.Enum):
    USER = "user"
    API_KEY = "api_key"
    SERVICE_ACCOUNT = "service_account"
    SCIM = "scim"

    @property
    def is_machine(self) -> bool:
        return self is not PrincipalKind.USER


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """A verified caller. Constructed only by an authenticator, never a route."""

    kind: PrincipalKind
    actor: User
    tenant_id: uuid.UUID
    #: ``None`` means "this principal is not scope-limited" — true for a human
    #: session, false for every machine credential, which always carries an
    #: explicit list even when that list is empty (an empty list grants
    #: nothing and is a legal, if useless, key).
    scopes: frozenset[str] | None = None
    api_key_id: uuid.UUID | None = None
    service_account_id: uuid.UUID | None = None
    credential_id: uuid.UUID | None = None
    display_name: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def is_machine(self) -> bool:
        return self.kind.is_machine

    @property
    def user_id(self) -> uuid.UUID:
        return self.actor.id

    def permits(self, permission: str) -> bool:
        """Whether this principal's scopes cover a permission *as well as* role.

        Role is checked by the caller (``app.auth.rbac.has_permission``); this
        is the second, narrower gate that makes a read-only key read-only.
        """
        if self.scopes is None:
            return True
        return permission in self.scopes
