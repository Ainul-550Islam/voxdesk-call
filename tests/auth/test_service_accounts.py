"""Service accounts, driven through the HTTP surface.

The lifecycle tests here are the ones an operator would actually run: create a
machine identity, give it a credential, use that credential for a real request,
rotate it, stop it, and clean up. Asserted along the way:

* scopes are the whole authority — a credential can read what its scope allows
  and is refused everywhere else, and it can never reach an identity-management
  route at all;
* rotation has no overlap: the predecessor stops working the moment the
  replacement exists;
* the emergency stop is one-way at the customer level (``enable`` refuses while
  it is in force) and lifting it needs an owner and a reason;
* a service account cannot manage service accounts;
* another tenant sees a 404, not somebody else's account.
"""
from __future__ import annotations

import pytest

from app.auth.permissions import Permission
from app.db.models import UserRole
from tests.conftest import auth_headers, failure_detail, make_user

pytestmark = pytest.mark.asyncio

ANALYTICS = "/api/analytics/overview"


async def _create(client, headers, **payload) -> dict:
    body = {"name": "nightly exporter", "scopes": [Permission.ANALYTICS_READ.value]}
    body.update(payload)
    response = await client.post("/api/service-accounts", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _issue(client, headers, account_id: str, **payload) -> dict:
    response = await client.post(
        f"/api/service-accounts/{account_id}/credentials", json=payload, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


async def test_a_credential_carries_exactly_its_scopes(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    account = await _create(client, headers)
    credential = await _issue(client, headers, account["id"])

    # Its scope works.
    allowed = await client.get(ANALYTICS, headers=_bearer(credential["secret"]))
    assert allowed.status_code == 200, allowed.text

    # A credential with a different scope is refused there.
    other = await _create(
        client,
        headers,
        name="call reader",
        scopes=[Permission.CALL_READ.value],
    )
    narrow = await _issue(client, headers, other["id"])
    refused = await client.get(ANALYTICS, headers=_bearer(narrow["secret"]))
    assert refused.status_code == 403, refused.text


async def test_a_credential_is_returned_once_and_never_listed_again(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    account = await _create(client, headers)
    credential = await _issue(client, headers, account["id"])
    assert credential["secret"]

    listed = await client.get(
        f"/api/service-accounts/{account['id']}/credentials", headers=headers
    )
    assert listed.status_code == 200, listed.text
    assert credential["secret"] not in listed.text
    for row in listed.json():
        assert "secret" not in row


async def test_rotating_a_credential_revokes_the_previous_one(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    account = await _create(client, headers)
    old = await _issue(client, headers, account["id"])
    assert (await client.get(ANALYTICS, headers=_bearer(old["secret"]))).status_code == 200

    rotated = await client.post(
        f"/api/service-accounts/{account['id']}/credentials/{old['id']}/rotate",
        headers=headers,
    )
    assert rotated.status_code == 200, rotated.text
    new_secret = rotated.json()["secret"]
    assert new_secret != old["secret"]

    assert (await client.get(ANALYTICS, headers=_bearer(old["secret"]))).status_code == 401
    assert (await client.get(ANALYTICS, headers=_bearer(new_secret))).status_code == 200


async def test_disabling_an_account_stops_its_credentials_and_enabling_restores_them(
    client, db, owner_a
):
    headers = await auth_headers(client, owner_a)
    account = await _create(client, headers)
    credential = await _issue(client, headers, account["id"])
    secret = credential["secret"]

    stopped = await client.post(
        f"/api/service-accounts/{account['id']}/disable",
        json={"reason": "vendor maintenance"},
        headers=headers,
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["enabled"] is False
    assert (await client.get(ANALYTICS, headers=_bearer(secret))).status_code == 401

    resumed = await client.post(
        f"/api/service-accounts/{account['id']}/enable", json={}, headers=headers
    )
    assert resumed.status_code == 200, resumed.text
    assert (await client.get(ANALYTICS, headers=_bearer(secret))).status_code == 200


async def test_the_emergency_stop_cannot_be_undone_by_enable(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    account = await _create(client, headers)
    credential = await _issue(client, headers, account["id"])

    stopped = await client.post(
        f"/api/service-accounts/{account['id']}/emergency-disable",
        json={"reason": "key seen in a public gist"},
        headers=headers,
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["emergency_disabled"] is True
    assert (await client.get(ANALYTICS, headers=_bearer(credential["secret"]))).status_code == 401

    refused = await client.post(
        f"/api/service-accounts/{account['id']}/enable", json={}, headers=headers
    )
    assert refused.status_code == 403, refused.text
    assert "emergency" in refused.text.lower()


async def test_lifting_an_emergency_stop_needs_an_owner_and_a_reason(client, db, tenant_a, owner_a):
    owner_headers = await auth_headers(client, owner_a)
    admin = await make_user(db, tenant_a, UserRole.ADMIN)
    admin_headers = await auth_headers(client, admin)

    account = await _create(client, owner_headers)
    await client.post(
        f"/api/service-accounts/{account['id']}/emergency-disable",
        json={},
        headers=admin_headers,
    )

    # An administrator may trip the stop...
    not_owner = await client.post(
        f"/api/service-accounts/{account['id']}/emergency-clear",
        json={"reason": "verified with the vendor, safe to resume"},
        headers=admin_headers,
    )
    assert not_owner.status_code == 403, not_owner.text
    assert not_owner.json()["detail"]["code"] == "owner_required"

    # ...and only an owner may lift it, with a written reason.
    no_reason = await client.post(
        f"/api/service-accounts/{account['id']}/emergency-clear",
        json={"reason": "ok"},
        headers=owner_headers,
    )
    assert no_reason.status_code == 422, no_reason.text

    cleared = await client.post(
        f"/api/service-accounts/{account['id']}/emergency-clear",
        json={"reason": "false positive: the key was a test fixture"},
        headers=owner_headers,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["emergency_disabled"] is False


async def test_deleting_an_account_revokes_its_credentials(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    account = await _create(client, headers)
    credential = await _issue(client, headers, account["id"])

    deleted = await client.delete(f"/api/service-accounts/{account['id']}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    assert (await client.get(ANALYTICS, headers=_bearer(credential["secret"]))).status_code == 401

    gone = await client.get(f"/api/service-accounts/{account['id']}", headers=headers)
    assert gone.status_code == 404, gone.text


async def test_a_machine_credential_cannot_manage_machine_credentials(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    account = await _create(client, headers)
    credential = await _issue(client, headers, account["id"])

    refused = await client.get("/api/service-accounts", headers=_bearer(credential["secret"]))
    assert refused.status_code == 403, refused.text

    refused_write = await client.post(
        "/api/service-accounts",
        json={"name": "escalated", "scopes": []},
        headers=_bearer(credential["secret"]),
    )
    assert refused_write.status_code == 403, refused_write.text

    refused_key = await client.post(
        "/api/api-keys", json={"name": "escalated", "scopes": []},
        headers=_bearer(credential["secret"]),
    )
    assert refused_key.status_code == 403, refused_key.text
    # Refused before the handler or inside it, depending on the credential's
    # scopes; either way the answer is a refusal that names a permission or the
    # human-session rule, never a minted key.
    assert failure_detail(refused_key), "a refusal must say why"


async def test_a_credential_cannot_be_granted_more_than_its_creator_holds(client, db, tenant_a, owner_a):
    """The scope ceiling is the maker's own permission set.

    An agent may hold the service-account permission in a tenant that granted
    it, but they cannot hand a machine an identity-management scope they do not
    hold themselves.
    """
    agent = await make_user(db, tenant_a, UserRole.AGENT)
    agent_headers = await auth_headers(client, agent)

    response = await client.post(
        "/api/service-accounts",
        json={
            "name": "over-reach",
            "scopes": [Permission.TENANT_DELETE.value, Permission.API_KEY_MANAGE.value],
        },
        headers=agent_headers,
    )
    # 403 when the caller cannot manage service accounts at all; 400/403 when
    # they can but are asking for a scope they do not hold. Either way: refused,
    # and nothing was created.
    assert response.status_code in (400, 403), response.text
    listed = await client.get("/api/service-accounts", headers=agent_headers)
    assert "over-reach" not in listed.text


async def test_another_tenants_admin_cannot_touch_the_account(client, db, owner_a, owner_b):
    owner_headers = await auth_headers(client, owner_a)
    account = await _create(client, owner_headers)
    credential = await _issue(client, owner_headers, account["id"])

    other = await auth_headers(client, owner_b)
    assert (await client.get(f"/api/service-accounts/{account['id']}", headers=other)).status_code == 404
    assert (
        await client.patch(
            f"/api/service-accounts/{account['id']}", json={"name": "hijacked"}, headers=other
        )
    ).status_code == 404
    assert (
        await client.post(f"/api/service-accounts/{account['id']}/disable", json={}, headers=other)
    ).status_code == 404
    assert (
        await client.delete(f"/api/service-accounts/{account['id']}", headers=other)
    ).status_code == 404

    # The account and its credential are untouched.
    still_there = await client.get(
        f"/api/service-accounts/{account['id']}", headers=owner_headers
    )
    assert still_there.status_code == 200
    assert still_there.json()["name"] == "nightly exporter"
    assert (await client.get(ANALYTICS, headers=_bearer(credential["secret"]))).status_code == 200
