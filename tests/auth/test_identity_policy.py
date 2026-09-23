"""The identity policy engine, driven through the HTTP surface.

Every switch in this file is one an administrator can flip, and every one of
them is evaluated somewhere deep in a login or a request path. A setting that is
stored but never *enforced* is worse than no setting at all — the operator
believes the workspace is locked down while it is not — so each test below turns
a switch off and then proves the effect on the surface the switch is supposed to
govern:

* password login (``password_login_allowed``, ``sso_required``, allow-list);
* federated login and just-in-time provisioning (``jit_provisioning_allowed``);
* directory sync (``scim_enabled``);
* machine credentials (``api_keys_allowed``, ``service_accounts_allowed``);
* fresh-proof-of-presence (``privileged_reauth_required``).

Two more things are asserted about the engine itself: that it defaults to the
documented values for a workspace that has never touched it, and that a refusal
is *uniform* on the login endpoint — the reason is written to the audit trail
for the operator, and never returned to the caller, because the reason is also a
map of which addresses exist.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.identity import api_keys as key_service
from app.auth.permissions import Permission
from app.db.models import AuditAction, AuditLog, User, UserRole
from tests.conftest import TEST_PASSWORD, auth_headers, login, make_user

pytestmark = pytest.mark.asyncio

POLICY = "/api/identity/policy"
KNOWN_USER = "policy@example.com"


async def _read(client, headers) -> dict:
    response = await client.get(POLICY, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _write(client, headers, **changes) -> dict:
    response = await client.patch(POLICY, json=changes, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _reason_of(db, tenant_id) -> str:
    """The reason code the operator sees, read out of the audit trail."""
    rows = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id, AuditLog.action == AuditAction.LOGIN_FAILURE)
            .order_by(AuditLog.created_at.desc())
        )
    ).scalars().all()
    assert rows, "a refused login is always recorded"
    return str((rows[0].detail or {}).get("reason", ""))


async def _rejections(db, tenant_id) -> list[str]:
    """The reasons a machine credential was refused, newest first."""
    rows = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action == AuditAction.CREDENTIAL_AUTH_REJECTED,
            )
            .order_by(AuditLog.created_at.desc())
        )
    ).scalars().all()
    return [str((row.detail or {}).get("reason", "")) for row in rows]


async def test_the_defaults_are_the_documented_ones(client, owner_a):
    """Defaults are deliberately permissive: a workspace that has never opened
    these settings must behave exactly as it did before the feature existed."""
    headers = await auth_headers(client, owner_a)
    policy = await _read(client, headers)

    assert policy["mfa_required"] is False
    assert policy["mfa_required_for_admins"] is False
    assert policy["privileged_reauth_required"] is True, (
        "a fresh proof of presence for privileged actions is on by default"
    )
    assert policy["privileged_reauth_minutes"] == 15
    assert policy["sso_required"] is False
    assert policy["password_login_allowed"] is True
    assert policy["api_keys_allowed"] is True
    assert policy["service_accounts_allowed"] is True
    assert policy["scim_enabled"] is True
    assert policy["jit_provisioning_allowed"] is True
    assert policy["session_idle_minutes"] == 720
    assert policy["session_max_active"] == 20
    assert policy["refresh_token_days"] == 14
    assert policy["allowed_email_domains"] == []


async def test_a_change_records_what_it_replaced(client, db, tenant_a, owner_a):
    headers = await auth_headers(client, owner_a)
    changed = await _write(client, headers, session_idle_minutes=60, refresh_token_days=7)
    assert changed["session_idle_minutes"] == 60
    assert changed["refresh_token_days"] == 7

    # A later partial write leaves the earlier fields alone.
    await _write(client, headers, session_max_active=5)
    reread = await _read(client, headers)
    assert reread["session_idle_minutes"] == 60
    assert reread["refresh_token_days"] == 7
    assert reread["session_max_active"] == 5

    events = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.tenant_id == tenant_a.id,
                AuditLog.action == AuditAction.SECURITY_SETTINGS_CHANGED,
            )
            .order_by(AuditLog.created_at.asc())
        )
    ).scalars().all()
    assert len(events) == 2
    assert events[0].detail["operation"] == "identity_policy_updated"
    assert events[0].detail["changes"] == {"session_idle_minutes": 60, "refresh_token_days": 7}
    assert events[1].detail["changes"] == {"session_max_active": 5}


async def test_a_value_outside_the_permitted_range_is_refused(client, owner_a):
    headers = await auth_headers(client, owner_a)
    for body in (
        {"privileged_reauth_minutes": 0},
        {"privileged_reauth_minutes": 1441},
        {"session_idle_minutes": 4},
        {"session_idle_minutes": 43201},
        {"session_max_active": 0},
        {"session_max_active": 201},
        {"refresh_token_days": 0},
        {"refresh_token_days": 366},
    ):
        response = await client.patch(POLICY, json=body, headers=headers)
        assert response.status_code == 422, f"{body}: {response.text}"

    unknown = await client.patch(POLICY, json={"not_a_setting": True}, headers=headers)
    assert unknown.status_code == 422, unknown.text

    # Nothing above changed anything.
    assert (await _read(client, headers))["session_idle_minutes"] == 720


async def test_an_allow_list_is_normalized_and_validated(client, owner_a):
    headers = await auth_headers(client, owner_a)
    stored = await _write(
        client, headers, allowed_email_domains=["@Acme.Example", " acme.example ", "other.example"]
    )
    assert stored["allowed_email_domains"] == ["acme.example", "other.example"]

    cleared = await _write(client, headers, allowed_email_domains=[])
    assert cleared["allowed_email_domains"] == []


async def test_requiring_sso_with_nothing_to_require_is_refused(client, owner_a):
    """The lockout guard: the two combinations that would leave the workspace
    with no way in at all are refusals, not warnings."""
    headers = await auth_headers(client, owner_a)

    no_connection = await client.patch(POLICY, json={"sso_required": True}, headers=headers)
    assert no_connection.status_code == 409, no_connection.text
    assert no_connection.json()["detail"]["code"] == "no_active_sso_connection"
    assert "lock" in no_connection.text.lower()

    no_password_either = await client.patch(
        POLICY, json={"password_login_allowed": False}, headers=headers
    )
    assert no_password_either.status_code == 409, no_password_either.text
    assert no_password_either.json()["detail"]["code"] == "no_active_sso_connection"

    # The workspace is unchanged, and password login still works.
    assert (await _read(client, headers))["sso_required"] is False


async def test_each_workspace_has_its_own(client, owner_a, owner_b):
    """A policy row is tenant data like any other: reading or writing one
    workspace's switches must never touch another's."""
    mine = await auth_headers(client, owner_a)
    theirs = await auth_headers(client, owner_b)

    await _write(client, mine, jit_provisioning_allowed=False, scim_enabled=False)

    assert (await _read(client, mine))["jit_provisioning_allowed"] is False
    other = await _read(client, theirs)
    assert other["jit_provisioning_allowed"] is True, "the neighbour is untouched"
    assert other["scim_enabled"] is True

    # Both workspaces can hold different values at the same time.
    await _write(client, theirs, session_idle_minutes=30)
    assert (await _read(client, mine))["session_idle_minutes"] == 720


async def test_reading_needs_a_permission_and_writing_needs_a_stronger_one(client, db, tenant_a):
    agent = await make_user(db, tenant_a, UserRole.AGENT)
    reader = await make_user(db, tenant_a, UserRole.MANAGER)
    agent_headers = await auth_headers(client, agent)

    assert (await client.get(POLICY, headers=agent_headers)).status_code == 403
    assert (
        await client.patch(POLICY, json={"session_idle_minutes": 60}, headers=agent_headers)
    ).status_code == 403

    # A manager does not hold identity:read either: the policy states what the
    # workspace's second-factor and SSO rules are, which is administrator
    # information. Reading it is a permission like any other.
    manager_headers = await auth_headers(client, reader)
    assert (await client.get(POLICY, headers=manager_headers)).status_code == 403
    assert (
        await client.patch(POLICY, json={"session_idle_minutes": 60}, headers=manager_headers)
    ).status_code == 403


async def test_a_machine_credential_cannot_change_the_policy(client, db, tenant_a, owner_a):
    """A key that could edit the policy could disable the controls that limit
    it, so the endpoint demands a human session."""
    owner_headers = await auth_headers(client, owner_a)
    created = await client.post(
        "/api/api-keys",
        json={
            "name": "automation",
            "scopes": key_service.scopes_from_permissions([Permission.IDENTITY_WRITE]),
        },
        headers=owner_headers,
    )
    assert created.status_code == 201, created.text
    key_headers = {"Authorization": f"Bearer {created.json()['secret']}"}

    refused = await client.patch(POLICY, json={"mfa_required": False}, headers=key_headers)
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "human_session_required"


async def test_turning_off_password_login_stops_a_password_login(
    client, db, tenant_a, owner_a, idp
):
    from tests.auth.sso.providers import create_saml_connection

    # A workspace may only abandon password login once it has somewhere else to
    # send people: the lockout guard refuses the change otherwise.
    await create_saml_connection(client, owner_a, idp, slug="policy")

    user = await make_user(db, tenant_a, UserRole.AGENT, email=KNOWN_USER)
    headers = await auth_headers(client, owner_a)
    await _write(client, headers, password_login_allowed=False)

    refused = await login(client, user.email, TEST_PASSWORD)
    assert refused.status_code == 401, refused.text
    assert "sso" not in refused.text.lower(), (
        "the refusal must not tell the caller that SSO is what is required"
    )
    assert refused.json()["detail"] == "Invalid email or password."
    assert await _reason_of(db, tenant_a.id) == "password_login_disabled"

    await _write(client, headers, password_login_allowed=True)
    assert (await login(client, user.email, TEST_PASSWORD)).status_code == 200


async def test_an_allow_list_keeps_outsiders_out_and_lets_the_listed_in(client, db, tenant_a, owner_a):
    insider = await make_user(db, tenant_a, UserRole.AGENT, email="person@acme.example")
    outsider = await make_user(db, tenant_a, UserRole.AGENT, email="person@elsewhere.example")
    headers = await auth_headers(client, owner_a)
    await _write(client, headers, allowed_email_domains=["acme.example"])

    refused = await login(client, outsider.email, TEST_PASSWORD)
    assert refused.status_code == 401, refused.text
    assert await _reason_of(db, tenant_a.id) == "email_domain_not_allowed"
    assert (await login(client, insider.email, TEST_PASSWORD)).status_code == 200


async def test_turning_off_api_keys_stops_a_live_key_on_the_next_request(client, db, tenant_a, owner_a):
    """Switching a credential class off has to bite immediately, or the
    operator's emergency measure waits for the key's expiry."""
    owner_headers = await auth_headers(client, owner_a)
    created = await client.post(
        "/api/api-keys",
        json={
            "name": "reporting",
            "scopes": key_service.scopes_from_permissions([Permission.ANALYTICS_READ]),
        },
        headers=owner_headers,
    )
    assert created.status_code == 201, created.text
    key_headers = {"Authorization": f"Bearer {created.json()['secret']}"}
    probe = "/api/analytics/overview"
    assert (await client.get(probe, headers=key_headers)).status_code == 200

    await _write(client, owner_headers, api_keys_allowed=False)
    stopped = await client.get(probe, headers=key_headers)
    assert stopped.status_code == 401, stopped.text

    rejected = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_a.id,
                AuditLog.action == AuditAction.CREDENTIAL_AUTH_REJECTED,
            )
        )
    ).scalars().all()
    assert len(rejected) == 1, "one refusal, one row"
    detail = rejected[0].detail
    assert detail["reason"] == "api_keys_disabled"
    assert detail["kind"] == "api_key"
    assert detail["prefix"] == created.json()["prefix"], (
        "the trail names the key that was used, without recording it"
    )
    assert created.json()["secret"] not in str(detail)

    await _write(client, owner_headers, api_keys_allowed=True)
    assert (await client.get(probe, headers=key_headers)).status_code == 200


async def test_turning_off_service_accounts_stops_a_live_credential(client, db, tenant_a, owner_a):
    owner_headers = await auth_headers(client, owner_a)
    account = await client.post(
        "/api/service-accounts",
        json={
            "name": "nightly",
            "scopes": key_service.scopes_from_permissions([Permission.ANALYTICS_READ]),
        },
        headers=owner_headers,
    )
    assert account.status_code == 201, account.text
    issued = await client.post(
        f"/api/service-accounts/{account.json()['id']}/credentials",
        json={"label": "first"},
        headers=owner_headers,
    )
    assert issued.status_code == 201, issued.text
    credential_headers = {"Authorization": f"Bearer {issued.json()['secret']}"}
    probe = "/api/analytics/overview"
    assert (await client.get(probe, headers=credential_headers)).status_code == 200

    await _write(client, owner_headers, service_accounts_allowed=False)
    stopped = await client.get(probe, headers=credential_headers)
    assert stopped.status_code == 401, stopped.text
    rejections = await _rejections(db, tenant_a.id)
    assert rejections and rejections[0] == "service_accounts_disabled", rejections


async def test_privileged_reauth_can_be_switched_off(client, db, tenant_a, owner_a):
    """The setting is a policy, not a law: a workspace that has accepted the
    risk may let a signed-in administrator act without re-proving presence."""
    from datetime import datetime, timedelta

    from app.auth.identity.models import UserSession

    # The owner, because the action used as the probe (minting a key) is one an
    # owner may perform; what is being tested is the *freshness* requirement,
    # not the permission.
    user = owner_a
    headers = await auth_headers(client, user)

    # Age the session's proof of presence past the window; the action that
    # requires one is refused with a code the client can act on.
    rows = (
        await db.execute(
            select(UserSession)
            .where(UserSession.user_id == user.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert rows
    for row in rows:
        row.password_confirmed_at = datetime.utcnow() - timedelta(hours=3)
        row.mfa_verified_at = None
    await db.commit()

    stale = await client.post(
        "/api/api-keys",
        json={
            "name": "late",
            "scopes": key_service.scopes_from_permissions([Permission.ANALYTICS_READ]),
        },
        headers=headers,
    )
    assert stale.status_code == 428, stale.text
    assert stale.json()["detail"]["code"] == "reauth_required"

    # Switching the requirement off is itself a privileged action, so it is
    # performed by a *freshly* signed-in session -- a second login for the same
    # owner, which is what an administrator would do.
    fresh = await auth_headers(client, owner_a)
    await _write(client, fresh, privileged_reauth_required=False)

    allowed = await client.post(
        "/api/api-keys",
        json={
            "name": "later",
            "scopes": key_service.scopes_from_permissions([Permission.ANALYTICS_READ]),
        },
        headers=headers,
    )
    assert allowed.status_code == 201, allowed.text
    assert (await client.delete(f"/api/api-keys/{allowed.json()['id']}", headers=headers)).status_code == 204


async def test_scim_can_be_switched_off_per_workspace(client, db, owner_a, idp):
    """The directory keeps its credential, and the credential stops working."""
    from tests.auth.sso.providers import create_saml_connection
    from tests.harness import ScimClient

    connection = await create_saml_connection(client, owner_a, idp, slug="acme")
    headers = await auth_headers(client, owner_a)
    issued = await client.post(
        "/api/scim/credentials",
        json={"label": "Okta", "connection_id": connection["id"]},
        headers=headers,
    )
    assert issued.status_code == 201, issued.text
    scim = ScimClient(client, connection["id"], issued.json()["token"])
    assert (await scim.get("/Users")).status_code == 200

    await _write(client, headers, scim_enabled=False)
    switched_off = await scim.get("/Users")
    assert switched_off.status_code == 403, switched_off.text
    body = switched_off.json()
    assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert body["status"] == "403"
    assert "switched off" in body["detail"]
    assert (await scim.post("/Users", {"userName": "new@acme.example"})).status_code == 403

    await _write(client, headers, scim_enabled=True)
    assert (await scim.get("/Users")).status_code == 200
    assert str(connection["id"]) in str(
        (await client.get("/api/scim/credentials", headers=headers)).json()
    )


async def test_provisioning_can_be_switched_off_without_breaking_linked_logins(
    client, db, tenant_a, owner_a, idp
):
    """Turning JIT off means "the directory decides who exists", not "nobody can
    sign in": a subject that is already linked still gets in."""
    from tests.auth.sso.providers import (
        create_saml_connection,
        post_assertion,
        start_saml_login,
    )

    connection = await create_saml_connection(
        client, owner_a, idp, slug="direct", allow_account_linking=True
    )
    headers = await auth_headers(client, owner_a)

    started = await start_saml_login(client, slug=connection["slug"])
    first = await post_assertion(
        client,
        idp,
        started=started,
        slug=connection["slug"],
        email="hire@acme.example",
        name_id="employee-1",
    )
    assert first.status_code == 200, first.text
    assert first.json()["created"] is True

    await _write(client, headers, jit_provisioning_allowed=False)

    started = await start_saml_login(client, slug=connection["slug"])
    linked = await post_assertion(
        client,
        idp,
        started=started,
        slug=connection["slug"],
        email="hire@acme.example",
        name_id="employee-1",
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["created"] is False, "the known identity still signs in"

    started = await start_saml_login(client, slug=connection["slug"])
    stranger = await post_assertion(
        client,
        idp,
        started=started,
        slug=connection["slug"],
        email="stranger@acme.example",
        name_id="stranger-1",
    )
    # The response is flat, as every SSO refusal is: a stranger must not learn
    # whether an address exists in this workspace. The reason the operator needs
    # is in the audit trail, where it names the switch that refused.
    assert stranger.status_code == 400, stranger.text
    assert stranger.json()["code"] == "sso_failed"
    link_rejections = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.tenant_id == tenant_a.id,
                AuditLog.action == AuditAction.IDENTITY_LINK_REJECTED,
            )
            .order_by(AuditLog.created_at.desc())
        )
    ).scalars().all()
    assert link_rejections, "a refused provisioning is recorded"
    assert link_rejections[0].detail["reason"] == "sso_provisioning_disabled_for_workspace"

    # Nobody was created for the refused login.
    rows = (
        await db.execute(
            select(User).where(User.tenant_id == tenant_a.id, User.email == "stranger@acme.example")
        )
    ).scalars().all()
    assert rows == []

    # And a directory that is still allowed to provision can still provision.
    await _write(client, headers, jit_provisioning_allowed=True)
    started = await start_saml_login(client, slug=connection["slug"])
    again = await post_assertion(
        client,
        idp,
        started=started,
        slug=connection["slug"],
        email="stranger@acme.example",
        name_id="stranger-1",
    )
    assert again.status_code == 200, again.text
    assert again.json()["created"] is True
