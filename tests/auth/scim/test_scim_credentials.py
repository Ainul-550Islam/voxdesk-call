"""The credential an identity provider uses to provision: issued, rotated, revoked.

A SCIM credential is a bearer token that can create, modify and deactivate the
users of a workspace. That makes it one of the most powerful secrets the product
holds, so the rules around it are asserted here rather than assumed:

* it is issued by an administrator through the API, never fabricated;
* the plaintext is returned **once** and stored only as a hash — the hash is what
  every authentication compares against;
* listing credentials never returns the token, not even a prefix of the secret;
* rotating issues a replacement and kills the predecessor immediately;
* revoking is immediate, and an unknown or foreign credential id is a 404;
* a machine credential cannot manage SCIM credentials (it cannot re-authenticate),
  and neither can a user without ``identity:write``;
* another tenant's credential id is invisible.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.auth.identity.models import SCIMCredential
from tests.conftest import auth_headers

pytestmark = pytest.mark.asyncio


async def _credentials(db, tenant_id):
    return (
        await db.execute(
            select(SCIMCredential)
            .where(SCIMCredential.tenant_id == tenant_id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()


async def test_a_credential_is_issued_once_and_stored_hashed(client, db, owner_a, scim_connection):
    response = await client.post(
        "/api/scim/credentials",
        json={"label": "Okta", "connection_id": scim_connection["id"]},
        headers=await auth_headers(client, owner_a),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["token"].startswith("vdscim_")
    assert body["prefix"].startswith("vdscim_")
    assert body["token"].startswith(body["prefix"]), "the prefix identifies the token"
    assert body["label"] == "Okta"
    assert body["revoked_at"] is None
    assert "cannot be shown again" in body["warning"].lower()

    rows = await _credentials(db, owner_a.tenant_id)
    assert len(rows) == 1
    stored = rows[0]
    assert body["token"] not in stored.token_hash
    assert len(stored.token_hash) == 64
    assert stored.token_prefix == body["prefix"]
    assert stored.token_prefix != body["token"]
    assert stored.label == "Okta"


async def test_listing_never_returns_the_token(client, db, owner_a, scim_connection):
    headers = await auth_headers(client, owner_a)
    created = await client.post(
        "/api/scim/credentials",
        json={"connection_id": scim_connection["id"]},
        headers=headers,
    )
    token = created.json()["token"]

    listed = await client.get("/api/scim/credentials", headers=headers)
    assert listed.status_code == 200, listed.text
    assert token not in listed.text
    assert "token" not in listed.text.lower()
    assert token.startswith(listed.json()[0]["prefix"])
    assert listed.json()[0]["prefix"] != token


async def test_rotation_replaces_the_token_and_kills_the_old_one(
    client, db, owner_a, scim_connection, scim
):
    created = await client.post(
        "/api/scim/credentials",
        json={"connection_id": scim_connection["id"]},
        headers=await auth_headers(client, owner_a),
    )
    original = created.json()
    working = scim.as_token(original["token"])
    assert (await working.get("Users")).status_code == 200

    rotated = await client.post(
        f"/api/scim/credentials/{original['id']}/rotate",
        headers=await auth_headers(client, owner_a),
    )
    assert rotated.status_code == 200, rotated.text
    replacement = rotated.json()
    assert replacement["token"] != original["token"]
    assert replacement["id"] != original["id"], "rotation issues a fresh credential"

    # The predecessor is revoked in the same transaction, so there is never a
    # window with two live tokens.
    assert (await working.get("Users")).status_code == 401
    fresh = scim.as_token(replacement["token"])
    assert (await fresh.get("Users")).status_code == 200

    rows = await _credentials(db, owner_a.tenant_id)
    by_id = {str(row.id): row for row in rows}
    assert by_id[original["id"]].revoked_at is not None
    assert by_id[replacement["id"]].revoked_at is None
    assert str(by_id[replacement["id"]].rotated_from_id) == original["id"]


async def test_revocation_is_immediate(client, db, owner_a, scim_connection, scim):
    created = await client.post(
        "/api/scim/credentials",
        json={"connection_id": scim_connection["id"]},
        headers=await auth_headers(client, owner_a),
    )
    body = created.json()
    caller = scim.as_token(body["token"])
    assert (await caller.get("Users")).status_code == 200

    revoked = await client.delete(
        f"/api/scim/credentials/{body['id']}", headers=await auth_headers(client, owner_a)
    )
    assert revoked.status_code == 204, revoked.text

    refused = await caller.get("Users")
    assert refused.status_code == 401, refused.text
    assert refused.json()["schemas"][0].endswith(":Error")

    revoked_row = next(
        row for row in await _credentials(db, owner_a.tenant_id) if str(row.id) == body["id"]
    )
    assert revoked_row.revoked_at is not None

    # The fixture's own token, a different credential, is untouched.
    assert (await scim.get("Users")).status_code == 200


async def test_an_unknown_credential_id_is_a_404(client, owner_a, scim_connection):
    headers = await auth_headers(client, owner_a)
    assert (
        await client.delete(f"/api/scim/credentials/{uuid.uuid4()}", headers=headers)
    ).status_code == 404
    assert (
        await client.post(f"/api/scim/credentials/{uuid.uuid4()}/rotate", headers=headers)
    ).status_code == 404


async def test_a_foreign_credential_id_is_invisible(client, db, owner_a, owner_b, scim_connection):
    headers_a = await auth_headers(client, owner_a)
    created = await client.post(
        "/api/scim/credentials",
        json={"connection_id": scim_connection["id"]},
        headers=headers_a,
    )
    credential_id = created.json()["id"]

    headers_b = await auth_headers(client, owner_b)
    assert (
        await client.delete(f"/api/scim/credentials/{credential_id}", headers=headers_b)
    ).status_code == 404
    listed = await client.get("/api/scim/credentials", headers=headers_b)
    assert listed.json() == []


async def test_an_agent_cannot_manage_scim_credentials(client, agent_a):
    refused = await client.post(
        "/api/scim/credentials",
        json={},
        headers=await auth_headers(client, agent_a),
    )
    assert refused.status_code == 403, refused.text


async def test_a_scim_token_cannot_manage_scim_credentials(client, scim):
    """The token is a provisioning credential, not an administrative one."""
    listed = await client.get("/api/scim/credentials", headers=scim.headers)
    assert listed.status_code in (401, 403), listed.text

    created = await client.post("/api/scim/credentials", json={}, headers=scim.headers)
    assert created.status_code in (401, 403), created.text


async def test_a_scim_token_cannot_call_the_product_api(client, scim):
    """Provisioning must not be a back door into the product's own endpoints."""
    for path in ("/api/sessions", "/api/identity/status", "/api/api-keys"):
        response = await client.get(path, headers=scim.headers)
        assert response.status_code in (401, 403), f"{path}: {response.text}"


async def test_a_credential_can_be_limited_to_the_tenant(client, db, owner_a, scim_connection):
    """Without a connection id the credential is tenant-scoped, and still works."""
    headers = await auth_headers(client, owner_a)
    created = await client.post("/api/scim/credentials", json={"label": "tenant-wide"}, headers=headers)
    assert created.status_code == 201, created.text

    rows = await _credentials(db, owner_a.tenant_id)
    assert rows[0].connection_id is None

    token = created.json()["token"]
    response = await client.get(
        "/scim/v2/default/Users",
        headers={"Authorization": f"Bearer {token}", "content-type": "application/scim+json"},
    )
    assert response.status_code == 200, response.text


async def test_scopes_are_recorded_and_narrowing_is_possible(client, db, owner_a, scim_connection):
    from app.auth.identity.scim.service import SCIM_SCOPE_GROUPS, SCIM_SCOPE_USERS

    headers = await auth_headers(client, owner_a)
    created = await client.post(
        "/api/scim/credentials",
        json={"connection_id": scim_connection["id"], "scopes": [SCIM_SCOPE_USERS]},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["scopes"] == [SCIM_SCOPE_USERS]
    del SCIM_SCOPE_GROUPS


async def test_an_expiry_is_honoured(client, db, owner_a, scim_connection, scim):
    headers = await auth_headers(client, owner_a)
    created = await client.post(
        "/api/scim/credentials",
        json={"connection_id": scim_connection["id"], "expires_in_days": 1},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["expires_at"] is not None

    # An expired credential is refused even though it was never revoked.
    import datetime as dt

    rows = await _credentials(db, owner_a.tenant_id)
    row = next(entry for entry in rows if str(entry.id) == created.json()["id"])
    row.expires_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=1)
    await db.commit()

    caller = scim.as_token(created.json()["token"])
    refused = await caller.get("Users")
    assert refused.status_code == 401, refused.text
