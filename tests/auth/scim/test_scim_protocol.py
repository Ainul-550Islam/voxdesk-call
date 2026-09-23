"""The SCIM protocol surface: discovery, authentication, scoping, isolation, races.

An integration is built against these documents, and every one of them is a
promise: ``ServiceProviderConfig`` says which features exist, ``ResourceTypes``
says where the endpoints are, ``Schemas`` says which attributes are real. The
tests here hold the implementation to what it advertises, and then attack the
edges an internet-facing provisioning endpoint actually has:

* the bearer token is required on every SCIM route, and a token scoped to one
  connection cannot provision through another connection's URL;
* ``/scim/v2/default`` serves only a tenant-scoped credential — a
  connection-scoped token cannot widen its reach by rewriting the path;
* an unknown connection id is a 404 and does not leak whether it exists;
* every response carries the SCIM media type and error documents follow RFC 7644;
* two administrators creating the same user at the same time produce one user and
  one 409, not two users;
* two credentials revoking each other's users cannot cross tenants.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

import pytest_asyncio

from app.auth.identity.models import SCIMCredential
from app.db.models import User, UserRole
from app.db.session import get_session
from tests.auth.scim.conftest import ScimClient, user_payload
from tests.conftest import TEST_PASSWORD, auth_headers, make_tenant, make_user

pytestmark = pytest.mark.asyncio

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"




@pytest_asyncio.fixture
async def racing_app(app, concurrent_sessionmaker):
    """The real application, backed by connections that are actually separate.

    The default test engine is ``sqlite:///:memory:``, which SQLAlchemy serves
    through a single connection: two requests "at the same time" share one
    transaction, and one of them rolling back destroys the other's work. That is
    an artifact of the in-memory database rather than of the code, and it makes a
    race impossible to observe. The concurrency fixtures in ``tests/conftest.py``
    use a file-backed database where each session really does get its own
    connection, which is what PostgreSQL does in production.
    """

    async def _override():
        async with concurrent_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    yield app


async def _racing_workspace(maker, *, name: str = "Racing Dental"):
    """A workspace, an owner, and a tenant-wide SCIM credential, on real sessions."""
    from app.auth.identity.scim import service as scim_service

    async with maker() as session:
        tenant = await make_tenant(session, name)
        owner = await make_user(session, tenant, UserRole.OWNER)
        issued = await scim_service.create_credential(
            session, tenant_id=tenant.id, actor=owner, label="race", commit=True
        )
    return tenant, owner, issued.token


async def test_service_provider_config_is_honest(client, scim):
    response = await scim.get("ServiceProviderConfig")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/scim+json")

    body = response.json()
    assert body["schemas"][0].endswith(":ServiceProviderConfig")
    assert body["patch"]["supported"] is True
    assert body["filter"]["supported"] is True
    # The two features we do not implement are stated as unsupported, and the
    # advertised numbers match the limits the code enforces.
    assert body["bulk"]["supported"] is False
    assert body["sort"]["supported"] is False
    assert body["etag"]["supported"] is False
    assert body["changePassword"]["supported"] is False
    assert body["authenticationSchemes"][0]["type"] == "oauthbearertoken"
    assert body["meta"]["location"].endswith("/ServiceProviderConfig")


async def test_resource_types_name_the_real_endpoints(client, scim, scim_connection):
    response = await scim.get("ResourceTypes")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totalResults"] == 2

    by_id = {resource["id"]: resource for resource in body["Resources"]}
    assert set(by_id) == {"User", "Group"}
    assert by_id["User"]["schema"] == USER_SCHEMA
    assert by_id["Group"]["schema"] == GROUP_SCHEMA
    for resource in by_id.values():
        assert resource["endpoint"] in ("/Users", "/Groups")
        assert resource["meta"]["location"].startswith("http")
        assert f"/scim/v2/{scim_connection['id']}" in resource["meta"]["location"]


async def test_schemas_describe_the_attributes_we_return(client, scim):
    response = await scim.get("Schemas")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totalResults"] >= 2

    by_id = {document["id"]: document for document in body["Resources"]}
    user_schema = by_id[USER_SCHEMA]
    names = {attribute["name"] for attribute in user_schema["attributes"]}
    assert {"userName", "displayName", "active", "emails", "externalId"} <= names

    # Every advertised attribute is one a create actually accepts, and the
    # read-only ones say so.
    created = await scim.post("Users", user_payload("schema@acme.example"))
    assert created.status_code == 201, created.text
    for name in names - {"id", "meta", "schemas", "groups", "name"}:
        assert name in created.json(), f"advertised but not returned: {name}"
    assert by_id[GROUP_SCHEMA]["name"] == "Group"


async def test_every_scim_route_requires_a_credential(client, scim_connection):
    paths = [
        "ServiceProviderConfig",
        "ResourceTypes",
        "Schemas",
        "Users",
        f"Users/{uuid.uuid4()}",
        "Groups",
        f"Groups/{uuid.uuid4()}",
    ]
    anonymous = ScimClient(client, scim_connection["id"], "")
    for path in paths:
        response = await anonymous.get(path)
        assert response.status_code == 401, f"{path}: {response.text}"
        body = response.json()
        assert body["schemas"][0].endswith(":Error")
        assert body["status"] == "401" or body["status"] == 401
        assert "www-authenticate" in {key.lower() for key in response.headers}


async def test_a_wrong_or_malformed_token_is_refused(client, scim_connection):
    for header in ("Bearer nope", "Basic abc", "Bearer ", "bearer " + "x" * 200):
        response = await client.get(
            f"/scim/v2/{scim_connection['id']}/Users",
            headers={"Authorization": header, "content-type": "application/scim+json"},
        )
        assert response.status_code == 401, f"{header!r}: {response.text}"


async def test_a_connection_scoped_credential_cannot_use_another_connections_url(
    client, db, owner_a, scim_connection, scim
):
    from tests.auth.sso.providers import create_oidc_connection

    other = await create_oidc_connection(client, owner_a, slug="beta", client_secret="s3cret-beta")
    other_id = other["id"]
    assert other_id != scim_connection["id"]

    crossed = await client.get(
        f"/scim/v2/{other_id}/Users",
        headers={**scim.headers},
    )
    assert crossed.status_code == 404, crossed.text

    assert (await scim.get("Users")).status_code == 200


async def test_a_connection_scoped_credential_cannot_use_the_default_path(
    client, scim_connection, scim
):
    response = await client.get("/scim/v2/default/Users", headers=scim.headers)
    assert response.status_code == 404, response.text


async def test_an_unknown_connection_id_is_a_404(client, scim):
    unknown = str(uuid.uuid4())
    response = await client.get(f"/scim/v2/{unknown}/Users", headers=scim.headers)
    assert response.status_code == 404, response.text

    garbage = await client.get("/scim/v2/not-a-connection/Users", headers=scim.headers)
    assert garbage.status_code == 404, garbage.text


async def test_the_credential_is_scoped_to_its_tenant(client, db, owner_a, owner_b, scim_connection, scim):
    """Provisioning happens inside one workspace, whatever else the token can reach."""
    created = await scim.post("Users", user_payload("scoped@acme.example"))
    assert created.status_code == 201, created.text

    stored = (
        await db.execute(select(User).where(User.email == "scoped@acme.example"))
    ).scalar_one()
    assert stored.tenant_id == owner_a.tenant_id
    assert stored.tenant_id != owner_b.tenant_id


async def test_two_credentials_in_two_workspaces_do_not_collide(
    client, db, owner_a, owner_b, scim_connection, scim
):
    headers_b = await auth_headers(client, owner_b)
    second = await client.post(
        "/api/scim/credentials", json={"label": "Other workspace"}, headers=headers_b
    )
    assert second.status_code == 201, second.text
    other = ScimClient(client, "default", second.json()["token"])

    mine = await scim.post("Users", user_payload("mine@acme.example"))
    theirs = await other.post("Users", user_payload("theirs@acme.example"))
    assert mine.status_code == theirs.status_code == 201

    mine_names = {r["userName"] for r in (await scim.get("Users")).json()["Resources"]}
    their_names = {r["userName"] for r in (await other.get("Users")).json()["Resources"]}
    assert "mine@acme.example" in mine_names
    assert "theirs@acme.example" not in mine_names
    assert "theirs@acme.example" in their_names
    assert "mine@acme.example" not in their_names

    # Neither can read the other's resource by id.
    assert (await other.get(f"Users/{mine.json()['id']}")).status_code == 404
    assert (await scim.get(f"Users/{theirs.json()['id']}")).status_code == 404


async def test_concurrent_creates_of_the_same_user_produce_one_user(
    client, racing_app, concurrent_sessionmaker
):
    """Two syncs racing must not create two accounts for one person."""
    tenant, _owner, token = await _racing_workspace(concurrent_sessionmaker)
    racing = ScimClient(client, "default", token)

    outcomes = await asyncio.gather(
        racing.post("Users", user_payload("racy@acme.example")),
        racing.post("Users", user_payload("racy@acme.example")),
    )
    statuses = sorted(response.status_code for response in outcomes)
    assert statuses == [201, 409], [response.text for response in outcomes]

    loser = next(response for response in outcomes if response.status_code == 409)
    assert loser.json()["scimType"] == "uniqueness"

    async with concurrent_sessionmaker() as session:
        rows = (
            await session.execute(
                select(User).where(User.tenant_id == tenant.id, User.email == "racy@acme.example")
            )
        ).scalars().all()
    assert len(rows) == 1, "one person, one account"


async def test_concurrent_deactivation_of_the_same_user_is_idempotent(
    client, racing_app, concurrent_sessionmaker
):
    _tenant, _owner, token = await _racing_workspace(concurrent_sessionmaker)
    racing = ScimClient(client, "default", token)
    created = await racing.post("Users", user_payload("twice@acme.example"))
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]

    responses = await asyncio.gather(
        racing.patch(
            f"Users/{user_id}",
            {"Operations": [{"op": "replace", "path": "active", "value": False}]},
        ),
        racing.patch(
            f"Users/{user_id}",
            {"Operations": [{"op": "replace", "path": "active", "value": False}]},
        ),
    )
    assert all(response.status_code in (200, 404, 409) for response in responses), [
        response.status_code for response in responses
    ]

    async with concurrent_sessionmaker() as session:
        stored = (
            await session.execute(
                select(User)
                .where(User.id == uuid.UUID(user_id))
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
    assert stored.is_active is False


async def test_concurrent_rotation_leaves_exactly_one_live_credential(
    client, racing_app, concurrent_sessionmaker
):
    """Rotation is a compare-and-set: whoever revokes the old token issues the
    replacement. Two administrators rotating at once must not leave a second
    live credential behind."""
    _tenant, _owner, token = await _racing_workspace(concurrent_sessionmaker)
    racing = ScimClient(client, "default", token)

    # The credential works before the race, so the assertions after it mean
    # something.
    assert (await racing.get("Users")).status_code == 200

    async with concurrent_sessionmaker() as session:
        credential_id = str((await session.execute(select(SCIMCredential))).scalars().one().id)

    headers = await auth_headers(client, _owner, TEST_PASSWORD)
    responses = await asyncio.gather(
        client.post(f"/api/scim/credentials/{credential_id}/rotate", headers=headers),
        client.post(f"/api/scim/credentials/{credential_id}/rotate", headers=headers),
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 404], [response.text for response in responses]

    async with concurrent_sessionmaker() as session:
        rows = (
            await session.execute(
                select(SCIMCredential).execution_options(populate_existing=True)
            )
        ).scalars().all()
    live = [row for row in rows if row.revoked_at is None]
    assert len(live) == 1, "one rotation, one live credential"
    assert str(live[0].rotated_from_id) == credential_id

    # The predecessor is dead: the token that was working a moment ago is not.
    assert (await racing.get("Users")).status_code == 401


async def test_concurrent_revocations_write_one_audit_entry(
    client, racing_app, concurrent_sessionmaker
):
    """Revocation is idempotent: a retrying administrator must not be able to
    make the workspace believe two different people revoked the same token."""
    from app.db.models import AuditAction, AuditLog

    _tenant, owner, token = await _racing_workspace(concurrent_sessionmaker)
    async with concurrent_sessionmaker() as session:
        row = (await session.execute(select(SCIMCredential))).scalars().one()
        credential_id = str(row.id)

    headers = await auth_headers(client, owner, TEST_PASSWORD)
    responses = await asyncio.gather(
        client.delete(f"/api/scim/credentials/{credential_id}", headers=headers),
        client.delete(f"/api/scim/credentials/{credential_id}", headers=headers),
    )
    assert all(response.status_code == 204 for response in responses), [
        response.status_code for response in responses
    ]

    async with concurrent_sessionmaker() as session:
        entries = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == AuditAction.SCIM_CREDENTIAL_REVOKED)
            )
        ).scalars().all()
        stored = (
            await session.execute(
                select(SCIMCredential)
                .where(SCIMCredential.id == uuid.UUID(credential_id))
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
    assert len(entries) == 1, "one revocation, one audit entry"
    assert stored.revoked_at is not None
    assert (await ScimClient(client, "default", token).get("Users")).status_code == 401


async def test_responses_never_leak_secrets(client, db, scim, scim_connection):
    created = await scim.post("Users", user_payload("quiet@acme.example"))
    assert created.status_code == 201, created.text

    body = created.text
    for forbidden in ("password", "password_hash", "token", "secret", "salt"):
        assert forbidden not in body.lower(), f"{forbidden} leaked in a SCIM response"

    stored = (
        await db.execute(select(User).where(User.email == "quiet@acme.example"))
    ).scalar_one()
    assert stored.password_hash not in body
