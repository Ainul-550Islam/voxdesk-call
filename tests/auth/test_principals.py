"""Who is calling: the four credential classes and the one structure they become.

Every authenticator in the codebase — the human JWT path, API keys, service
accounts and SCIM bearers — returns the same ``AuthenticatedPrincipal``, and
everything downstream reads only that. So the properties worth asserting are the
ones the rest of the system relies on:

* the four kinds are distinguishable, and "machine" means all three of the
  non-human ones, because that is what the human-only guard checks;
* a machine principal's ``actor`` is a real person (the one who created the
  credential) and its tenant binding is the credential's, never the caller's;
* scopes are a *second* gate that narrows a principal, and ``None`` — meaning
  "not scope-limited" — is reserved for humans;
* the credential families do not cross: a SCIM bearer is not an API credential
  and a session token is not a directory credential;
* nothing is accepted on the strength of its prefix: an unknown bearer is an
  authentication failure, not an anonymous request.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.auth.identity import api_keys as key_service
from app.auth.identity.principals import AuthenticatedPrincipal, PrincipalKind
from app.auth.permissions import Permission
from app.db.models import User, UserRole
from tests.conftest import auth_headers, make_user

pytestmark = pytest.mark.asyncio

ANALYTICS = "/api/analytics/overview"
BILLING = "/api/billing"
HUMAN_ONLY = "/api/identity/policy"


def _actor() -> User:
    return User(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        email="actor@example.com",
        full_name="Actor",
        role=UserRole.OWNER,
        password_hash="x",
        is_active=True,
    )


def test_the_four_kinds_and_which_of_them_are_machines():
    assert {kind.value for kind in PrincipalKind} == {
        "user",
        "api_key",
        "service_account",
        "scim",
    }
    assert PrincipalKind.USER.is_machine is False
    assert PrincipalKind.API_KEY.is_machine is True
    assert PrincipalKind.SERVICE_ACCOUNT.is_machine is True
    assert PrincipalKind.SCIM.is_machine is True


def test_scopes_are_a_second_gate_and_none_means_unlimited():
    actor = _actor()

    human = AuthenticatedPrincipal(
        kind=PrincipalKind.USER, actor=actor, tenant_id=actor.tenant_id
    )
    assert human.scopes is None
    assert human.is_machine is False
    assert human.permits(Permission.BILLING_WRITE.value), "a person is limited by role"

    machine = AuthenticatedPrincipal(
        kind=PrincipalKind.API_KEY,
        actor=actor,
        tenant_id=actor.tenant_id,
        scopes=frozenset({Permission.ANALYTICS_READ.value}),
        api_key_id=uuid.uuid4(),
    )
    assert machine.is_machine is True
    assert machine.permits(Permission.ANALYTICS_READ.value) is True
    assert machine.permits(Permission.BILLING_WRITE.value) is False

    # An empty scope set grants nothing. It is legal — a key can be minted
    # before it is needed — and it must not be confused with "unlimited".
    empty = AuthenticatedPrincipal(
        kind=PrincipalKind.SERVICE_ACCOUNT,
        actor=actor,
        tenant_id=actor.tenant_id,
        scopes=frozenset(),
        service_account_id=uuid.uuid4(),
    )
    assert empty.permits(Permission.ANALYTICS_READ.value) is False

    assert machine.user_id == actor.id, "the actor is always a users row"


async def _key(client, headers, permissions=(Permission.ANALYTICS_READ,), **payload):
    body = {"name": "probe", "scopes": key_service.scopes_from_permissions(list(permissions))}
    body.update(payload)
    created = await client.post("/api/api-keys", json=body, headers=headers)
    assert created.status_code == 201, created.text
    return created.json()


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


async def test_a_human_session_and_a_key_both_work_but_are_not_the_same_caller(
    client, db, tenant_a, owner_a
):
    """A key reaches what its scopes cover; a human session reaches what their
    role covers; and the human-only endpoint refuses the key that the same
    person minted minutes earlier."""
    human = await auth_headers(client, owner_a)
    issued = await _key(client, human)

    assert (await client.get(ANALYTICS, headers=human)).status_code == 200
    assert (await client.get(ANALYTICS, headers=_bearer(issued["secret"]))).status_code == 200

    # A human may read their own workspace policy; a machine credential may not
    # touch it, even though its creator could.
    assert (await client.get(HUMAN_ONLY, headers=human)).status_code == 200
    assert (await client.get(HUMAN_ONLY, headers=_bearer(issued["secret"]))).status_code == 403
    assert (await client.get(ANALYTICS, headers={"Authorization": "Bearer "})).status_code == 401


async def test_scope_narrowing_survives_the_owners_role(client, db, tenant_a, owner_a):
    """The key is not a downgrade token for the *owner's* role: it is the
    intersection, and the intersection is what the request is answered with."""
    human = await auth_headers(client, owner_a)
    narrow = await _key(client, human, permissions=(Permission.ANALYTICS_READ,))

    refused = await client.get(BILLING, headers=_bearer(narrow["secret"]))
    assert refused.status_code == 403, refused.text
    assert "permission" in refused.text.lower()

    # The same owner, in person, may read the billing status.
    assert (await client.get(BILLING, headers=human)).status_code == 200

    # A key minted with the billing scope reaches it.
    wide = await _key(client, human, permissions=(Permission.ANALYTICS_READ, Permission.BILLING_READ))
    assert (await client.get(BILLING, headers=_bearer(wide["secret"]))).status_code == 200


async def test_the_actor_of_a_machine_credential_is_the_person_who_made_it(
    client, db, tenant_a, owner_a
):
    """The audit trail must name a real account, and the tenant binding must be
    the credential's — never something the caller asserts."""
    human = await auth_headers(client, owner_a)
    issued = await _key(client, human)

    await client.get(ANALYTICS, headers=_bearer(issued["secret"]))

    from app.db.models import AuditLog

    rows = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_a.id)
            .order_by(AuditLog.created_at.desc())
        )
    ).scalars().all()
    assert rows, "using a credential is recorded"
    assert any(row.actor_user_id == owner_a.id for row in rows), (
        "the machine's actor is the human who created it"
    )

    # The key's tenant is its own, whatever the request path says.
    row = (
        await db.execute(
            select(key_service.APIKey).where(key_service.APIKey.id == uuid.UUID(issued["id"]))
        )
    ).scalar_one()
    assert row.tenant_id == tenant_a.id
    assert row.created_by_user_id == owner_a.id


async def test_a_key_belongs_to_the_workspace_that_issued_it(client, db, tenant_a, owner_a, owner_b):
    mine = await auth_headers(client, owner_a)
    theirs = await auth_headers(client, owner_b)
    issued = await _key(client, mine, permissions=(Permission.API_KEY_MANAGE,))

    # The key's own workspace can list it; the other workspace cannot.
    mine_listed = await client.get("/api/api-keys", headers=mine)
    assert issued["id"] in {row["id"] for row in mine_listed.json()}

    # Key management is a human-only operation in every workspace: a key that
    # could rotate keys could make itself permanent.
    foreign_key = await _key(client, theirs, permissions=(Permission.API_KEY_MANAGE,))
    managed_by_machine = await client.delete(
        f"/api/api-keys/{issued['id']}", headers=_bearer(foreign_key["secret"])
    )
    assert managed_by_machine.status_code == 403, managed_by_machine.text

    # A person in the other workspace still cannot name it.
    assert (
        await client.delete(f"/api/api-keys/{issued['id']}", headers=theirs)
    ).status_code == 404
    assert (
        await client.post(f"/api/api-keys/{issued['id']}/rotate", headers=theirs)
    ).status_code == 404

    # And a key issued by the first workspace is invisible to the second.
    listed = await client.get("/api/api-keys", headers=theirs)
    assert issued["id"] not in {row["id"] for row in listed.json()}


async def test_suspending_the_actor_stops_their_credentials(client, db, tenant_a, owner_a):
    """There is no phantom identity behind a key: if the person is deactivated,
    everything they minted stops authenticating."""
    stand_in = await make_user(db, tenant_a, UserRole.ADMIN)
    human = await auth_headers(client, stand_in)
    issued = await _key(client, human)
    assert (await client.get(ANALYTICS, headers=_bearer(issued["secret"]))).status_code == 200

    owner_headers = await auth_headers(client, owner_a)
    suspended = await client.patch(
        f"/api/team/users/{stand_in.id}/active", json={"is_active": False}, headers=owner_headers
    )
    assert suspended.status_code == 200, suspended.text

    assert (await client.get(ANALYTICS, headers=_bearer(issued["secret"]))).status_code == 401


async def test_the_credential_families_do_not_cross(client, db, tenant_a, owner_a, idp):
    """A directory bearer is only a directory bearer, and a session token is not
    a directory credential."""
    from tests.auth.sso.providers import create_saml_connection
    from tests.harness import ScimClient, user_payload

    human = await auth_headers(client, owner_a)
    connection = await create_saml_connection(client, owner_a, idp, slug="acme")
    credential = await client.post(
        "/api/scim/credentials",
        json={"label": "Okta", "connection_id": connection["id"]},
        headers=human,
    )
    assert credential.status_code == 201, credential.text
    scim_token = credential.json()["token"]
    assert scim_token.startswith("vdscim_")

    scim = ScimClient(client, connection["id"], scim_token)
    created = await scim.post("/Users", user_payload("crossed@acme.example"))
    assert created.status_code == 201, created.text

    # The directory credential is not an API credential.
    on_api = await client.get(ANALYTICS, headers=_bearer(scim_token))
    assert on_api.status_code in (401, 403), on_api.text

    # A session token is not a directory credential.
    on_scim = await client.get(scim.url("/Users"), headers=human)
    assert on_scim.status_code == 401, on_scim.text
    assert "www-authenticate" in {key.lower() for key in on_scim.headers}

    # The web session is unaffected.
    assert (await client.get(ANALYTICS, headers=human)).status_code == 200


async def test_a_token_that_is_not_a_token_is_refused_everywhere(client, db, tenant_a, owner_a):
    human = await auth_headers(client, owner_a)
    for token in (
        "vdk_nope",
        "vdsa_nope",
        "vdscim_nope",
        "vdk",
        "Bearer",
        "eyJhbGciOiJub25lIn0.e30.",
        "a" * 300,
    ):
        response = await client.get(ANALYTICS, headers=_bearer(token))
        assert response.status_code == 401, f"{token[:16]}: {response.text}"

    assert (await client.get(ANALYTICS, headers=human)).status_code == 200
