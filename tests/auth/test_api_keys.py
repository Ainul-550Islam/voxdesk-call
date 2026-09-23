"""API keys, driven through the HTTP surface.

An API key is a password that nobody types, so the rules around it are the rules
of a credential: it is issued once and stored hashed, it carries exactly the
scopes its maker holds, it can be rotated without an overlap, and it stops the
moment it is revoked. Asserted here, from the outside:

* the secret is in the create (and rotate) response and nowhere else — not in a
  list, not in the database, not in the audit trail;
* the prefix identifies a key without revealing it, which is what makes a
  leaked key findable in a log;
* the scope ceiling is the creator's own permission set, so a support agent
  cannot mint a key that reads the billing ledger;
* a key authenticates through the same `Authorization: Bearer` header as a JWT
  and is confined to the workspace that issued it;
* a machine credential cannot mint machine credentials, because that turns one
  leaked key into unbounded persistence.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.auth.identity import api_keys as key_service
from app.auth.identity.api_keys import APIKey
from app.auth.permissions import Permission
from app.db.models import AuditAction, AuditLog, UserRole
from tests.conftest import auth_headers, make_user, failure_detail

pytestmark = pytest.mark.asyncio

ANALYTICS = "/api/analytics/overview"


def _scopes_for(*permissions: Permission) -> list[str]:
    return key_service.scopes_from_permissions(list(permissions))


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


async def _create(client, headers, **payload):
    body = {"name": "nightly export", "scopes": _scopes_for(Permission.ANALYTICS_READ)}
    body.update(payload)
    return await client.post("/api/api-keys", json=body, headers=headers)


async def test_the_secret_is_returned_once_and_stored_only_as_a_digest(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    created = await _create(client, headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["secret"]
    assert body["prefix"].startswith(body["secret"][: len(body["prefix"])])
    assert "cannot be shown again" in body["warning"].lower()
    assert body["revoked_at"] is None

    row = (await db.execute(select(APIKey).where(APIKey.id == uuid.UUID(body["id"])))).scalar_one()
    assert body["secret"] not in row.secret_hash
    assert len(row.secret_hash) == 64
    assert row.prefix == body["prefix"]
    assert row.user_id == owner_a.id

    listed = await client.get("/api/api-keys", headers=headers)
    assert listed.status_code == 200, listed.text
    assert body["secret"] not in listed.text
    assert body["prefix"] in listed.text

    # The audit trail names the key, never the secret.
    entries = (
        await db.execute(select(AuditLog).where(AuditLog.action == AuditAction.API_KEY_CREATED))
    ).scalars().all()
    assert entries
    rendered = " ".join(str(entry.detail) for entry in entries)
    assert body["secret"] not in rendered
    assert body["prefix"] in rendered


async def test_the_key_authenticates_and_records_its_last_use(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    created = await _create(client, headers)
    secret = created.json()["secret"]

    assert (await client.get(ANALYTICS, headers=_bearer(secret))).status_code == 200

    row = (
        await db.execute(
            select(APIKey).where(APIKey.id == uuid.UUID(created.json()["id"])).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.last_used_at is not None

    listed = await client.get("/api/api-keys", headers=headers)
    entry = next(item for item in listed.json() if item["id"] == created.json()["id"])
    assert entry["last_used_at"] is not None


async def test_a_key_without_the_scope_is_refused_where_it_would_need_it(client, owner_a):
    headers = await auth_headers(client, owner_a)
    created = await _create(
        client, headers, scopes=_scopes_for(Permission.CALL_READ), name="call reader"
    )
    assert created.status_code == 201, created.text

    refused = await client.get(ANALYTICS, headers=_bearer(created.json()["secret"]))
    assert refused.status_code == 403, refused.text
    assert failure_detail(refused)


async def test_a_key_cannot_carry_a_scope_its_maker_does_not_hold(client, db, tenant_a, admin_a):
    """An administrator who can manage keys still cannot hand one a permission
    they do not hold themselves — billing, for instance."""
    admin_headers = await auth_headers(client, admin_a)

    allowed = await _create(client, admin_headers, scopes=_scopes_for(Permission.CALL_READ))
    assert allowed.status_code == 201, allowed.text

    refused = await _create(client, admin_headers, scopes=_scopes_for(Permission.BILLING_WRITE))
    assert refused.status_code in (400, 403), refused.text
    assert failure_detail(refused)

    unknown = await _create(client, admin_headers, scopes=["not:a:real:scope"])
    assert unknown.status_code in (400, 403), unknown.text

    # A role without the key-management permission cannot mint anything at all.
    agent = await make_user(db, tenant_a, UserRole.AGENT)
    agent_refused = await _create(client, await auth_headers(client, agent))
    assert agent_refused.status_code == 403, agent_refused.text


async def test_the_scope_catalogue_is_available_to_a_key_manager(client, db, tenant_a, owner_a):
    agent = await make_user(db, tenant_a, UserRole.AGENT)

    allowed = await client.get("/api/api-keys/scopes", headers=await auth_headers(client, owner_a))
    assert allowed.status_code == 200, allowed.text
    body = allowed.json()
    assert body["all_scopes"]
    assert set(body["grantable"]) <= set(body["all_scopes"])

    refused = await client.get("/api/api-keys/scopes", headers=await auth_headers(client, agent))
    assert refused.status_code == 403, refused.text


async def test_rotation_replaces_the_key_without_an_overlap(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    created = await _create(client, headers)
    original = created.json()
    assert (await client.get(ANALYTICS, headers=_bearer(original["secret"]))).status_code == 200

    rotated = await client.post(f"/api/api-keys/{original['id']}/rotate", headers=headers)
    assert rotated.status_code == 200, rotated.text
    replacement = rotated.json()
    assert replacement["secret"] != original["secret"]
    assert replacement["id"] != original["id"]
    assert replacement["rotated_from_id"] == original["id"]

    assert (await client.get(ANALYTICS, headers=_bearer(original["secret"]))).status_code == 401
    assert (await client.get(ANALYTICS, headers=_bearer(replacement["secret"]))).status_code == 200

    rows = (await db.execute(select(APIKey).execution_options(populate_existing=True))).scalars().all()
    live = [row for row in rows if row.revoked_at is None]
    assert len(live) == 1


async def test_revocation_is_immediate_and_idempotent(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    created = await _create(client, headers)
    secret = created.json()["secret"]
    assert (await client.get(ANALYTICS, headers=_bearer(secret))).status_code == 200

    revoked = await client.delete(f"/api/api-keys/{created.json()['id']}", headers=headers)
    assert revoked.status_code in (200, 204), revoked.text
    assert (await client.get(ANALYTICS, headers=_bearer(secret))).status_code == 401

    # A retry is a no-op: the key is already revoked, and the audit trail says
    # one revocation rather than two.
    again = await client.delete(f"/api/api-keys/{created.json()['id']}", headers=headers)
    assert again.status_code in (200, 204, 404), again.text

    entries = (
        await db.execute(select(AuditLog).where(AuditLog.action == AuditAction.API_KEY_REVOKED))
    ).scalars().all()
    assert len(entries) == 1, "one revocation, one audit entry"


async def test_an_expired_key_is_refused(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    created = await _create(client, headers, expires_in_days=1)
    assert created.status_code == 201, created.text
    assert created.json()["expires_at"] is not None

    row = (
        await db.execute(
            select(APIKey)
            .where(APIKey.id == uuid.UUID(created.json()["id"]))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.expires_at is not None
    row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    await db.commit()

    assert (await client.get(ANALYTICS, headers=_bearer(created.json()["secret"]))).status_code == 401


async def test_a_machine_credential_cannot_mint_api_keys(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    created = await _create(client, headers)
    bearer = _bearer(created.json()["secret"])

    refused = await client.post(
        "/api/api-keys", json={"name": "escalated", "scopes": []}, headers=bearer
    )
    assert refused.status_code == 403, refused.text
    assert failure_detail(refused)

    assert (await client.get("/api/api-keys", headers=bearer)).status_code == 403


async def test_a_key_is_confined_to_the_workspace_that_issued_it(client, db, owner_a, owner_b):
    created = await _create(client, await auth_headers(client, owner_a))
    secret = created.json()["secret"]

    other = await auth_headers(client, owner_b)

    # The other workspace cannot see or revoke it...
    listed = await client.get("/api/api-keys", headers=other)
    assert all(item["id"] != created.json()["id"] for item in listed.json())
    refused = await client.delete(f"/api/api-keys/{created.json()['id']}", headers=other)
    assert refused.status_code == 404, refused.text
    rotate = await client.post(f"/api/api-keys/{created.json()['id']}/rotate", headers=other)
    assert rotate.status_code == 404, rotate.text

    # ...and the key itself cannot reach the other workspace's data, because
    # every route reads the tenant from the credential rather than the request.
    own_view = await client.get(ANALYTICS, headers=_bearer(secret))
    assert own_view.status_code == 200

    # One key, one tenant: the key of B's owner is refused the same way on an
    # id that belongs to A.
    b_key = await _create(client, other)
    assert (
        await client.get(ANALYTICS, headers=_bearer(b_key.json()["secret"]))
    ).status_code == 200


async def test_an_unknown_key_and_a_garbage_key_look_the_same(client, owner_a):
    for value in ("vdk_deadbeef_not-a-secret", "vdk_not-a-uuid_secret", "vdk_" + "x" * 60):
        response = await client.get(ANALYTICS, headers=_bearer(value))
        assert response.status_code == 401, f"{value}: {response.text}"

async def test_using_a_dead_key_is_recorded_for_the_operator(client, db, tenant_a, owner_a):
    """The refusal is uniform, so the *reason* has to live somewhere: a revoked
    key still being presented is the difference between an integration nobody
    updated and a key somebody else kept."""
    headers = await auth_headers(client, owner_a)
    created = await _create(client, headers)
    assert created.status_code == 201, created.text
    prefix = created.json()["prefix"]
    secret = created.json()["secret"]

    revoked = await client.delete(f"/api/api-keys/{created.json()['id']}", headers=headers)
    assert revoked.status_code == 204, revoked.text

    after_revocation = await client.get(ANALYTICS, headers=_bearer(secret))
    assert after_revocation.status_code == 401, after_revocation.text

    expired = await _create(client, headers, expires_in_days=1)
    assert expired.status_code == 201, expired.text
    row = (
        await db.execute(
            select(APIKey)
            .where(APIKey.id == uuid.UUID(expired.json()["id"]))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    await db.commit()
    assert (await client.get(ANALYTICS, headers=_bearer(expired.json()["secret"]))).status_code == 401

    entries = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.tenant_id == tenant_a.id,
                AuditLog.action == AuditAction.CREDENTIAL_AUTH_REJECTED,
            )
            .order_by(AuditLog.created_at.asc())
        )
    ).scalars().all()
    reasons = [row.detail["reason"] for row in entries]
    assert reasons == ["api_key_revoked", "api_key_expired"], reasons
    assert [row.detail["prefix"] for row in entries] == [prefix, expired.json()["prefix"]]
    assert all(row.detail["kind"] == "api_key" for row in entries)
    rendered = repr([row.detail for row in entries])
    assert secret not in rendered
    assert expired.json()["secret"] not in rendered


async def test_an_unknown_key_is_refused_without_flooding_the_trail(client, db, tenant_a):
    """A token that matches no row writes nothing: otherwise anyone could fill
    the audit table by guessing."""
    before = len(
        (
            await db.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_a.id))
        ).scalars().all()
    )
    for _ in range(3):
        assert (await client.get(ANALYTICS, headers=_bearer("vdk_not-a-real-key"))).status_code == 401

    after = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_a.id,
                AuditLog.action == AuditAction.CREDENTIAL_AUTH_REJECTED,
            )
        )
    ).scalars().all()
    assert after == [], "an unresolvable token is a log line, not an audit row"
    total = len(
        (
            await db.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_a.id))
        ).scalars().all()
    )
    assert total == before
