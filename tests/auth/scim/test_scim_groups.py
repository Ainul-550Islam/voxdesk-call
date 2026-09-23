"""SCIM Groups and the role they grant.

Groups are how an IdP expresses "these people are managers". The rules asserted
here are the ones that keep that from becoming a privilege-escalation path:

* a group is created, read, replaced, patched and deleted, with membership
  reported back as ``members`` and the granted role carried as an extension
  attribute so an administrator can see it from the IdP's own view;
* a group whose name matches the connection's group mapping grants that role to
  its members — and only a role the mapping names, never an owner;
* a member keeps the highest role any of their groups grants, so a partial sync
  cannot silently demote someone;
* group names are unique per connection for the same reason user names are;
* groups are tenant-scoped: another workspace's group id is a 404, and a
  credential without the groups scope cannot touch groups at all;
* the group list paginates and filters like the user list.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models import User, UserRole
from tests.auth.scim.conftest import user_payload

pytestmark = pytest.mark.asyncio

GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"


async def _user_id(scim, email: str) -> str:
    created = await scim.post("Users", user_payload(email))
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def _group(scim, display_name: str, member_ids: list[str], **overrides) -> dict:
    body = {
        "schemas": [GROUP_SCHEMA],
        "displayName": display_name,
        "members": [{"value": member_id} for member_id in member_ids],
    }
    body.update(overrides)
    created = await scim.post("Groups", body)
    assert created.status_code == 201, created.text
    return created.json()


async def test_a_group_is_created_with_its_members(client, db, scim):
    first = await _user_id(scim, "member-one@acme.example")
    second = await _user_id(scim, "member-two@acme.example")

    body = await _group(scim, "Support", [first, second], externalId="grp-1")
    assert body["schemas"] == [GROUP_SCHEMA]
    assert body["displayName"] == "Support"
    assert body["externalId"] == "grp-1"
    assert {member["value"] for member in body["members"]} == {first, second}
    assert body["meta"]["resourceType"] == "Group"

    fetched = await scim.get(f"Groups/{body['id']}")
    assert fetched.status_code == 200, fetched.text
    assert {member["value"] for member in fetched.json()["members"]} == {first, second}
    assert all(member.get("display") for member in fetched.json()["members"])


async def test_a_group_with_a_duplicate_name_updates_the_existing_one(client, db, scim):
    """IdPs re-POST a group on every sync; treating that as a create would
    fragment membership across duplicate rows."""
    first = await _group(scim, "Support", [])
    second = await _group(scim, "Support", [])

    assert second["id"] == first["id"]
    listed = await scim.get("Groups")
    assert listed.json()["totalResults"] == 1


async def test_a_group_without_a_display_name_is_refused(client, scim):
    refused = await scim.post("Groups", {"schemas": [GROUP_SCHEMA]})
    assert refused.status_code == 400, refused.text
    assert refused.json()["scimType"] == "invalidValue"


async def test_a_group_member_that_is_not_a_user_id_is_refused(client, scim):
    refused = await scim.post(
        "Groups",
        {"displayName": "Bad", "members": [{"value": "someone@acme.example"}]},
    )
    assert refused.status_code == 400, refused.text
    assert "valid user id" in refused.text.lower()


async def test_put_replaces_membership(client, db, scim):
    keep = await _user_id(scim, "keep@acme.example")
    drop = await _user_id(scim, "drop@acme.example")
    join = await _user_id(scim, "join@acme.example")
    group = await _group(scim, "Rotating", [keep, drop])

    replaced = await scim.put(
        f"Groups/{group['id']}",
        {"schemas": [GROUP_SCHEMA], "displayName": "Rotating", "members": [{"value": keep}, {"value": join}]},
    )
    assert replaced.status_code == 200, replaced.text
    assert {member["value"] for member in replaced.json()["members"]} == {keep, join}

    stored = (
        await db.execute(
            select(User).where(User.id.in_([uuid.UUID(keep), uuid.UUID(drop), uuid.UUID(join)]))
        )
    ).scalars().all()
    assert len(stored) == 3


async def test_patch_updates_membership(client, db, scim):
    member = await _user_id(scim, "patched-member@acme.example")
    group = await _group(scim, "Patchable", [])

    patched = await scim.patch(
        f"Groups/{group['id']}",
        {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "add", "path": "members", "value": [{"value": member}]}],
        },
    )
    assert patched.status_code == 200, patched.text
    assert [entry["value"] for entry in patched.json()["members"]] == [member]

    renamed = await scim.patch(
        f"Groups/{group['id']}",
        {"Operations": [{"op": "replace", "path": "displayName", "value": "Renamed"}]},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["displayName"] == "Renamed"


async def test_delete_removes_the_group_and_leaves_members_alone(client, db, scim):
    member = await _user_id(scim, "survivor@acme.example")
    group = await _group(scim, "Temporary", [member])

    assert (await scim.delete(f"Groups/{group['id']}")).status_code == 204
    assert (await scim.get(f"Groups/{group['id']}")).status_code == 404

    still_there = (
        await db.execute(select(User).where(User.id == uuid.UUID(member)))
    ).scalar_one_or_none()
    assert still_there is not None
    assert still_there.is_active is True


async def test_a_known_group_grants_its_mapped_role(client, db, scim, scim_connection, owner_a):
    """The connection maps "Agents" to manager; SCIM makes it real."""
    from tests.auth.sso.providers import set_mappings

    await set_mappings(
        client, owner_a, scim_connection, group_mapping={"Agents": "manager"}
    )

    member = await _user_id(scim, "promoted@acme.example")
    stored = (
        await db.execute(select(User).where(User.id == uuid.UUID(member)).execution_options(populate_existing=True))
    ).scalar_one()
    assert stored.role is UserRole.AGENT

    group = await _group(scim, "Agents", [member])
    assert group["urn:voxdesk:params:scim:schemas:extension:role"]["role"] == "manager"

    promoted = (
        await db.execute(select(User).where(User.id == uuid.UUID(member)).execution_options(populate_existing=True))
    ).scalar_one()
    assert promoted.role is UserRole.MANAGER


async def test_an_unmapped_group_grants_nothing(client, db, scim, scim_connection, owner_a):
    from tests.auth.sso.providers import set_mappings

    await set_mappings(client, owner_a, scim_connection, group_mapping={"Agents": "manager"})

    member = await _user_id(scim, "unmapped@acme.example")
    group = await _group(scim, "Contractors", [member])
    assert "urn:voxdesk:params:scim:schemas:extension:role" not in group

    stored = (
        await db.execute(select(User).where(User.id == uuid.UUID(member)).execution_options(populate_existing=True))
    ).scalar_one()
    assert stored.role is UserRole.AGENT


async def test_a_group_cannot_demote_a_member_below_another_groups_role(
    client, db, scim, scim_connection, owner_a
):
    """A partial sync removes someone from one group at a time; the highest role
    any of their groups grants is the one they keep."""
    from tests.auth.sso.providers import set_mappings

    await set_mappings(
        client, owner_a, scim_connection, group_mapping={"Managers": "manager", "Agents": "agent"}
    )

    member = await _user_id(scim, "both@acme.example")
    await _group(scim, "Managers", [member])
    await _group(scim, "Agents", [member])

    roles = (
        await db.execute(select(User.role).where(User.id == uuid.UUID(member)).execution_options(populate_existing=True))
    ).scalar_one()
    assert roles is UserRole.MANAGER


async def test_a_group_sync_never_lowers_a_role_it_did_not_grant(
    client, db, scim, scim_connection, owner_a
):
    """A directory sync is not how access is revoked: removing someone from a
    mapped group leaves their role alone, which is asserted here so the rule is
    a decision rather than an accident."""
    from tests.auth.sso.providers import set_mappings

    await set_mappings(client, owner_a, scim_connection, group_mapping={"Managers": "manager"})

    member = await _user_id(scim, "sticky@acme.example")
    group = await _group(scim, "Managers", [member])

    def role_of(user_id):
        return select(User.role).where(User.id == uuid.UUID(user_id)).execution_options(
            populate_existing=True
        )

    assert (
        await db.execute(role_of(member))
    ).scalar_one() is UserRole.MANAGER

    emptied = await scim.put(
        f"Groups/{group['id']}",
        {"schemas": [GROUP_SCHEMA], "displayName": "Managers", "members": []},
    )
    assert emptied.status_code == 200, emptied.text
    assert (await db.execute(role_of(member))).scalar_one() is UserRole.MANAGER


async def test_a_role_set_by_an_administrator_is_not_lowered_by_a_sync(
    client, db, scim, scim_connection, owner_a
):
    from tests.auth.sso.providers import set_mappings

    await set_mappings(client, owner_a, scim_connection, group_mapping={"Agents": "agent"})

    member = await _user_id(scim, "admin-set@acme.example")
    stored = (
        await db.execute(select(User).where(User.id == uuid.UUID(member)).execution_options(populate_existing=True))
    ).scalar_one()
    stored.role = UserRole.ADMIN
    await db.commit()

    await _group(scim, "Agents", [member])

    after = (
        await db.execute(select(User).where(User.id == uuid.UUID(member)).execution_options(populate_existing=True))
    ).scalar_one()
    assert after.role is UserRole.ADMIN, "an agent mapping cannot demote an admin"


async def test_a_group_never_grants_ownership(client, db, scim, scim_connection, owner_a):
    """Even if the mapping names owner, ownership is not granted by a directory
    sync — the connection refuses to store such a mapping at all."""
    from tests.conftest import auth_headers

    refused = await client.put(
        f"/api/sso/connections/{scim_connection['id']}/mappings",
        json={"group_mapping": {"Owners": "owner"}},
        headers=await auth_headers(client, owner_a),
    )
    assert refused.status_code == 400, refused.text
    assert "owner" in refused.text.lower()

    member = await _user_id(scim, "not-an-owner@acme.example")
    group = await _group(scim, "Owners", [member])
    assert group.get("urn:voxdesk:params:scim:schemas:extension:role", {}).get("role") in ("", None)

    stored = (
        await db.execute(select(User).where(User.id == uuid.UUID(member)).execution_options(populate_existing=True))
    ).scalar_one()
    assert stored.role is not UserRole.OWNER


async def test_an_existing_owner_is_never_demoted_by_a_group(client, db, scim, owner_a, scim_connection, tenant_a):
    from tests.auth.sso.providers import set_mappings

    await set_mappings(client, owner_a, scim_connection, group_mapping={"Agents": "agent"})
    group = await _group(scim, "Agents", [str(owner_a.id)])

    team = (
        await db.execute(select(User).where(User.email == owner_a.email).execution_options(populate_existing=True))
    ).scalar_one()
    assert team.role is UserRole.OWNER
    assert group["urn:voxdesk:params:scim:schemas:extension:role"]["role"] == "agent"


async def test_groups_are_listed_with_paging(client, db, scim):
    for index in range(3):
        await _group(scim, f"Team {index}", [])

    body = (await scim.get("Groups", count=2)).json()
    assert body["totalResults"] == 3
    assert body["itemsPerPage"] == 2
    assert len(body["Resources"]) == 2

    filtered = await scim.get("Groups", filter='displayName sw "Team 1"')
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["totalResults"] == 1


async def test_an_unknown_group_id_is_a_404_and_a_foreign_one_is_invisible(
    client, db, scim, owner_b, scim_connection
):
    assert (await scim.get(f"Groups/{uuid.uuid4()}")).status_code == 404

    group = await _group(scim, "Private", [])
    other = scim.as_token("not-a-real-token")
    assert (await other.get(f"Groups/{group['id']}")).status_code == 401

    # Another workspace cannot see the group, and asking for a group id that
    # belongs to it — owner_b's tenant — is a miss here, not a peek across.
    assert (await scim.get(f"Groups/{owner_b.tenant_id}")).status_code == 404

    from tests.conftest import auth_headers

    headers_b = await auth_headers(client, owner_b)
    listed_b = await client.get(
        f"/scim/v2/{scim_connection['id']}/Groups",
        headers={**headers_b, "content-type": "application/scim+json"},
    )
    assert listed_b.status_code in (401, 403, 404), listed_b.text


async def test_a_credential_without_the_groups_scope_cannot_manage_groups(
    client, db, owner_a, scim_connection
):
    from app.auth.identity.scim.service import SCIM_SCOPE_USERS
    from tests.conftest import auth_headers

    created = await client.post(
        "/api/scim/credentials",
        json={"connection_id": scim_connection["id"], "scopes": [SCIM_SCOPE_USERS]},
        headers=await auth_headers(client, owner_a),
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]

    headers = {"Authorization": f"Bearer {token}", "content-type": "application/scim+json"}
    refused = await client.get(f"/scim/v2/{scim_connection['id']}/Groups", headers=headers)
    assert refused.status_code == 403, refused.text

    allowed = await client.get(f"/scim/v2/{scim_connection['id']}/Users", headers=headers)
    assert allowed.status_code == 200, allowed.text
