"""Who may call the identity surface, and what the policy engine decides.

Two layers, because they fail in different ways:

* ``policies.*`` are pure functions. They are pinned here directly, including
  the decisions that must be refusals: a policy engine tested only on the happy
  path is a policy engine that will one day allow something.
* The HTTP matrix drives the real app with real tokens for every role. That is
  the layer which catches a route that forgot its dependency, or a handler that
  assumed an attribute it does not have -- failures no unit test can see.

The endpoints split into two kinds, and the split is the design:

* **Tenant settings** (identity policy, MFA administration, API keys, service
  accounts, domains, SSO, SCIM) need ``identity:*``-family permissions, which
  only owners and admins hold.
* **Your own account** (your MFA status and enrollment, your sessions, your
  reauthentication) is available to every signed-in human, including a viewer,
  because refusing it would lock a user out of their own security settings.
* Machine credentials are refused by everything that requires a human session,
  regardless of the scopes they carry.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.auth.identity import policies
from app.auth.identity.models import IdentityPolicy
from app.auth.identity.policies import AuthMethod, DomainEvaluation, ResolvedPolicy
from app.auth.permissions import Permission
from app.db.models import UserRole
from tests.conftest import TEST_PASSWORD, auth_headers, make_tenant, make_user

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")

#: Reads that require ``identity:read`` (tenant settings).
ADMIN_READS = [
    "/api/identity/policy",
    "/api/identity/events",
    "/api/api-keys",
    "/api/service-accounts",
    "/api/domains",
    "/api/sso/connections",
    "/api/sso/attempts",
    "/api/scim/credentials",
]

#: Reads about the caller's own account: any signed-in human, no machine.
SELF_READS = ["/api/identity/status", "/api/mfa/status", "/api/sessions"]

#: Changes to tenant settings, with a body that passes validation.
ADMIN_WRITES = [
    ("PATCH", "/api/identity/policy", {"mfa_required": True}),
    ("POST", "/api/api-keys", {"name": "k", "scopes": ["call:read"]}),
    ("POST", "/api/service-accounts", {"name": "sa", "scopes": ["call:read"]}),
    ("POST", "/api/domains", {"domain": "matrix-check.test"}),
    ("POST", "/api/sso/connections", {"name": "Okta", "protocol": "oidc"}),
    ("POST", "/api/scim/credentials", {"label": "idp"}),
]

#: Changes to the caller's own account.
SELF_WRITES = [
    ("POST", "/api/identity/reauth", {"password": TEST_PASSWORD}),
    ("POST", "/api/mfa/enroll", {}),
    ("POST", "/api/mfa/challenge", {}),
    ("POST", "/api/mfa/recovery-codes/regenerate", {}),
    ("POST", "/api/sessions/revoke-others", {}),
]

ANONYMOUS_WRITES = ADMIN_WRITES + SELF_WRITES


# ============================================================ pure policies ===


def _policy(**overrides) -> ResolvedPolicy:
    base = policies.default_policy(TENANT_ID)
    return ResolvedPolicy(**{**base.__dict__, **overrides})


@pytest.mark.unit
def test_the_default_policy_changes_nothing_for_an_existing_tenant():
    policy = policies.default_policy(TENANT_ID)
    evaluation = policies.evaluate_login_policy(policy, email="ada@acme.test", role=UserRole.AGENT)
    assert evaluation.allowed is True
    assert evaluation.mfa_required is False
    assert evaluation.sso_required is False
    assert policy.password_login_allowed is True
    assert policy.api_keys_allowed is True
    assert policy.service_accounts_allowed is True


@pytest.mark.unit
def test_a_policy_that_requires_mfa_demands_it_for_everyone_including_owners():
    policy = _policy(mfa_required=True)
    for role in UserRole:
        assert policies.mfa_required_for(policy, role=role) is True
    evaluation = policies.evaluate_login_policy(
        policy, email="ada@acme.test", role=UserRole.VIEWER, mfa_available=True
    )
    assert evaluation.mfa_required is True


@pytest.mark.unit
def test_admin_only_mfa_does_not_touch_agents():
    policy = _policy(mfa_required_for_admins=True)
    assert policies.mfa_required_for(policy, role=UserRole.ADMIN) is True
    assert policies.mfa_required_for(policy, role=UserRole.OWNER) is True
    assert policies.mfa_required_for(policy, role=UserRole.MANAGER) is False
    assert policies.mfa_required_for(policy, role=UserRole.AGENT) is False


@pytest.mark.unit
def test_a_per_user_value_wins_in_both_directions():
    """The documented break-glass rule, pinned so it cannot drift silently.

    NULL means "follow the tenant policy"; a value is an operator's explicit
    decision about one account, and it beats the tenant rule both ways. A
    break-glass account that a tenant-wide change could re-arm is not one.
    """
    policy = _policy()
    assert policies.mfa_required_for(policy, role=UserRole.AGENT) is False
    assert policies.mfa_required_for(policy, role=UserRole.AGENT, user_override=True) is True

    strict = _policy(mfa_required=True)
    assert policies.mfa_required_for(strict, role=UserRole.AGENT) is True
    assert policies.mfa_required_for(strict, role=UserRole.AGENT, user_override=None) is True
    assert policies.mfa_required_for(strict, role=UserRole.AGENT, user_override=False) is False


@pytest.mark.unit
def test_a_tenant_that_requires_sso_refuses_password_logins():
    policy = _policy(sso_required=True)
    evaluation = policies.evaluate_login_policy(
        policy, email="ada@acme.test", role=UserRole.ADMIN, auth_method=AuthMethod.PASSWORD
    )
    assert evaluation.allowed is False
    assert evaluation.sso_required is True
    assert evaluation.reason


@pytest.mark.unit
def test_a_verified_domain_can_require_sso_and_an_unverified_one_cannot():
    policy = _policy()

    unverified = DomainEvaluation(domain="acme.test", known=True, verified=False,
                                  requires_sso=True, enforcement="require_sso")
    evaluation = policies.evaluate_login_policy(
        policy, email="ada@acme.test", domain_policy=unverified
    )
    assert evaluation.allowed is True, "an unverified claim must never refuse a login"
    assert evaluation.sso_required is False, "an unproven claim is not a policy"

    verified = DomainEvaluation(domain="acme.test", known=True, verified=True,
                                requires_sso=True, enforcement="require_sso")
    evaluation = policies.evaluate_login_policy(
        policy, email="ada@acme.test", domain_policy=verified
    )
    assert evaluation.allowed is False and evaluation.sso_required is True


@pytest.mark.unit
def test_an_address_outside_the_allowed_domains_is_refused():
    policy = _policy(allowed_email_domains=["acme.test"])
    allowed = policies.evaluate_login_policy(policy, email="ada@acme.test")
    refused = policies.evaluate_login_policy(policy, email="ada@elsewhere.test")
    assert allowed.allowed is True
    assert refused.allowed is False


@pytest.mark.unit
def test_privileged_actions_need_fresh_proof():
    """``allowed`` plus ``requires_mfa`` means "ask for a factor", not "refuse".

    The distinction is the difference between a 428 that a user resolves in ten
    seconds and a 403 that looks like a broken account, so it is pinned.
    """
    policy = _policy(privileged_reauth_required=True, privileged_reauth_minutes=15)
    stale = policies.evaluate_privileged_action(
        policy,
        now=_now(),
        mfa_verified_at=_now() - timedelta(minutes=30),
        has_active_mfa_factor=True,
    )
    assert stale.allowed is True
    assert stale.requires_mfa is True
    assert stale.reason == "fresh_mfa_required"

    never = policies.evaluate_privileged_action(
        policy, now=_now(), has_active_mfa_factor=True
    )
    assert never.requires_mfa is True and never.reason == "mfa_required"

    fresh = policies.evaluate_privileged_action(
        policy,
        now=_now(),
        mfa_verified_at=_now() - timedelta(minutes=1),
        has_active_mfa_factor=True,
    )
    assert fresh.allowed is True and fresh.requires_mfa is False

    # With no factor enrolled, the password is the proof of presence instead.
    without_factor = policies.evaluate_privileged_action(
        policy, now=_now(), password_confirmed_at=_now() - timedelta(minutes=30)
    )
    assert without_factor.allowed is True and without_factor.requires_password is True


@pytest.mark.unit
def test_a_tenant_that_requires_no_reauth_is_not_slowed_down():
    policy = _policy(privileged_reauth_required=False)
    evaluation = policies.evaluate_privileged_action(policy, now=_now())
    assert evaluation.allowed is True and evaluation.requires_mfa is False


@pytest.mark.unit
def test_a_machine_credential_is_never_privileged_enough_for_identity_settings():
    policy = _policy(privileged_reauth_required=True)
    evaluation = policies.evaluate_privileged_action(
        policy, now=_now(), auth_method=AuthMethod.API_KEY
    )
    assert evaluation.allowed is False, "privileged actions require a human session"


@pytest.mark.unit
def test_scope_grants_never_exceed_the_creator():
    from app.auth.permissions import Permission

    denied = policies.evaluate_scope_grant(
        creator_role=UserRole.AGENT,
        creator_permissions=None,
        requested_scopes=[Permission.TENANT_DELETE.value],
    )
    assert denied.allowed is False
    assert "platform" in denied.reason or "not_held" in denied.reason

    allowed = policies.evaluate_scope_grant(
        creator_role=UserRole.OWNER,
        creator_permissions=None,
        requested_scopes=[Permission.CALL_READ.value],
    )
    assert allowed.allowed is True


@pytest.mark.unit
def test_an_unknown_scope_is_refused_rather_than_ignored():
    evaluation = policies.evaluate_scope_grant(
        creator_role=UserRole.OWNER,
        creator_permissions=None,
        requested_scopes=["not:a:permission"],
    )
    assert evaluation.allowed is False
    assert "unknown_scope" in evaluation.reason


@pytest.mark.unit
def test_api_credentials_can_be_switched_off_per_tenant():
    assert policies.evaluate_api_credential(
        _policy(api_keys_allowed=False), kind="api_key"
    ).allowed is False
    assert policies.evaluate_api_credential(_policy(), kind="api_key").allowed is True
    assert policies.evaluate_api_credential(
        _policy(service_accounts_allowed=False), kind="service_account"
    ).allowed is False


@pytest.mark.unit
def test_a_disabled_sso_connection_cannot_be_used_even_when_the_tenant_allows_sso():
    policy = _policy()
    assert policies.evaluate_sso_policy(policy, connection_status="active").allowed is True
    assert policies.evaluate_sso_policy(policy, connection_status="disabled").allowed is False
    assert policies.evaluate_sso_policy(policy, connection_status="draft").allowed is False


@pytest.mark.unit
def test_jit_requires_both_the_tenant_and_the_connection_to_allow_it():
    policy = _policy(jit_provisioning_allowed=False)
    evaluation = policies.evaluate_sso_policy(
        policy, connection_status="active", jit_requested=True
    )
    assert evaluation.jit_allowed is False


# =============================================================== http layer ===


async def _owner_headers(client, db, name="Owner"):
    tenant = await make_tenant(db, name)
    user = await make_user(db, tenant, UserRole.OWNER)
    return await auth_headers(client, user), user, tenant


@pytest.mark.parametrize("path", ADMIN_READS + SELF_READS)
async def test_an_anonymous_caller_gets_401_not_403(client, path):
    response = await client.get(path)
    assert response.status_code == 401, f"{path} answered {response.status_code}"


@pytest.mark.parametrize("method,path,body", ANONYMOUS_WRITES)
async def test_an_anonymous_caller_cannot_write(client, method, path, body):
    response = await client.request(method, path, json=body)
    assert response.status_code in (401, 422), f"{method} {path} answered {response.status_code}"


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.AGENT, UserRole.MANAGER])
@pytest.mark.parametrize("path", ADMIN_READS)
async def test_a_role_without_identity_read_gets_403(client, db, path, role):
    tenant = await make_tenant(db, f"{role.value} tenant")
    headers = await auth_headers(client, await make_user(db, tenant, role))
    response = await client.get(path, headers=headers)
    assert response.status_code == 403, f"{path} answered {response.status_code} for {role.value}"


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.AGENT, UserRole.MANAGER])
@pytest.mark.parametrize("method,path,body", ADMIN_WRITES)
async def test_a_role_without_identity_write_gets_403(client, db, method, path, body, role):
    tenant = await make_tenant(db, f"{role.value} tenant")
    headers = await auth_headers(client, await make_user(db, tenant, role))
    response = await client.request(method, path, json=body, headers=headers)
    assert response.status_code == 403, f"{method} {path} answered {response.status_code}"


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.AGENT, UserRole.MANAGER])
@pytest.mark.parametrize("path", SELF_READS)
async def test_every_human_role_may_read_their_own_account_state(client, db, path, role):
    tenant = await make_tenant(db, f"{role.value} tenant")
    headers = await auth_headers(client, await make_user(db, tenant, role))
    response = await client.get(path, headers=headers)
    assert response.status_code == 200, f"{path} answered {response.status_code}: {response.text}"


@pytest.mark.parametrize("path", ADMIN_READS + SELF_READS)
async def test_an_owner_can_read_every_identity_view(client, db, path):
    headers, _, _ = await _owner_headers(client, db, "Owner reads")
    response = await client.get(path, headers=headers)
    assert response.status_code == 200, f"{path} answered {response.status_code}: {response.text}"


@pytest.mark.parametrize("method,path,body", ADMIN_WRITES + SELF_WRITES)
async def test_an_owner_reaches_the_handler_rather_than_being_refused(
    client, db, method, path, body
):
    """Anything below 500 that is not a bare 403 means the handler ran."""
    headers, _, _ = await _owner_headers(client, db, "Owner writes")
    response = await client.request(method, path, json=body, headers=headers)
    assert response.status_code != 403, f"{method} {path} refused an owner"
    assert response.status_code < 500, f"{method} {path} answered {response.status_code}: {response.text}"


async def test_resetting_another_users_second_factor_needs_a_fresh_proof(client, db):
    """Administrative MFA reset is the most dangerous identity action there is.

    The guard is *freshness*, not the permission: a session that proved presence
    moments ago may reset a colleague's factor, one that has been sitting idle
    for an hour may not until the operator proves presence again. The stale case
    is created the same way real time would create it -- by moving the session's
    recorded confirmation into the past.
    """
    from app.auth.identity import sessions as identity_sessions
    from app.auth.identity.models import UserSession

    tenant = await make_tenant(db, "Reset tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    target = await make_user(db, tenant, UserRole.ADMIN)
    headers = await auth_headers(client, owner)
    path = f"/api/mfa/users/{target.id}/reset"

    assert (await client.post(path, json={}, headers=headers)).status_code == 204

    enabled = await client.patch(
        "/api/identity/policy",
        json={"privileged_reauth_required": True, "privileged_reauth_minutes": 15},
        headers=headers,
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["privileged_reauth_required"] is True

    live = await identity_sessions.live_sessions(db, user_id=owner.id)
    assert live, "the owner's session should still be live"
    row = await db.get(UserSession, live[0].id)
    assert row.password_confirmed_at is not None, "a password login records when it proved presence"
    row.password_confirmed_at = _now() - timedelta(hours=1)
    await db.commit()

    stale = await client.post(path, json={}, headers=headers)
    assert stale.status_code == 428, stale.text
    assert stale.json()["detail"]["code"] == "reauth_required"

    # Proving presence again clears it.
    reauth = await client.post(
        "/api/identity/reauth", json={"password": TEST_PASSWORD}, headers=headers
    )
    assert reauth.status_code in (200, 204), reauth.text
    assert (await client.post(path, json={}, headers=headers)).status_code == 204


async def test_lifting_an_emergency_stop_is_owner_only(client, db):
    """Trip it anywhere; lift it as an owner, with a reason, and only then.

    An administrator tripping the stop is a mitigation and must be cheap. An
    administrator lifting it would put a credential the platform suspended back
    into service, so that is an owner action -- and the customer-facing enable
    toggle still refuses while the stop stands, so there is exactly one path
    back.
    """
    from app.auth.identity import service_accounts

    tenant = await make_tenant(db, "Emergency tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    admin = await make_user(db, tenant, UserRole.ADMIN)
    owner_headers = await auth_headers(client, owner)
    admin_headers = await auth_headers(client, admin)

    account = await service_accounts.create_account(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="leaked-bot",
        scopes=[Permission.LEAD_READ.value],
        commit=True,
    )

    stopped = await client.post(
        f"/api/service-accounts/{account.id}/emergency-disable",
        json={"reason": "credential leaked"},
        headers=admin_headers,
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["emergency_disabled"] is True

    # The ordinary toggle cannot undo it -- from either role.
    for headers in (admin_headers, owner_headers):
        refused = await client.post(
            f"/api/service-accounts/{account.id}/enable", json={}, headers=headers
        )
        assert refused.status_code == 403, refused.text
        # The refusal contract is `code` for the class of failure and `reason`
        # for the specific rule; a client switches on the pair, an operator
        # reads the pair.
        assert refused.json()["detail"]["code"] == "policy_denied"
        assert refused.json()["detail"]["reason"] == "emergency_disabled"

    # A reason is mandatory, and the caller must be the owner.
    assert (
        await client.post(
            f"/api/service-accounts/{account.id}/emergency-clear",
            json={"reason": "x"},
            headers=owner_headers,
        )
    ).status_code == 422

    denied = await client.post(
        f"/api/service-accounts/{account.id}/emergency-clear",
        json={"reason": "false positive, verified with the vendor"},
        headers=admin_headers,
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "owner_required"
    await db.refresh(account)
    assert account.emergency_disabled is True, "a refused call changes nothing"

    cleared = await client.post(
        f"/api/service-accounts/{account.id}/emergency-clear",
        json={"reason": "false positive, verified with the vendor"},
        headers=owner_headers,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["emergency_disabled"] is False

    # And the account can be put back into service.
    assert (
        await client.post(
            f"/api/service-accounts/{account.id}/enable", json={}, headers=owner_headers
        )
    ).status_code == 200


async def test_a_machine_credential_cannot_reach_tenant_identity_settings(client, db):
    """A key with no identity scope is refused; scopes are the whole authority."""
    from app.auth.identity import api_keys

    tenant = await make_tenant(db, "Machine caller")
    owner = await make_user(db, tenant, UserRole.OWNER)
    issued = await api_keys.create_key(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="machine",
        scopes=["call:read"],
        owner_user_id=owner.id,
        commit=True,
    )
    headers = {"Authorization": f"Bearer {issued.token}"}

    # Reads about tenant settings need an identity scope the key does not have.
    for path in ADMIN_READS:
        response = await client.get(path, headers=headers)
        assert response.status_code == 403, f"{path} answered {response.status_code} to a key"

    # Reads about a person are refused outright: a key is not a person.
    for path in SELF_READS:
        response = await client.get(path, headers=headers)
        assert response.status_code == 403, f"{path} answered {response.status_code} to a key"

    for method, path, body in ADMIN_WRITES + SELF_WRITES:
        response = await client.request(method, path, json=body, headers=headers)
        assert response.status_code in (401, 403, 422), (
            f"{method} {path} answered {response.status_code} to a machine credential"
        )


async def test_every_authenticated_identity_route_is_free_of_5xx(client, db):
    """Probe the whole surface as an owner: a 5xx is a bug, a 4xx is an answer.

    The anonymous contract test in ``tests/test_api_contract.py`` cannot see
    this class of defect, because authentication fails before the handler body
    ever runs. This one gets past the door, which is how it caught
    ``PATCH /api/identity/policy`` calling an audit action that did not exist.
    """
    from app.main import app as fastapi_app

    headers, _, _ = await _owner_headers(client, db, "Probe tenant")

    prefixes = ("/api/identity", "/api/mfa", "/api/sessions", "/api/api-keys",
                "/api/service-accounts", "/api/domains", "/api/sso", "/api/scim")
    parameters = ("{user_id}", "{session_id}", "{key_id}", "{account_id}",
                  "{credential_id}", "{domain_id}", "{connection_id}", "{certificate_id}")
    probed = 0
    for route in fastapi_app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not path.startswith(prefixes):
            continue
        if not methods & {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            continue
        concrete = path
        for parameter in parameters:
            concrete = concrete.replace(parameter, str(uuid.uuid4()))
        if "{" in concrete:
            continue
        for method in sorted(methods & {"GET", "POST", "PATCH", "PUT", "DELETE"}):
            response = await client.request(method, concrete, json={}, headers=headers)
            probed += 1
            assert response.status_code < 500, (
                f"{method} {concrete} answered {response.status_code}: {response.text[:400]}"
            )
    assert probed > 50, f"only {probed} routes probed; the sweep is not covering the surface"


async def test_a_second_tenant_cannot_see_a_first_tenants_identity_settings(client, db):
    tenant_a = await make_tenant(db, "Tenant A")
    tenant_b = await make_tenant(db, "Tenant B")
    headers_a = await auth_headers(client, await make_user(db, tenant_a, UserRole.OWNER))
    headers_b = await auth_headers(client, await make_user(db, tenant_b, UserRole.OWNER))

    response = await client.patch(
        "/api/identity/policy", json={"mfa_required": True}, headers=headers_a
    )
    assert response.status_code == 200, response.text

    assert (await client.get("/api/identity/policy", headers=headers_a)).json()["mfa_required"] is True
    assert (await client.get("/api/identity/policy", headers=headers_b)).json()["mfa_required"] is False

    rows = (await db.execute(select(IdentityPolicy))).scalars().all()
    assert len(rows) == 1 and rows[0].tenant_id == tenant_a.id
    await db.refresh(rows[0])
    assert rows[0].updated_by_user_id is not None, "a policy change must name who made it"
