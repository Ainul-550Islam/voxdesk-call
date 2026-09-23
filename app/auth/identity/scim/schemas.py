"""SCIM 2.0 representations (RFC 7643).

Hand-written rather than generated from a schema package: the four resources an
identity provider actually provisions (User, Group, ServiceProviderConfig,
ResourceTypes/Schemas) fit in one file, and a representation that is written out
is one an operator can compare against RFC 7643 in a minute.

Two conventions worth stating:

* **``meta.location`` is built from the request's own base path**, so a tenant
  that reaches us through a proxy sees the URL it used, not a hard-coded one.
* **Nothing secret is ever represented.** A SCIM credential is created once, in
  the response to the call that created it; it never appears in a resource.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCIM_SERVICE_PROVIDER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
SCIM_RESOURCE_TYPE_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
SCIM_SCHEMA_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"

SCIM_CONTENT_TYPE = "application/scim+json"


def _timestamp(value: datetime | None) -> str:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def user_resource(
    *,
    user,
    base_url: str,
    external_id: str = "",
    groups: list[dict] | None = None,
    email_verified: bool = False,
) -> dict[str, Any]:
    """The SCIM representation of a user.

    ``userName`` is the login address, which is what every IdP matches on.
    ``active`` mirrors ``User.is_active`` exactly — SCIM deactivation and the
    product's deactivation are the same bit, deliberately, so there is no state
    where the IdP believes someone is disabled and they are not.
    """
    resource = {
        "schemas": [SCIM_USER_SCHEMA],
        "id": str(user.id),
        "externalId": external_id or None,
        "userName": user.email,
        "name": {"formatted": user.full_name or user.email.split("@", 1)[0]},
        "displayName": user.full_name or user.email.split("@", 1)[0],
        "emails": [
            {
                "value": user.email,
                "primary": True,
                "type": "work",
                "verified": bool(email_verified or user.email_verified_at),
            }
        ],
        "active": bool(user.is_active),
        "meta": {
            "resourceType": "User",
            "created": _timestamp(user.created_at),
            "lastModified": _timestamp(user.updated_at or user.created_at),
            "location": f"{base_url}/Users/{user.id}",
        },
    }
    if groups:
        resource["groups"] = groups
    return resource


def group_resource(
    *,
    group,
    base_url: str,
    members: list[dict] | None = None,
    mapped_role: str = "",
) -> dict[str, Any]:
    resource = {
        "schemas": [SCIM_GROUP_SCHEMA],
        "id": str(group.id),
        "externalId": group.external_id or None,
        "displayName": group.display_name,
        "meta": {
            "resourceType": "Group",
            "created": _timestamp(group.created_at),
            "lastModified": _timestamp(group.updated_at or group.created_at),
            "location": f"{base_url}/Groups/{group.id}",
        },
    }
    if members is not None:
        resource["members"] = members
    if mapped_role:
        # Not part of RFC 7643; carried as an extension attribute so an
        # administrator can see, from the IdP's own view, which role this group
        # grants. Read-only.
        resource["urn:voxdesk:params:scim:schemas:extension:role"] = {"role": mapped_role}
    return resource


def list_response(
    resources: list[dict], *, total: int, start_index: int, items_per_page: int
) -> dict[str, Any]:
    return {
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": items_per_page,
        "Resources": resources,
    }


def error_response(
    *, detail: str, status: int, scim_type: str = ""
) -> dict[str, Any]:
    payload: dict[str, Any] = {"schemas": [SCIM_ERROR_SCHEMA], "detail": detail, "status": str(status)}
    if scim_type:
        payload["scimType"] = scim_type
    return payload


def service_provider_config(*, base_url: str) -> dict[str, Any]:
    """What this SCIM implementation supports, stated honestly.

    The IdP reads this and adapts: advertising PATCH support we did not have
    would give an administrator a broken-looking integration with no
    explanation, and advertising less than we do makes them do extra work.
    """
    return {
        "schemas": [SCIM_SERVICE_PROVIDER_SCHEMA],
        "documentationUri": "https://voxdesk.local/docs/SCIM.md",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 500},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "OAuth Bearer Token",
                "description": (
                    "A SCIM credential issued for this workspace. It can only "
                    "provision users and groups; it is not an API key and carries "
                    "no other permission."
                ),
                "specUri": "https://www.rfc-editor.org/info/rfc6750",
                "primary": True,
            }
        ],
        "meta": {
            "resourceType": "ServiceProviderConfig",
            "location": f"{base_url}/ServiceProviderConfig",
        },
    }


def resource_types(*, base_url: str) -> list[dict[str, Any]]:
    return [
        {
            "schemas": [SCIM_RESOURCE_TYPE_SCHEMA],
            "id": "User",
            "name": "User",
            "endpoint": "/Users",
            "description": "User account",
            "schema": SCIM_USER_SCHEMA,
            "schemaExtensions": [],
            "meta": {"resourceType": "ResourceType", "location": f"{base_url}/ResourceTypes/User"},
        },
        {
            "schemas": [SCIM_RESOURCE_TYPE_SCHEMA],
            "id": "Group",
            "name": "Group",
            "endpoint": "/Groups",
            "description": "Group of users, mapped to a workspace role",
            "schema": SCIM_GROUP_SCHEMA,
            "schemaExtensions": [],
            "meta": {"resourceType": "ResourceType", "location": f"{base_url}/ResourceTypes/Group"},
        },
    ]


def schemas(*, base_url: str) -> list[dict[str, Any]]:
    """The attributes we actually support, per RFC 7643 §7."""
    return [
        {
            "schemas": [SCIM_SCHEMA_SCHEMA],
            "id": SCIM_USER_SCHEMA,
            "name": "User",
            "description": "User Account",
            "attributes": [
                {"name": "userName", "type": "string", "multiValued": False,
                 "required": True, "caseExact": False, "mutability": "readWrite",
                 "returned": "default", "uniqueness": "server"},
                {"name": "externalId", "type": "string", "multiValued": False,
                 "required": False, "caseExact": True, "mutability": "readWrite",
                 "returned": "default", "uniqueness": "none"},
                {"name": "displayName", "type": "string", "multiValued": False,
                 "required": False, "caseExact": False, "mutability": "readWrite",
                 "returned": "default", "uniqueness": "none"},
                {"name": "emails", "type": "complex", "multiValued": True,
                 "required": False, "mutability": "readWrite", "returned": "default",
                 "subAttributes": [
                     {"name": "value", "type": "string", "multiValued": False,
                      "required": False, "mutability": "readWrite", "returned": "default"},
                     {"name": "primary", "type": "boolean", "multiValued": False,
                      "required": False, "mutability": "readWrite", "returned": "default"},
                     {"name": "verified", "type": "boolean", "multiValued": False,
                      "required": False, "mutability": "readOnly", "returned": "default"},
                 ]},
                {"name": "active", "type": "boolean", "multiValued": False,
                 "required": False, "mutability": "readWrite", "returned": "default"},
                {"name": "groups", "type": "complex", "multiValued": True,
                 "required": False, "mutability": "readOnly", "returned": "default",
                 "subAttributes": [
                     {"name": "value", "type": "string", "multiValued": False,
                      "required": False, "mutability": "readOnly", "returned": "default"},
                     {"name": "display", "type": "string", "multiValued": False,
                      "required": False, "mutability": "readOnly", "returned": "default"},
                 ]},
            ],
            "meta": {"resourceType": "Schema", "location": f"{base_url}/Schemas/{SCIM_USER_SCHEMA}"},
        },
        {
            "schemas": [SCIM_SCHEMA_SCHEMA],
            "id": SCIM_GROUP_SCHEMA,
            "name": "Group",
            "description": "Group",
            "attributes": [
                {"name": "displayName", "type": "string", "multiValued": False,
                 "required": True, "caseExact": False, "mutability": "readWrite",
                 "returned": "default", "uniqueness": "server"},
                {"name": "externalId", "type": "string", "multiValued": False,
                 "required": False, "caseExact": True, "mutability": "readWrite",
                 "returned": "default", "uniqueness": "none"},
                {"name": "members", "type": "complex", "multiValued": True,
                 "required": False, "mutability": "readWrite", "returned": "default",
                 "subAttributes": [
                     {"name": "value", "type": "string", "multiValued": False,
                      "required": False, "mutability": "readWrite", "returned": "default"},
                     {"name": "display", "type": "string", "multiValued": False,
                      "required": False, "mutability": "readOnly", "returned": "default"},
                 ]},
            ],
            "meta": {"resourceType": "Schema", "location": f"{base_url}/Schemas/{SCIM_GROUP_SCHEMA}"},
        },
    ]


def parse_uuid(value: str) -> uuid.UUID:
    """SCIM ids are opaque strings; ours are UUIDs.

    A non-UUID id is an *invalid value* (400), not a 404: the difference tells
    an integrator that their id format is wrong rather than that the resource
    vanished.
    """
    from app.auth.identity.exceptions import SCIMInvalidValue

    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise SCIMInvalidValue("Resource identifiers must be UUIDs.") from None
