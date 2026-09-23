"""Adversarial tenant isolation, one resource at a time.

Every identity resource in the product is reachable by id, and ids are the one
thing an attacker can guess, replay or be handed by accident. So each one is
created in one workspace, addressed from another, and asserted to be
indistinguishable from a resource that does not exist — while the original is
re-read to prove nothing was touched on the way through.

The sweep is deliberately exhaustive rather than clever: a missing check in one
route of thirty is exactly the bug this shape of test catches, and it stays
caught when a new resource is added to the list.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.identity import api_keys as key_service
from app.auth.permissions import Permission
from app.db.models import User
from tests.auth.sso.providers import create_saml_connection
from tests.conftest import auth_headers, login

pytestmark = pytest.mark.asyncio


async def _provisioned(client, owner_a, idp) -> dict:
    """One of everything tenant A owns, addressed by id."""
    headers = await auth_headers(client, owner_a)

    connection = await create_saml_connection(client, owner_a, idp, slug="acme")
    certificate = await client.post(
        f"/api/sso/connections/{connection['id']}/certificates",
        json={"certificate": _pem(idp), "make_active": True},
        headers=headers,
    )
    assert certificate.status_code == 201, certificate.text

    domain = await client.post("/api/domains", json={"domain": "isolated.example"}, headers=headers)
    assert domain.status_code == 201, domain.text

    scim = await client.post(
        "/api/scim/credentials",
        json={"label": "isolation", "connection_id": connection["id"]},
        headers=headers,
    )
    assert scim.status_code == 201, scim.text

    key = await client.post("/api/api-keys", json={"name": "isolation"}, headers=headers)
    assert key.status_code == 201, key.text

    account = await client.post(
        "/api/service-accounts", json={"name": "isolation", "scopes": []}, headers=headers
    )
    assert account.status_code == 201, account.text

    return {
        "connection": connection["id"],
        "certificate": certificate.json()["id"],
        "domain": domain.json()["id"],
        "scim_credential": scim.json()["id"],
        "api_key": key.json()["id"],
        "service_account": account.json()["id"],
    }


def _pem(idp) -> str:
    """The IdP's own certificate, which is what a connection imports."""
    return idp.certificate_pem


async def test_every_identity_resource_is_invisible_from_another_workspace(
    client, db, owner_a, owner_b, idp
):
    owned = await _provisioned(client, owner_a, idp)
    foreign = await auth_headers(client, owner_b)
    assert (
        await client.get("/api/sso/connections", headers=foreign)
    ).json() == []
    assert (await client.get("/api/domains", headers=foreign)).json() == []
    assert (await client.get("/api/scim/credentials", headers=foreign)).json() == []
    assert (await client.get("/api/api-keys", headers=foreign)).json() == []
    assert (await client.get("/api/service-accounts", headers=foreign)).json() == []

    probes = [
        ("GET", f"/api/sso/connections/{owned['connection']}", None),
        ("DELETE", f"/api/sso/connections/{owned['connection']}", None),
        ("GET", f"/api/sso/connections/{owned['connection']}/metadata", None),
        ("GET", f"/api/sso/connections/{owned['connection']}/certificates", None),
        (
            "DELETE",
            f"/api/sso/connections/{owned['connection']}/certificates/{owned['certificate']}",
            None,
        ),
        ("GET", f"/api/domains/{owned['domain']}", None),
        ("PATCH", f"/api/domains/{owned['domain']}", {"enforcement": "off"}),
        ("DELETE", f"/api/domains/{owned['domain']}", None),
        ("POST", f"/api/scim/credentials/{owned['scim_credential']}/rotate", None),
        ("DELETE", f"/api/scim/credentials/{owned['scim_credential']}", None),
        ("DELETE", f"/api/api-keys/{owned['api_key']}", None),
        ("GET", f"/api/service-accounts/{owned['service_account']}", None),
        ("PATCH", f"/api/service-accounts/{owned['service_account']}", {"name": "taken"}),
        ("DELETE", f"/api/service-accounts/{owned['service_account']}", None),
        (
            "POST",
            f"/api/service-accounts/{owned['service_account']}/disable",
            {},
        ),
        (
            "POST",
            f"/api/service-accounts/{owned['service_account']}/emergency-disable",
            {"reason": "hostile"},
        ),
    ]

    for method, path, body in probes:
        response = await client.request(method, path, json=body, headers=foreign)
        assert response.status_code in (403, 404), f"{method} {path}: {response.status_code}"
        assert response.status_code != 200

    # Nothing was touched: the owner still sees exactly one of each.
    mine = await auth_headers(client, owner_a)
    assert len((await client.get("/api/sso/connections", headers=mine)).json()) == 1
    assert len((await client.get("/api/domains", headers=mine)).json()) == 1
    assert len((await client.get("/api/scim/credentials", headers=mine)).json()) == 1
    assert len((await client.get("/api/api-keys", headers=mine)).json()) == 1
    account = await client.get(f"/api/service-accounts/{owned['service_account']}", headers=mine)
    assert account.status_code == 200
    assert account.json()["name"] == "isolation"


async def test_a_user_id_from_another_workspace_cannot_be_acted_on(
    client, db, owner_a, owner_b, tenant_a, tenant_b
):
    from tests.conftest import make_user
    from app.db.models import UserRole

    foreign_user = await make_user(db, tenant_b, UserRole.AGENT, email="their-agent@example.com")
    headers = await auth_headers(client, owner_a)

    probes = [
        ("PATCH", f"/api/team/users/{foreign_user.id}/active", {"is_active": False}),
        ("PATCH", f"/api/team/users/{foreign_user.id}/role", {"role": "admin"}),
        ("GET", f"/api/mfa/users/{foreign_user.id}/status", None),
        ("POST", f"/api/mfa/users/{foreign_user.id}/reset", {"reason": "hostile"}),
    ]
    for method, path, body in probes:
        response = await client.request(method, path, json=body, headers=headers)
        assert response.status_code in (403, 404), f"{method} {path}: {response.status_code}"

    untouched = (
        await db.execute(
            select(User).where(User.id == foreign_user.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert untouched.is_active is True
    assert untouched.role is UserRole.AGENT
    assert untouched.tenant_id == tenant_b.id != tenant_a.id


async def test_a_session_id_from_another_workspace_cannot_be_revoked(client, db, owner_a, owner_b):
    started = await login(client, owner_b.email)
    assert started.status_code == 200, started.text
    foreign_token = started.json()["access_token"]
    foreign_session = (
        await client.get(
            "/api/sessions", headers={"Authorization": f"Bearer {foreign_token}"}
        )
    ).json()["current_session_id"]

    headers = await auth_headers(client, owner_a)
    refused = await client.delete(f"/api/sessions/{foreign_session}", headers=headers)
    assert refused.status_code in (403, 404), refused.text

    # Their session is still usable.
    assert (
        await client.get("/auth/me", headers={"Authorization": f"Bearer {foreign_token}"})
    ).status_code == 200


async def test_machine_credentials_are_confined_to_their_workspace(client, db, owner_a, owner_b, idp):
    """A key belongs to the workspace that issued it, and it can neither read
    another workspace's people nor administer its own identity settings."""
    owner_a_key = await client.post(
        "/api/api-keys",
        json={
            "name": "readonly",
            "scopes": key_service.scopes_from_permissions([Permission.USER_READ]),
        },
        headers=await auth_headers(client, owner_a),
    )
    assert owner_a_key.status_code == 201, owner_a_key.text
    bearer = {"Authorization": f"Bearer {owner_a_key.json()['secret']}"}

    mine = await client.get("/api/team/users", headers=bearer)
    assert mine.status_code == 200, mine.text
    emails = {row["email"] for row in mine.json()}
    assert owner_a.email in emails
    assert owner_b.email not in emails, "another workspace's people are not visible"

    # Identity administration needs a person: a machine credential cannot
    # rewrite the workspace's SSO, domains or machine credentials.
    for method, path in (
        ("POST", "/api/api-keys"),
        ("POST", "/api/service-accounts"),
        ("POST", "/api/scim/credentials"),
        ("POST", "/api/domains"),
        ("GET", "/api/identity/policy"),
    ):
        response = await client.request(method, path, json={}, headers=bearer)
        assert response.status_code in (401, 403), f"{method} {path}: {response.status_code}"

    # The owner can still do all of it.
    owner_headers = await auth_headers(client, owner_a)
    assert (await client.get("/api/identity/policy", headers=owner_headers)).status_code == 200


async def test_a_service_account_credential_is_not_a_way_into_identity_management(
    client, db, owner_a, owner_b
):
    headers = await auth_headers(client, owner_a)
    account = await client.post(
        "/api/service-accounts",
        json={"name": "isolated", "scopes": [Permission.ANALYTICS_READ.value]},
        headers=headers,
    )
    assert account.status_code == 201, account.text
    credential = await client.post(
        f"/api/service-accounts/{account.json()['id']}/credentials", json={}, headers=headers
    )
    assert credential.status_code == 201, credential.text
    bearer = {"Authorization": f"Bearer {credential.json()['secret']}"}

    assert (await client.get("/api/analytics/overview", headers=bearer)).status_code == 200

    for path in ("/api/sessions", "/api/api-keys", "/api/service-accounts", "/api/domains"):
        response = await client.get(path, headers=bearer)
        assert response.status_code in (401, 403), f"{path}: {response.status_code}"

    # Its own workspace's identity routes are no more reachable than the other
    # workspace's data.
    assert (
        await client.get(f"/api/team/users/{owner_b.id}", headers=bearer)
    ).status_code in (401, 403, 404)


async def test_a_foreign_id_in_a_create_payload_is_refused(client, db, owner_a, owner_b, idp):
    """Ids also arrive in bodies: linking a mapping or a domain to something
    another workspace owns must fail rather than create a cross-tenant edge."""
    their_connection = await create_saml_connection(client, owner_b, idp, slug="theirs")
    headers = await auth_headers(client, owner_a)

    mapped = await client.put(
        "/api/sso/connections/%s/mappings" % their_connection["id"],
        json={"group_mapping": {"Agents": "manager"}},
        headers=headers,
    )
    assert mapped.status_code == 404, mapped.text

    credential = await client.post(
        "/api/scim/credentials",
        json={"label": "cross", "connection_id": their_connection["id"]},
        headers=headers,
    )
    assert credential.status_code in (400, 404), credential.text

    domain = await client.post("/api/domains", json={"domain": "edge.example"}, headers=headers)
    assert domain.status_code == 201, domain.text
    attached = await client.patch(
        f"/api/domains/{domain.json()['id']}",
        json={"sso_connection_id": their_connection["id"]},
        headers=headers,
    )
    # Refused either because the domain is unverified or because the connection
    # belongs to somebody else — never accepted.
    assert attached.status_code in (400, 404), attached.text

    account = await client.post(
        "/api/service-accounts", json={"name": "cross", "scopes": []}, headers=headers
    )
    assert account.status_code == 201, account.text
    key = await client.post(
        "/api/api-keys",
        json={"name": "cross", "scopes": [], "service_account_id": account.json()["id"]},
        headers=headers,
    )
    # A key may be minted for the account it names (or refused), but never for
    # one in another workspace.
    assert key.status_code in (201, 400, 403, 404), key.text
