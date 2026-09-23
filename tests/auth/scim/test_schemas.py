"""The SCIM resource representations, in isolation.

An identity provider reads these documents and decides what to send back, so
the shapes are a contract rather than a convenience: ``schemas`` names the
RFC 7643 URI, ``meta.resourceType`` and ``meta.location`` tell the IdP where the
resource lives, ids are strings (not UUIDs), and the ServiceProviderConfig has
to advertise exactly what the implementation can actually do — an IdP that is
told PATCH is unsupported will refuse to sync, and one that is told PATCH *is*
supported will send patches.

The other property asserted here is subtraction: the documents carry the SCIM
vocabulary and nothing else. A representation that leaks a password hash, a
tenant id, a token digest or an internal flag is a disclosure bug wearing a
specification's clothes, so each test checks the key set rather than trusting
the function that builds it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.auth.identity.exceptions import SCIMInvalidValue
from app.auth.identity.models import SCIMGroupMapping
from app.auth.identity.scim import schemas
from app.db.models import User, UserRole
from tests.conftest import make_user

pytestmark = pytest.mark.asyncio

BASE = "https://voxdesk.local/scim/v2/default"

#: Everything a user resource is allowed to contain. Anything outside this set
#: is internal state that must not have been included.
USER_KEYS = {
    "schemas",
    "id",
    "externalId",
    "userName",
    "name",
    "displayName",
    "nickName",
    "emails",
    "active",
    "groups",
    "meta",
}
GROUP_KEYS = {
    "schemas",
    "id",
    "externalId",
    "displayName",
    "members",
    "meta",
    "urn:voxdesk:params:scim:schemas:extension:role",
}
LEAKY = {
    "password_hash",
    "password",
    "tenant_id",
    "token_hash",
    "token_prefix",
    "secret",
    "mfa_secret",
    "recovery_codes",
    "failed_login_count",
    "locked_until",
    "idp_subject",
}


def test_a_user_resource_has_the_required_shape(db, tenant_a):
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        email="shape@example.com",
        full_name="Shape Check",
        role=UserRole.AGENT,
        password_hash="not-a-real-hash",
        # Column defaults are applied by the database at INSERT, so a row built
        # in Python has to state the flag it is being tested for.
        is_active=True,
    )
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    user.created_at = created
    user.updated_at = created

    resource = schemas.user_resource(
        user=user, base_url=BASE, external_id="dir-42", email_verified=True
    )

    assert set(resource) <= USER_KEYS, set(resource) - USER_KEYS
    assert set(resource) & LEAKY == set()
    assert resource["schemas"] == [schemas.SCIM_USER_SCHEMA]
    assert resource["id"] == str(user.id)
    assert isinstance(resource["id"], str), "SCIM ids are strings"
    assert resource["userName"] == "shape@example.com"
    assert resource["externalId"] == "dir-42"
    assert resource["active"] is True
    assert resource["name"]["formatted"] == "Shape Check"
    assert resource["meta"]["resourceType"] == "User"
    assert resource["meta"]["location"] == f"{BASE}/Users/{user.id}"
    assert resource["meta"]["created"] == "2026-01-02T03:04:05Z"
    assert resource["meta"]["lastModified"] == "2026-01-02T03:04:05Z"
    assert resource["emails"] == [
        {"value": "shape@example.com", "primary": True, "type": "work", "verified": True}
    ]


def test_a_user_with_no_display_name_falls_back_to_the_login_address(db, tenant_a):
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        email="no-name@example.com",
        full_name="",
        role=UserRole.AGENT,
        password_hash="x",
        is_active=True,
    )
    resource = schemas.user_resource(user=user, base_url=BASE)
    assert resource["name"]["formatted"] == "no-name"
    assert resource["displayName"] == "no-name"
    assert resource["externalId"] is None, "an absent externalId is null, not empty"
    assert resource["emails"][0]["verified"] is False
    assert "groups" not in resource, "no groups means no attribute, not an empty list"


def test_a_deactivated_user_is_reported_as_inactive(db, tenant_a):
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        email="gone@example.com",
        role=UserRole.AGENT,
        password_hash="x",
        is_active=False,
    )
    assert schemas.user_resource(user=user, base_url=BASE)["active"] is False


def test_groups_on_a_user_are_value_and_display_only(db, tenant_a):
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        email="member@example.com",
        role=UserRole.AGENT,
        password_hash="x",
        is_active=True,
    )
    resource = schemas.user_resource(
        user=user,
        base_url=BASE,
        groups=[{"value": "g-1", "display": "Managers"}],
    )
    assert resource["groups"] == [{"value": "g-1", "display": "Managers"}]
    assert isinstance(resource["groups"], list)


def test_a_group_resource_carries_members_and_the_mapped_role_honestly(db, tenant_a):
    group = SCIMGroupMapping(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        external_id="grp-1",
        display_name="Managers",
        member_user_ids=[],
    )
    plain = schemas.group_resource(group=group, base_url=BASE, members=[])
    assert set(plain) <= GROUP_KEYS, set(plain) - GROUP_KEYS
    assert set(plain) & LEAKY == set()
    assert plain["displayName"] == "Managers"
    assert plain["externalId"] == "grp-1"
    assert plain["schemas"] == [schemas.SCIM_GROUP_SCHEMA]
    assert plain["meta"]["resourceType"] == "Group"
    assert plain["meta"]["location"] == f"{BASE}/Groups/{group.id}"
    assert "urn:voxdesk:params:scim:schemas:extension:role" not in plain, (
        "a group that grants no role must not claim one"
    )

    mapped = schemas.group_resource(
        group=group, base_url=BASE, mapped_role="manager"
    )
    assert mapped["urn:voxdesk:params:scim:schemas:extension:role"] == {"role": "manager"}

    # Without a members argument the attribute is omitted entirely, which is
    # what lets a list response stay small.
    assert "members" not in schemas.group_resource(group=group, base_url=BASE)


def test_a_list_response_reports_paging():
    body = schemas.list_response(
        [{"id": "1"}], total=57, start_index=21, items_per_page=20
    )
    assert body["schemas"] == [schemas.SCIM_LIST_SCHEMA]
    assert body["totalResults"] == 57
    assert body["startIndex"] == 21
    assert body["itemsPerPage"] == 20
    assert body["Resources"] == [{"id": "1"}]


def test_an_error_document_states_the_status_as_a_string():
    """RFC 7644 §3.12 says the error status is a string; some clients parse it
    strictly, and `"403"` is what they accept."""
    body = schemas.error_response(detail="Nope", status=403, scim_type="mutability")
    assert body == {
        "schemas": [schemas.SCIM_ERROR_SCHEMA],
        "detail": "Nope",
        "status": "403",
        "scimType": "mutability",
    }

    without_type = schemas.error_response(detail="Nope", status=403)
    assert "scimType" not in without_type, "no scimType is invented when there is none"


def test_the_service_provider_config_promises_only_what_is_implemented():
    config = schemas.service_provider_config(base_url=BASE)
    assert config["schemas"] == [schemas.SCIM_SERVICE_PROVIDER_SCHEMA]
    assert config["patch"]["supported"] is True
    assert config["filter"]["supported"] is True
    assert config["filter"]["maxResults"] >= 1
    assert config["bulk"]["supported"] is False
    assert config["changePassword"]["supported"] is False
    assert config["sort"]["supported"] is False
    assert config["etag"]["supported"] is False

    schemes = config["authenticationSchemes"]
    assert len(schemes) == 1
    assert schemes[0]["type"] == "oauthbearertoken"
    assert schemes[0]["primary"] is True
    assert schemes[0]["specUri"].startswith("https://www.rfc-editor.org/")
    assert config["meta"]["resourceType"] == "ServiceProviderConfig"
    assert config["meta"]["location"] == f"{BASE}/ServiceProviderConfig"


def test_resource_types_match_the_endpoints_that_exist():
    types = {entry["id"]: entry for entry in schemas.resource_types(base_url=BASE)}
    assert set(types) == {"User", "Group"}
    assert types["User"]["endpoint"] == "/Users"
    assert types["User"]["schema"] == schemas.SCIM_USER_SCHEMA
    assert types["Group"]["endpoint"] == "/Groups"
    assert types["Group"]["schema"] == schemas.SCIM_GROUP_SCHEMA
    for entry in types.values():
        assert entry["schemas"] == [schemas.SCIM_RESOURCE_TYPE_SCHEMA]
        assert entry["meta"]["resourceType"] == "ResourceType"
        assert entry["meta"]["location"].startswith(BASE)


def test_the_declared_schemas_describe_attributes_that_are_actually_supported():
    declared = {entry["name"]: entry for entry in schemas.schemas(base_url=BASE)}
    assert set(declared) == {"User", "Group"}

    user_attributes = {attr["name"] for attr in declared["User"]["attributes"]}
    assert {"userName", "externalId", "displayName", "emails", "active", "groups"} <= user_attributes
    assert declared["User"]["id"] == schemas.SCIM_USER_SCHEMA

    group_attributes = {attr["name"] for attr in declared["Group"]["attributes"]}
    assert {"displayName", "externalId", "members"} <= group_attributes

    # `userName` is the one attribute an IdP cannot omit, and it is the one we
    # match on, so it must be declared required and server-unique.
    username = next(a for a in declared["User"]["attributes"] if a["name"] == "userName")
    assert username["required"] is True
    assert username["uniqueness"] == "server"

    # Nothing declared is writable that we refuse to write.
    for entry in declared.values():
        for attr in entry["attributes"]:
            assert attr["mutability"] in {"readOnly", "readWrite", "immutable", "writeOnly"}
            assert attr["returned"] in {"always", "never", "default", "request"}


async def test_a_filter_id_that_is_not_a_uuid_is_an_invalid_value():
    with pytest.raises(SCIMInvalidValue):
        schemas.parse_uuid("not-a-uuid")
    with pytest.raises(SCIMInvalidValue):
        schemas.parse_uuid("")

    value = uuid.uuid4()
    assert schemas.parse_uuid(str(value)) == value


async def test_a_real_user_from_the_database_renders_without_internals(db, tenant_a):
    """The unit tests above build detached rows; this one renders a row the way
    the service does, so a mapped-only attribute cannot slip through."""
    user = await make_user(db, tenant_a, UserRole.MANAGER, email="real@example.com")
    user.password_changed_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db.commit()

    resource = schemas.user_resource(user=user, base_url=BASE, email_verified=False)
    assert set(resource) <= USER_KEYS
    assert resource["userName"] == user.email
    rendered = repr(resource)
    assert user.password_hash not in rendered
    assert str(user.tenant_id) not in rendered, "the tenant is implied by the URL"
