"""SCIM Users: the RFC 7644 surface an identity provider actually drives.

Every assertion here is about a rule an IdP integration depends on:

* ``POST`` answers **201 with a ``Location``** and the created resource — a
  provisioning client that cannot read back what it created will create it twice;
* a duplicate ``userName`` is a **409 with ``scimType: uniqueness``**, which is
  the one error an idempotent client is expected to handle;
* the list endpoint paginates with ``startIndex``/``count`` and reports the true
  ``totalResults`` for the whole filter, not for the page;
* ``filter`` is parsed and bound to columns (never evaluated), supports the
  operators the RFC names, and an attribute that is not filterable is refused;
* ``PUT`` replaces, ``PATCH`` applies operations, and an unsupported path is
  refused rather than ignored;
* ``DELETE`` deactivates *and* deletes, so nothing can be orphaned;
* ``externalId`` round-trips, because that is the only handle the IdP has on a
  user it did not name with an address;
* every deactivation kills the user's sessions immediately.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.auth import password as pw
from app.db.models import User, UserRole
from tests.auth.scim.conftest import user_payload
from tests.conftest import auth_headers, login

pytestmark = pytest.mark.asyncio

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"


async def test_create_returns_201_with_a_location_and_the_resource(client, db, scim):
    response = await scim.post("Users", user_payload("grace@acme.example", displayName="Grace Hopper"))
    assert response.status_code == 201, response.text
    assert response.headers["content-type"].startswith("application/scim+json")

    body = response.json()
    assert body["schemas"] == [USER_SCHEMA]
    assert body["userName"] == "grace@acme.example"
    assert body["displayName"] == "Grace Hopper"
    assert body["active"] is True
    assert body["id"]
    assert response.headers["location"].endswith(f"/Users/{body['id']}")
    assert body["meta"]["location"].endswith(f"/Users/{body['id']}")
    assert body["meta"]["resourceType"] == "User"

    stored = (await db.execute(select(User).where(User.email == "grace@acme.example"))).scalar_one()
    assert stored.is_active is True
    assert stored.full_name == "Grace Hopper"
    assert stored.password_hash, "a provisioned user must not have an empty password"


async def test_the_created_user_cannot_be_signed_into_without_a_password(client, db, scim):
    """Provisioning creates an account, not a credential: the password is unguessable."""
    await scim.post("Users", user_payload("nopass@acme.example"))
    refused = await login(client, "nopass@acme.example", "Correct-Horse-Battery-9!")
    assert refused.status_code == 401, refused.text


async def test_a_duplicate_user_name_is_a_409_with_uniqueness(client, db, scim):
    first = await scim.post("Users", user_payload("dup@acme.example"))
    assert first.status_code == 201, first.text

    again = await scim.post("Users", user_payload("dup@acme.example"))
    assert again.status_code == 409, again.text
    body = again.json()
    assert body["scimType"] == "uniqueness"
    assert body["schemas"][0].endswith(":Error")

    rows = (await db.execute(select(User).where(User.email == "dup@acme.example"))).scalars().all()
    assert len(rows) == 1


async def test_a_user_name_that_is_not_an_address_is_refused(client, scim):
    refused = await scim.post("Users", user_payload("just-a-name"))
    assert refused.status_code == 400, refused.text
    assert refused.json()["scimType"] == "invalidValue"

    missing = await scim.post("Users", {"schemas": [USER_SCHEMA]})
    assert missing.status_code == 400, missing.text


async def test_external_id_round_trips(client, db, scim):
    created = await scim.post("Users", user_payload("ext@acme.example", externalId="okta-4711"))
    assert created.status_code == 201, created.text
    assert created.json()["externalId"] == "okta-4711"

    fetched = await scim.get(f"Users/{created.json()['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["externalId"] == "okta-4711"


async def test_listing_reports_the_total_and_paginates(client, db, scim):
    before = await scim.get("Users", count=1)
    baseline = before.json()["totalResults"]
    for index in range(5):
        created = await scim.post("Users", user_payload(f"page{index}@acme.example"))
        assert created.status_code == 201, created.text

    first = await scim.get("Users", count=2)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["schemas"][0].endswith(":ListResponse")
    assert body["totalResults"] == baseline + 5, "the total covers the whole filter, not the page"
    assert body["itemsPerPage"] == 2
    assert body["startIndex"] == 1
    assert len(body["Resources"]) == 2

    second = await scim.get("Users", count=2, startIndex=4)
    assert second.json()["startIndex"] == 4
    assert len(second.json()["Resources"]) == 2
    ids = {resource["id"] for resource in body["Resources"]}
    assert not ids & {resource["id"] for resource in second.json()["Resources"]}

    beyond = await scim.get("Users", count=2, startIndex=50)
    assert beyond.status_code == 200
    assert beyond.json()["Resources"] == []
    assert beyond.json()["totalResults"] == baseline + 5


async def test_filtering_by_user_name(client, db, scim):
    await scim.post("Users", user_payload("alpha@acme.example"))
    await scim.post("Users", user_payload("beta@acme.example"))

    filtered = await scim.get("Users", filter='userName eq "alpha@acme.example"')
    assert filtered.status_code == 200, filtered.text
    body = filtered.json()
    assert body["totalResults"] == 1
    assert body["Resources"][0]["userName"] == "alpha@acme.example"


async def test_filtering_by_active(client, db, scim):
    created = await scim.post("Users", user_payload("inactive@acme.example"))
    await scim.patch(
        f"Users/{created.json()['id']}", {"Operations": [{"op": "replace", "path": "active", "value": False}]}
    )
    await scim.post("Users", user_payload("active@acme.example"))

    inactive = await scim.get("Users", filter="active eq false")
    assert inactive.status_code == 200, inactive.text
    assert [resource["userName"] for resource in inactive.json()["Resources"]] == ["inactive@acme.example"]


async def test_a_filter_naming_an_unfilterable_attribute_is_refused(client, scim):
    refused = await scim.get("Users", filter='password eq "x"')
    assert refused.status_code == 400, refused.text
    assert refused.json()["scimType"] == "invalidFilter"

    malformed = await scim.get("Users", filter="userName eq")
    assert malformed.status_code == 400, malformed.text


async def test_a_sql_shaped_filter_is_data_not_syntax(client, db, scim):
    """The filter grammar is parsed, so an injection attempt is just a bad value."""
    await scim.post("Users", user_payload("safe@acme.example"))

    hostile = await scim.get("Users", filter='userName eq "x\"; DROP TABLE users; --"')
    assert hostile.status_code in (200, 400), hostile.text
    if hostile.status_code == 200:
        assert hostile.json()["totalResults"] == 0

    still_there = (await db.execute(select(User).where(User.email == "safe@acme.example"))).scalar_one_or_none()
    assert still_there is not None


async def test_put_replaces_the_resource(client, db, scim):
    created = await scim.post("Users", user_payload("put@acme.example", displayName="Before"))
    user_id = created.json()["id"]

    replaced = await scim.put(
        f"Users/{user_id}",
        {
            "schemas": [USER_SCHEMA],
            "userName": "put@acme.example",
            "displayName": "After",
            "active": True,
            "externalId": "put-4711",
        },
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["displayName"] == "After"
    assert replaced.json()["externalId"] == "put-4711"

    stored = (
        await db.execute(select(User).where(User.id == uuid.UUID(user_id)).execution_options(populate_existing=True))
    ).scalar_one()
    assert stored.full_name == "After"


async def test_patch_deactivates_and_revokes_sessions(client, db, scim):
    created = await scim.post("Users", user_payload("deactivate@acme.example"))
    user_id = created.json()["id"]
    stored = (await db.execute(select(User).where(User.id == uuid.UUID(user_id)))).scalar_one()

    # Give the user a live session, so the revocation has something to revoke.
    stored.password_hash = pw.hash_password("Correct-Horse-Battery-9!")
    await db.commit()
    headers = await auth_headers(client, stored)
    assert (await client.get("/api/sessions", headers=headers)).status_code == 200

    patched = await scim.patch(
        f"Users/{user_id}",
        {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
         "Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["active"] is False

    afterwards = (
        await db.execute(select(User).where(User.id == uuid.UUID(user_id)).execution_options(populate_existing=True))
    ).scalar_one()
    assert afterwards.is_active is False
    assert afterwards.token_version >= 1

    # The token that was working a moment ago is refused now.
    assert (await client.get("/api/sessions", headers=headers)).status_code == 401

    refused_login = await login(client, "deactivate@acme.example")
    assert refused_login.status_code in (401, 403), refused_login.text


async def test_patch_without_a_path_uses_the_value_object(client, db, scim):
    created = await scim.post("Users", user_payload("valueobj@acme.example"))
    user_id = created.json()["id"]

    patched = await scim.patch(
        f"Users/{user_id}",
        {"Operations": [{"op": "replace", "value": {"displayName": "From Value"}}]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["displayName"] == "From Value"


async def test_an_unsupported_patch_path_is_refused_not_ignored(client, scim):
    created = await scim.post("Users", user_payload("patchpath@acme.example"))
    refused = await scim.patch(
        f"Users/{created.json()['id']}",
        {"Operations": [{"op": "add", "path": "nickName", "value": "Nick"}]},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["scimType"] == "invalidValue"

    remove = await scim.patch(
        f"Users/{created.json()['id']}",
        {"Operations": [{"op": "remove", "path": "displayName"}]},
    )
    assert remove.status_code == 400, remove.text


async def test_a_patch_without_operations_is_refused(client, scim):
    created = await scim.post("Users", user_payload("noop@acme.example"))
    refused = await scim.patch(f"Users/{created.json()['id']}", {"Operations": []})
    assert refused.status_code == 400, refused.text


async def test_delete_deprovisions_and_removes_the_user(client, db, scim):
    created = await scim.post("Users", user_payload("remove@acme.example"))
    user_id = created.json()["id"]

    deleted = await scim.delete(f"Users/{user_id}")
    assert deleted.status_code == 204, deleted.text

    assert (await scim.get(f"Users/{user_id}")).status_code == 404
    assert (
        await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    ).scalar_one_or_none() is None

    missing = await scim.delete(f"Users/{user_id}")
    assert missing.status_code == 404, missing.text


async def test_deleting_the_last_active_owner_is_refused(client, db, scim, owner_a):
    """A provisioning client must not be able to lock a workspace out."""
    refused = await scim.delete(f"Users/{owner_a.id}")
    assert refused.status_code == 403, refused.text

    still = (
        await db.execute(select(User).where(User.id == owner_a.id).execution_options(populate_existing=True))
    ).scalar_one()
    assert still.is_active is True
    assert still.role is UserRole.OWNER


async def test_an_unknown_user_id_is_a_404_and_a_foreign_one_is_too(client, db, owner_b, scim):
    assert (await scim.get(f"Users/{uuid.uuid4()}")).status_code == 404
    assert (await scim.get(f"Users/{owner_b.id}")).status_code == 404
    assert (await scim.delete(f"Users/{owner_b.id}")).status_code == 404
    assert (
        await scim.get(f"Users/{owner_b.id}")
    ).json()["detail"] == "User not found"

    unchanged = (
        await db.execute(select(User).where(User.id == owner_b.id).execution_options(populate_existing=True))
    ).scalar_one()
    assert unchanged.is_active is True


async def test_a_malformed_user_id_is_a_400_and_an_unknown_one_is_a_404(client, scim):
    """A malformed identifier is the client's mistake; a well-formed one we do not
    hold is a missing resource. The two are distinguishable, which is what an IdP
    needs to decide between fixing its request and dropping a stale record."""
    malformed = await scim.get("Users/not-a-uuid")
    assert malformed.status_code == 400, malformed.text
    assert malformed.json()["scimType"] == "invalidValue"

    unknown = await scim.get(f"Users/{uuid.uuid4()}")
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["scimType"] == "notFound"


async def test_the_resource_reports_the_tenant_scoped_location(client, scim, scim_connection):
    created = await scim.post("Users", user_payload("location@acme.example"))
    body = created.json()
    assert str(body["meta"]["location"]).endswith(f"/scim/v2/{scim_connection['id']}/Users/{body['id']}")
    assert body["meta"]["created"]
    assert body["meta"]["lastModified"]


async def test_display_name_falls_back_to_the_local_part(client, db, scim):
    created = await scim.post("Users", {"userName": "plain@acme.example"})
    assert created.status_code == 201, created.text
    assert created.json()["displayName"] == "plain"


async def test_an_address_from_another_workspace_cannot_be_provisioned(client, db, owner_b, scim):
    """Provisioning must not be able to reach across a tenant boundary."""
    refused = await scim.post("Users", user_payload(owner_b.email))
    assert refused.status_code == 409, refused.text
    assert "another workspace" in refused.text.lower()

    accounts = (
        await db.execute(select(User).where(User.email == owner_b.email))
    ).scalars().all()
    assert len(accounts) == 1
