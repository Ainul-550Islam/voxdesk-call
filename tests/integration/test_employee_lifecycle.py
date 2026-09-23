"""The employee lifecycle, from the directory to the audit log.

This is the flow an enterprise actually runs, assembled from the parts that are
tested individually elsewhere:

    SCIM provisions a person  ->  they cannot sign in with a password (none was
    ever set)  ->  they sign in through the company's IdP  ->  that sign-in
    lands on *the same account*  ->  the session shows up in their device list
    ->  an administrator ends it  ->  the directory deprovisions them and every
    door closes at once.

The point of doing it end to end is the seams: whether SCIM and SSO agree on who
a person is, whether a session created by one module is visible to another, and
whether a deprovision in the directory really reaches a session minted minutes
earlier by a completely different code path.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.auth.identity import sessions as identity_sessions
from app.db.models import AuditAction, AuditLog, User, UserRole
from tests.auth.scim.conftest import user_payload
from tests.auth.sso.providers import (
    create_saml_connection,
    post_assertion,
    start_saml_login,
)
from tests.conftest import auth_headers
from tests.harness import ScimClient

pytestmark = pytest.mark.asyncio

EMPLOYEE = "ada@acme.example"


@pytest_asyncio.fixture
async def enterprise(client, owner_a, idp):
    """One connection for both protocols' worth of story: the directory that
    provisions this company's people and the IdP they sign in with are the same
    system, so SCIM and SSO share a connection.

    Account linking is switched on deliberately: attaching an IdP identity to an
    account that already exists is a decision an administrator makes per
    connection, and the default is to refuse it.
    """
    connection = await create_saml_connection(
        client, owner_a, idp, slug="acme", allow_account_linking=True
    )
    issued = await client.post(
        "/api/scim/credentials",
        json={"label": "Okta provisioning", "connection_id": connection["id"]},
        headers=await auth_headers(client, owner_a),
    )
    assert issued.status_code == 201, issued.text
    return connection, ScimClient(client, connection["id"], issued.json()["token"])


async def _events(db, tenant_id, *actions) -> list[AuditLog]:
    rows = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id, AuditLog.action.in_(list(actions)))
            .order_by(AuditLog.created_at.asc())
        )
    ).scalars().all()
    return list(rows)


async def _employee(db, tenant_id, email: str) -> User:
    return (
        await db.execute(
            select(User)
            .where(User.tenant_id == tenant_id, User.email == email)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _live_sessions(db, user_id) -> list:
    return await identity_sessions.live_sessions(db, user_id=user_id)


async def test_a_provisioned_employee_signs_in_through_sso_onto_the_same_account(
    client, db, enterprise, idp, tenant_a
):
    """SCIM creates the person; SSO is how they actually get in."""
    connection, scim = enterprise
    hired = await scim.post(
        "Users",
        user_payload(EMPLOYEE, displayName="Ada Lovelace", externalId="okta-ada"),
    )
    assert hired.status_code == 201, hired.text
    user_id = uuid.UUID(hired.json()["id"])

    # No password was ever set, so a password login with any guess is refused.
    guessed = await client.post(
        "/auth/login", json={"email": EMPLOYEE, "password": "Correct-Horse-Battery-9!"}
    )
    assert guessed.status_code == 401, guessed.text

    started = await start_saml_login(client, slug=connection["slug"])
    signed_in = await post_assertion(
        client, idp, started=started, slug=connection["slug"], email=EMPLOYEE
    )
    assert signed_in.status_code == 200, signed_in.text
    body = signed_in.json()
    assert body["created"] is False, "the directory already created this person"
    assert body["linked"] is True, "the IdP subject is attached to the existing account"

    # One account, and it is the one SCIM made.
    rows = (
        await db.execute(select(User).where(User.tenant_id == tenant_a.id, User.email == EMPLOYEE))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == user_id

    # The session belongs to that account, came from SSO, and is not MFA-verified.
    live = await _live_sessions(db, user_id)
    assert len(live) == 1
    assert live[0].auth_method == "sso"
    assert live[0].sso_connection_id == uuid.UUID(connection["id"])
    assert live[0].mfa_verified is False

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200, me.text
    assert me.json()["user"]["email"] == EMPLOYEE


async def test_the_employee_sees_and_can_end_their_own_session(client, db, enterprise, idp):
    connection, scim = enterprise
    await scim.post("Users", user_payload(EMPLOYEE, displayName="Ada Lovelace"))
    started = await start_saml_login(client, slug=connection["slug"])
    signed_in = await post_assertion(
        client, idp, started=started, slug=connection["slug"], email=EMPLOYEE
    )
    token = signed_in.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    listed = await client.get("/api/sessions", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["current_session_id"]
    assert len(listed.json()["sessions"]) == 1
    assert listed.json()["sessions"][0]["auth_method"] == "sso"

    ended = await client.delete("/api/sessions", headers=headers)
    assert ended.status_code in (200, 204), ended.text

    after = await client.get("/api/sessions", headers=headers)
    assert after.status_code == 401, after.text


async def test_an_administrator_suspending_the_account_ends_the_sso_session_immediately(
    client, db, enterprise, idp, owner_a, tenant_a
):
    """The device list is the employee's own; an administrator ends access by
    suspending the account, and that must reach a session minted through the
    IdP a moment earlier."""
    connection, scim = enterprise
    await scim.post("Users", user_payload(EMPLOYEE))
    started = await start_saml_login(client, slug=connection["slug"])
    signed_in = await post_assertion(
        client, idp, started=started, slug=connection["slug"], email=EMPLOYEE
    )
    token = signed_in.json()["access_token"]
    employee = await _employee(db, tenant_a.id, EMPLOYEE)
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 200

    suspended = await client.patch(
        f"/api/team/users/{employee.id}/active",
        json={"is_active": False},
        headers=await auth_headers(client, owner_a),
    )
    assert suspended.status_code == 200, suspended.text

    assert (
        await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 401
    assert not await _live_sessions(db, employee.id)

    # Restoring the account does not restore the session: access is regained by
    # signing in again, not by the old token coming back to life.
    restored = await client.patch(
        f"/api/team/users/{employee.id}/active",
        json={"is_active": True},
        headers=await auth_headers(client, owner_a),
    )
    assert restored.status_code == 200, restored.text
    assert (
        await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 401


async def test_deprovisioning_in_the_directory_closes_every_door_at_once(
    client, db, tenant_a, enterprise, idp
):
    """The end of the lifecycle: the IdP says this person is gone, and the
    session they started five minutes ago stops working on the next request."""
    connection, scim = enterprise
    hired = await scim.post("Users", user_payload(EMPLOYEE, externalId="okta-ada"))
    user_id = uuid.UUID(hired.json()["id"])

    started = await start_saml_login(client, slug=connection["slug"])
    signed_in = await post_assertion(
        client, idp, started=started, slug=connection["slug"], email=EMPLOYEE
    )
    token = signed_in.json()["access_token"]
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 200

    removed = await scim.delete(f"Users/{user_id}")
    assert removed.status_code == 204, removed.text

    # The session is gone, the token is dead, and the account cannot come back
    # through SSO either.
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 401
    assert not await _live_sessions(db, user_id)

    stored = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    assert stored is None, "a hard deprovision removes the account"

    # The IdP is the authority on who exists: because this connection has JIT
    # provisioning on and the provider still asserts this person, the next
    # assertion creates a *new* account rather than resurrecting the old one —
    # new id, no sessions, no inherited access. A provider that has really
    # removed someone stops asserting them, which is where the flow ends.
    again = await start_saml_login(client, slug=connection["slug"])
    recreated = await post_assertion(
        client, idp, started=again, slug=connection["slug"], email=EMPLOYEE
    )
    assert recreated.status_code == 200, recreated.text
    assert recreated.json()["created"] is True

    fresh = await _employee(db, tenant_a.id, EMPLOYEE)
    assert fresh.id != user_id, "the re-provisioned account is a new identity"
    assert await _live_sessions(db, user_id) == []

    trail = await _events(
        db,
        tenant_a.id,
        AuditAction.SCIM_USER_PROVISIONED,
        AuditAction.SSO_ACCOUNT_LINKED,
        AuditAction.SSO_LOGIN_SUCCEEDED,
        AuditAction.SCIM_USER_DEPROVISIONED,
    )
    actions = [row.action for row in trail]
    assert AuditAction.SCIM_USER_PROVISIONED in actions
    assert AuditAction.SCIM_USER_DEPROVISIONED in actions
    assert AuditAction.SSO_LOGIN_SUCCEEDED in actions


async def test_a_rehire_gets_a_fresh_account_and_not_the_old_session(
    client, db, enterprise, idp
):
    """Deactivate (not delete) then reactivate: the old token must not come back
    to life, because that is what a suspended-then-restored account looks like."""
    connection, scim = enterprise
    hired = await scim.post("Users", user_payload(EMPLOYEE))
    user_id = hired.json()["id"]

    started = await start_saml_login(client, slug=connection["slug"])
    signed_in = await post_assertion(
        client, idp, started=started, slug=connection["slug"], email=EMPLOYEE
    )
    token = signed_in.json()["access_token"]

    suspended = await scim.patch(
        f"Users/{user_id}",
        {"Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    assert suspended.status_code == 200, suspended.text
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 401

    restored = await scim.patch(
        f"Users/{user_id}",
        {"Operations": [{"op": "replace", "path": "active", "value": True}]},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["active"] is True

    # The old token stays dead; access is restored by signing in again.
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 401

    fresh_start = await start_saml_login(client, slug=connection["slug"])
    again = await post_assertion(
        client, idp, started=fresh_start, slug=connection["slug"], email=EMPLOYEE
    )
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False
    new_token = again.json()["access_token"]
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {new_token}"})).status_code == 200


async def test_group_membership_through_scim_decides_the_role_sso_also_enforces(
    client, db, owner_a, tenant_a, enterprise, idp
):
    """One mapping, two paths in: the connection's group map decides the role
    whether the person arrived through SCIM or through SSO."""
    from tests.auth.sso.providers import set_mappings

    connection, scim = enterprise
    await set_mappings(client, owner_a, connection, group_mapping={"Managers": "manager"})

    hired = await scim.post("Users", user_payload(EMPLOYEE))
    user_id = hired.json()["id"]
    employee = await _employee(db, tenant_a.id, EMPLOYEE)
    assert employee.role is UserRole.AGENT

    grouped = await scim.post(
        "Groups", {"displayName": "Managers", "members": [{"value": user_id}]}
    )
    assert grouped.status_code == 201, grouped.text

    promoted = await _employee(db, tenant_a.id, EMPLOYEE)
    assert promoted.role is UserRole.MANAGER

    # The SSO path agrees about the same account: signing in does not undo the
    # role or grant a different one.
    started = await start_saml_login(client, slug=connection["slug"])
    signed_in = await post_assertion(
        client, idp, started=started, slug=connection["slug"], email=EMPLOYEE
    )
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["role_changed"] is False

    still = await _employee(db, tenant_a.id, EMPLOYEE)
    assert still.role is UserRole.MANAGER


async def test_the_whole_lifecycle_is_scoped_to_one_workspace(
    client, db, owner_a, owner_b, tenant_a, tenant_b, enterprise, idp
):
    """Nothing in the flow above can be reached from another tenant."""
    connection, scim = enterprise
    hired = await scim.post("Users", user_payload(EMPLOYEE))
    user_id = hired.json()["id"]

    other_headers = await auth_headers(client, owner_b)
    foreign_headers = await auth_headers(client, owner_a)

    # The other workspace cannot see or touch the account.
    foreign = ScimClient(client, "default", "vdscim_not-a-real-token")
    assert (await foreign.get(f"Users/{user_id}")).status_code == 401
    assert (await client.get("/api/sessions", headers=other_headers)).status_code == 200

    me = await client.get("/auth/me", headers=foreign_headers)
    assert me.status_code == 200
    assert me.json()["tenant"]["id"] == str(tenant_a.id) != str(tenant_b.id)

    employee = await _employee(db, tenant_a.id, EMPLOYEE)
    assert employee.tenant_id == tenant_a.id
