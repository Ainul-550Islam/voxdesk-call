"""Who a federated login is allowed to become: JIT, linking, role mapping.

Four separate decisions, and each one has a way of going wrong that is worse
than "the login fails":

* **JIT** may create a user only when the connection allows it. A connection
  with ``jit_enabled: false`` is a connection whose operator wants to provision
  accounts deliberately.
* **Linking** may attach an assertion to an existing account only when the
  connection allows it *and* the provider asserted a verified address — and,
  crucially, only inside the same workspace. An address registered in another
  tenant is refused and audited, never adopted.
* **Role mapping** may assign a role, but only one an administrator configured.
  The claim is data; the mapping is policy. A provider that invents a ``role``
  attribute where no ``role_claim`` is configured changes nothing.
* **A disabled account stays disabled.** An identity provider asserting a user
  we deprovisioned must not be able to undo that.

Plus one structural rule: a mapping that names ``owner`` (or any role outside the
assignable set) is refused when it is saved, rather than at someone's login.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.identity.models import IdentityMapping, UserEmail
from app.db.models import AuditLog, User, UserRole
from tests.auth.sso.providers import (
    create_saml_connection,
    post_assertion,
    start_saml_login,
)
from tests.auth.sso.providers import create_oidc_connection, hand_back_token, query_of
from tests.conftest import auth_headers, make_user

pytestmark = pytest.mark.asyncio


async def _login(client, idp, **assertion) -> object:
    started = await start_saml_login(client)
    return await post_assertion(
        client,
        idp,
        started=started,
        response=idp.response(request_id=started["request_id"], **assertion),
    )


async def _user(db, email: str) -> User | None:
    return (
        await db.execute(select(User).where(User.email == email).execution_options(populate_existing=True))
    ).scalar_one_or_none()


async def _audit_reasons(db, action_names: set[str]) -> list[str]:
    from app.db.models import AuditAction

    wanted = [action for action in AuditAction if action.name in action_names]
    rows = (
        await db.execute(select(AuditLog).where(AuditLog.action.in_(wanted)))
    ).scalars().all()
    reasons: list[str] = []
    for row in rows:
        detail = row.detail if isinstance(row.detail, dict) else {}
        reasons.append(str(detail.get("reason") or ""))
    return reasons


# ---------------------------------------------------------------- JIT --------


async def test_jit_creates_a_user_with_the_configured_default_role(client, db, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp, default_role="manager")

    completed = await _login(client, idp, email="newcomer@acme.test")
    assert completed.status_code == 200, completed.text
    assert completed.json()["created"] is True

    user = await _user(db, "newcomer@acme.test")
    assert user is not None
    assert user.role is UserRole.MANAGER
    assert user.tenant_id == owner_a.tenant_id
    assert user.is_active is True
    assert user.password_hash, "a provisioned account still holds a real hash"
    assert connection["jit_enabled"] is True
    assert user.email == "newcomer@acme.test"


async def test_a_connection_with_jit_off_creates_nobody(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp, jit_enabled=False)

    refused = await _login(client, idp, email="newcomer@acme.test")
    assert refused.status_code == 400, refused.text
    assert "access_token" not in refused.text
    assert await _user(db, "newcomer@acme.test") is None


async def test_a_second_login_reuses_the_provisioned_account(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)

    first = await _login(client, idp, email="newcomer@acme.test", name_id="user-42")
    assert first.status_code == 200
    second = await _login(client, idp, email="newcomer@acme.test", name_id="user-42")
    assert second.json()["created"] is False

    rows = (await db.execute(select(User).where(User.email == "newcomer@acme.test"))).scalars().all()
    assert len(rows) == 1


# ------------------------------------------------------------- linking -------


async def test_linking_is_refused_unless_the_connection_allows_it(client, db, tenant_a, owner_a, idp):
    existing = await make_user(db, tenant_a, UserRole.MANAGER, email="existing@acme.test")
    await create_saml_connection(client, owner_a, idp, allow_account_linking=False)

    refused = await _login(client, idp, email="existing@acme.test")
    assert refused.status_code == 400, refused.text
    assert "access_token" not in refused.text

    user = await _user(db, "existing@acme.test")
    assert user.role is existing.role, "a refused link must not touch the account"


async def test_linking_attaches_the_subject_to_the_existing_account(client, db, tenant_a, owner_a, idp):
    existing = await make_user(db, tenant_a, UserRole.MANAGER, email="existing@acme.test")
    await create_saml_connection(client, owner_a, idp, allow_account_linking=True)

    completed = await _login(client, idp, email="existing@acme.test", name_id="user-99")
    assert completed.status_code == 200, completed.text
    assert completed.json()["linked"] is True
    assert completed.json()["created"] is False

    rows = (await db.execute(select(User).where(User.email == "existing@acme.test"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == existing.id

    mappings = (
        await db.execute(select(IdentityMapping).where(IdentityMapping.user_id == existing.id))
    ).scalars().all()
    assert len(mappings) == 1
    assert mappings[0].connection_id is not None
    assert mappings[0].external_subject == "user-99"


async def test_linking_needs_a_verified_address_when_the_connection_says_so(
    client, db, tenant_a, owner_a, idp, oidc_provider
):
    """OIDC is where ``email_verified`` is a real claim, so it is asserted there.

    The same person, the same address and the same connection: only the provider's
    assertion about the address changes.
    """
    await make_user(db, tenant_a, UserRole.MANAGER, email="existing@acme.test")
    await create_oidc_connection(
        client, owner_a, allow_account_linking=True, require_verified_email=True
    )

    async def attempt(*, verified: bool):
        started = await client.post("/auth/sso/acme/start")
        nonce = query_of(started.json()["authorization_url"])["nonce"]
        hand_back_token(
            idp, oidc_provider, nonce=nonce, email="existing@acme.test", email_verified=verified
        )
        return await client.get(
            "/auth/sso/acme/callback",
            params={"code": "c-1", "state": started.json()["state"]},
        )

    refused = await attempt(verified=False)
    assert refused.status_code == 400, refused.text
    assert "access_token" not in refused.text

    accepted = await attempt(verified=True)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["linked"] is True


async def test_an_address_belonging_to_another_tenant_is_refused(
    client, db, tenant_a, tenant_b, owner_a, owner_b, idp
):
    """The one place a federated login could cross a workspace boundary."""
    foreign = await make_user(db, tenant_b, UserRole.OWNER, email="shared@acme.test")
    await create_saml_connection(client, owner_a, idp, allow_account_linking=True)

    refused = await _login(client, idp, email="shared@acme.test")
    assert refused.status_code == 400, refused.text
    assert "access_token" not in refused.text

    unchanged = (
        await db.execute(select(User).where(User.id == foreign.id).execution_options(populate_existing=True))
    ).scalar_one()
    assert unchanged.tenant_id == owner_b.tenant_id
    assert unchanged.role is UserRole.OWNER

    created = (
        await db.execute(select(User).where(User.email == "shared@acme.test"))
    ).scalars().all()
    assert len(created) == 1, "no second account may be created in the other tenant"

    reasons = await _audit_reasons(db, {"IDENTITY_LINK_REJECTED"})
    assert "address_belongs_to_another_tenant" in reasons


async def test_a_disabled_account_stays_disabled(client, db, tenant_a, owner_a, idp):
    disabled = await make_user(db, tenant_a, UserRole.AGENT, email="gone@acme.test", active=False)
    await create_saml_connection(client, owner_a, idp, allow_account_linking=True)

    refused = await _login(client, idp, email="gone@acme.test")
    assert refused.status_code == 400, refused.text
    assert "access_token" not in refused.text

    user = (
        await db.execute(select(User).where(User.id == disabled.id).execution_options(populate_existing=True))
    ).scalar_one()
    assert user.is_active is False


async def test_an_inactive_tenant_refuses_the_login(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    from app.db.models import Tenant

    stored = (
        await db.execute(select(Tenant).where(Tenant.id == owner_a.tenant_id))
    ).scalar_one()
    stored.is_active = False
    await db.commit()

    refused = await _login(client, idp, email="newcomer@acme.test")
    assert refused.status_code == 400, refused.text
    assert "access_token" not in refused.text


# -------------------------------------------------------- role mapping -------


async def test_a_group_mapping_assigns_the_configured_role(client, db, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp, group_mapping={"agents": "manager"})
    assert connection["group_mapping"] == {"agents": "manager"}

    completed = await _login(client, idp, email="mapped@acme.test", groups=("agents",))
    assert completed.status_code == 200, completed.text

    user = await _user(db, "mapped@acme.test")
    assert user is not None
    assert user.role is UserRole.MANAGER


async def test_an_unmapped_group_is_refused_for_a_new_user(client, db, owner_a, idp):
    await create_saml_connection(
        client, owner_a, idp, group_mapping={"agents": "manager"}, deny_unmapped_roles=True
    )

    refused = await _login(client, idp, email="mystery@acme.test", groups=("contractors",))
    assert refused.status_code == 400, refused.text
    assert await _user(db, "mystery@acme.test") is None


async def test_an_unmapped_group_keeps_an_existing_users_role(client, db, owner_a, idp):
    """Refusing the login would take everything away for a mapping still being tuned.

    The same person signs in twice: the first assertion carries a group that is
    mapped, the second one — after an administrator renamed the group at the IdP
    — carries a group nobody has mapped yet. The second login succeeds and the
    role the first one granted is left alone.
    """
    await create_saml_connection(
        client, owner_a, idp, group_mapping={"agents": "manager"}, deny_unmapped_roles=True
    )

    first = await _login(client, idp, email="tuned@acme.test", name_id="user-7", groups=("agents",))
    assert first.status_code == 200, first.text
    assert (await _user(db, "tuned@acme.test")).role is UserRole.MANAGER

    second = await _login(client, idp, email="tuned@acme.test", name_id="user-7", groups=("nobody",))
    assert second.status_code == 200, second.text
    assert second.json()["role_changed"] is False
    assert (await _user(db, "tuned@acme.test")).role is UserRole.MANAGER


async def test_a_role_change_is_applied_and_reported(client, db, owner_a, idp):
    connection = await create_saml_connection(
        client, owner_a, idp, group_mapping={"agents": "agent"}
    )
    first = await _login(client, idp, email="promote@acme.test", groups=("agents",))
    assert first.status_code == 200, first.text
    assert (await _user(db, "promote@acme.test")).role is UserRole.AGENT

    headers = await auth_headers(client, owner_a)
    updated = await client.put(
        f"/api/sso/connections/{connection['id']}/mappings",
        json={"group_mapping": {"agents": "manager"}},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text

    second = await _login(client, idp, email="promote@acme.test", groups=("agents",))
    assert second.status_code == 200, second.text
    assert second.json()["role_changed"] is True
    assert (await _user(db, "promote@acme.test")).role is UserRole.MANAGER

    from app.db.models import AuditAction

    changes = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.ROLE_CHANGED)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert changes, "a role change must be recorded"
    detail = changes[-1].detail if isinstance(changes[-1].detail, dict) else {}
    assert detail.get("via") == "sso_mapping"
    assert detail.get("from") == "agent"
    assert detail.get("to") == "manager"
    assert detail.get("matched") == "agents"


async def test_a_self_chosen_role_claim_is_ignored_when_no_role_claim_is_configured(
    client, db, owner_a, idp
):
    """The provider cannot promote a user by inventing an attribute.

    ``role`` and ``roles`` are present in the assertion, but this connection maps
    no role claim, so the only role the login can produce is the default one.
    """
    await create_saml_connection(client, owner_a, idp, role_claim="", default_role="agent")

    completed = await _login(
        client,
        idp,
        email="sneaky@acme.test",
        extra_attributes={"role": "owner", "roles": "owner,admin"},
    )
    assert completed.status_code == 200, completed.text
    assert (await _user(db, "sneaky@acme.test")).role is UserRole.AGENT


async def test_a_configured_role_claim_can_only_reach_the_mapped_role(client, db, owner_a, idp):
    connection = await create_saml_connection(
        client, owner_a, idp, role_claim="role", role_mapping={"staff": "agent"}
    )
    assert connection["role_mapping"] == {"staff": "agent"}

    completed = await _login(
        client,
        idp,
        email="claimant@acme.test",
        extra_attributes={"role": "staff"},
    )
    assert completed.status_code == 200, completed.text
    assert (await _user(db, "claimant@acme.test")).role is UserRole.AGENT

    # The same attribute carrying a value nobody mapped does not escalate. With
    # the connection's default posture the identity is admitted at the default
    # role — never at the role it named.
    created = await _login(
        client,
        idp,
        email="claimant2@acme.test",
        name_id="user-claimant-2",
        extra_attributes={"role": "owner"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["created"] is True
    assert (await _user(db, "claimant2@acme.test")).role is UserRole.AGENT

    # And with ``deny_unmapped_roles`` the same identity is refused outright.
    strict = await create_saml_connection(
        client,
        owner_a,
        idp,
        slug="strict",
        role_claim="role",
        role_mapping={"staff": "agent"},
        deny_unmapped_roles=True,
        default_role="agent",
    )
    assert strict["deny_unmapped_roles"] is True

    started = await start_saml_login(client, slug="strict")
    refused = await post_assertion(
        client,
        idp,
        started=started,
        slug="strict",
        response=idp.response(
            request_id=started["request_id"],
            email="claimant3@acme.test",
            name_id="user-claimant-3",
            extra_attributes={"role": "owner"},
        ),
    )
    assert refused.status_code == 400, refused.text
    assert await _user(db, "claimant3@acme.test") is None


async def test_a_mapping_that_names_owner_is_refused_when_it_is_saved(client, db, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    headers = await auth_headers(client, owner_a)

    refused = await client.put(
        f"/api/sso/connections/{connection['id']}/mappings",
        json={"group_mapping": {"founders": "owner"}},
        headers=headers,
    )
    assert refused.status_code in (400, 422), refused.text

    unchanged = await client.get(f"/api/sso/connections/{connection['id']}", headers=headers)
    assert "founders" not in unchanged.json()["group_mapping"]


async def test_mapping_administration_is_tenant_scoped(client, db, owner_a, owner_b, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    refused = await client.put(
        f"/api/sso/connections/{connection['id']}/mappings",
        json={"group_mapping": {"agents": "manager"}},
        headers=await auth_headers(client, owner_b),
    )
    assert refused.status_code == 404, refused.text


async def test_the_same_person_via_two_connections_maps_to_one_account(client, db, owner_a, idp):
    """A subject mapping is per connection; the account is per person."""
    await create_saml_connection(client, owner_a, idp, slug="alpha")
    await create_saml_connection(client, owner_a, idp, slug="beta")

    first = await post_assertion(
        client,
        idp,
        started=await start_saml_login(client, slug="alpha"),
        slug="alpha",
    )
    assert first.status_code == 200, first.text

    second = await post_assertion(
        client,
        idp,
        started=await start_saml_login(client, slug="beta"),
        slug="beta",
    )
    assert second.status_code == 400, (
        "a different connection does not link implicitly: " + second.text
    )
    assert await _user(db, "ada@acme.test") is not None


async def test_an_email_registry_entry_is_recorded_for_a_provisioned_user(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    assert (await _login(client, idp, email="registry@acme.test")).status_code == 200

    registered = (
        await db.execute(select(UserEmail).where(UserEmail.email == "registry@acme.test"))
    ).scalar_one_or_none()
    assert registered is not None
    assert registered.tenant_id == owner_a.tenant_id


async def test_oidc_jit_and_linking_follow_the_same_rules(client, db, owner_a, idp, oidc_provider):
    """The rules live in shared code, so the other protocol is asserted to obey them too."""
    await create_oidc_connection(client, owner_a, jit_enabled=False)

    started = await client.post("/auth/sso/acme/start")
    nonce = query_of(started.json()["authorization_url"])["nonce"]
    hand_back_token(idp, oidc_provider, nonce=nonce, email="oidc-new@acme.test")

    refused = await client.get(
        "/auth/sso/acme/callback",
        params={"code": "c-1", "state": started.json()["state"]},
    )
    assert refused.status_code == 400, refused.text
    assert await _user(db, "oidc-new@acme.test") is None
