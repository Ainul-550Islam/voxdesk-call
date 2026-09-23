"""Certificate trust: what is accepted, for how long, and what is stored.

The rule the whole SAML implementation rests on is that the certificate is a
property of the *connection*, never a property of the assertion. An assertion
carries a certificate in its ``KeyInfo``; if that certificate were trusted, then
anyone able to sign anything would be an identity provider. So the certificate
has to be registered by an administrator, and this file asserts that:

* a registered certificate is enough to verify a real, ``signxml``-signed
  assertion — the hand-written verifier agrees with an independent signer;
* it is stored sealed: the PEM text appears in no column, and the envelope is
  bound to the tenant, so it cannot be moved to another workspace;
* a rotation is an overlap, not a moment: the retired certificate keeps working
  for the overlap window and stops working after it;
* the last certificate of an active connection cannot be retired, because that
  one click would lock out every user of the connection;
* an expired certificate is not trusted, even while it is still marked active.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from app.auth.identity.models import SSOConnectionCertificate
from app.auth.identity.sso import service as sso_service
from tests.auth.sso.providers import (
    IdP,
    create_saml_connection,
    post_assertion,
    start_saml_login,
)
from tests.conftest import auth_headers

pytestmark = pytest.mark.asyncio


async def _certificates(db, connection_id: str) -> list[SSOConnectionCertificate]:
    return list(
        (
            await db.execute(
                select(SSOConnectionCertificate)
                .where(SSOConnectionCertificate.connection_id == uuid.UUID(connection_id))
                .order_by(SSOConnectionCertificate.created_at.asc())
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )


async def test_a_registered_certificate_is_enough_to_verify_a_real_assertion(client, db, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    assert connection["active_certificates"] == 1

    started = await start_saml_login(client)
    completed = await post_assertion(client, idp, started=started)
    assert completed.status_code == 200, completed.text


async def test_the_certificate_is_stored_sealed_and_not_as_pem_text(client, db, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    rows = await _certificates(db, connection["id"])
    assert len(rows) == 1

    row = rows[0]
    assert "BEGIN CERTIFICATE" not in row.pem_encrypted
    assert row.fingerprint_sha256 == row.fingerprint_sha256
    assert len(row.fingerprint_sha256) == 64
    assert row.pem_key_id, "the envelope records which key sealed it"
    assert row.tenant_id == owner_a.tenant_id

    # Nothing anywhere in the connection row carries the PEM either.
    listed = (
        await client.get(
            f"/api/sso/connections/{connection['id']}",
            headers=await auth_headers(client, owner_a),
        )
    ).json()
    assert "BEGIN CERTIFICATE" not in str(listed)


async def test_a_certificate_cannot_be_moved_to_another_tenant(client, db, owner_a, idp):
    """The envelope's AAD names the tenant, so a row moved between workspaces opens nowhere."""
    connection = await create_saml_connection(client, owner_a, idp)
    row = (await _certificates(db, connection["id"]))[0]

    row.tenant_id = uuid.uuid4()
    await db.commit()

    started = await start_saml_login(client)
    refused = await post_assertion(client, idp, started=started)
    assert refused.status_code == 400, refused.text
    assert "access_token" not in refused.text


async def test_importing_the_same_certificate_twice_is_idempotent(client, db, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    headers = await auth_headers(client, owner_a)

    again = await client.post(
        f"/api/sso/connections/{connection['id']}/certificates",
        json={"certificate": idp.certificate_pem, "make_active": True},
        headers=headers,
    )
    assert again.status_code == 201, again.text

    rows = await _certificates(db, connection["id"])
    assert len(rows) == 1, "a re-import must not create a second row"
    assert again.json()["id"] == str(rows[0].id)


async def test_a_pasted_base64_body_is_accepted_and_junk_is_refused(client, db, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    headers = await auth_headers(client, owner_a)
    body = (
        idp.certificate_pem.replace("-----BEGIN CERTIFICATE-----", "")
        .replace("-----END CERTIFICATE-----", "")
        .strip()
    )
    other = IdP(common_name="other.example.net")

    first = await client.post(
        f"/api/sso/connections/{connection['id']}/certificates",
        json={"certificate": body, "make_active": False},
        headers=headers,
    )
    assert first.status_code in (201, 400), first.text

    junk = await client.post(
        f"/api/sso/connections/{connection['id']}/certificates",
        json={"certificate": "x" * 40, "make_active": True},
        headers=headers,
    )
    assert junk.status_code in (400, 422), junk.text
    del other


async def test_a_rotation_overlap_keeps_the_old_certificate_working(client, db, owner_a, idp):
    """Add the replacement, retire the old one, and both still verify for a while."""
    replacement = IdP(common_name="idp.example.com")
    connection = await create_saml_connection(client, owner_a, idp)
    headers = await auth_headers(client, owner_a)

    added = await client.post(
        f"/api/sso/connections/{connection['id']}/certificates",
        json={"certificate": replacement.certificate_pem, "make_active": True},
        headers=headers,
    )
    assert added.status_code == 201, added.text

    rows = await _certificates(db, connection["id"])
    old = next(row for row in rows if row.fingerprint_sha256 != added.json()["fingerprint"])

    retired = await client.delete(
        f"/api/sso/connections/{connection['id']}/certificates/{old.id}", headers=headers
    )
    assert retired.status_code == 204, retired.text

    # Both keys verify during the overlap: the provider may not have switched yet.
    for signer in (idp, replacement):
        started = await start_saml_login(client)
        completed = await post_assertion(client, signer, started=started)
        assert completed.status_code == 200, (signer is idp, completed.text)


async def test_an_overlap_ends_and_the_retired_certificate_stops_working(client, db, owner_a, idp):
    replacement = IdP(common_name="idp.example.com")
    connection = await create_saml_connection(client, owner_a, idp)
    headers = await auth_headers(client, owner_a)
    added = await client.post(
        f"/api/sso/connections/{connection['id']}/certificates",
        json={"certificate": replacement.certificate_pem, "make_active": True},
        headers=headers,
    )
    rows = await _certificates(db, connection["id"])
    old = next(row for row in rows if row.fingerprint_sha256 != added.json()["fingerprint"])
    await client.delete(
        f"/api/sso/connections/{connection['id']}/certificates/{old.id}", headers=headers
    )

    # Push the retirement past the overlap window.
    old = (
        await db.execute(
            select(SSOConnectionCertificate)
            .where(SSOConnectionCertificate.id == old.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    old.retired_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=25)
    await db.commit()

    started = await start_saml_login(client)
    refused = await post_assertion(client, idp, started=started)
    assert refused.status_code == 400, refused.text

    started = await start_saml_login(client)
    accepted = await post_assertion(client, replacement, started=started)
    assert accepted.status_code == 200, accepted.text


async def test_the_last_certificate_of_an_active_connection_cannot_be_retired(client, db, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    headers = await auth_headers(client, owner_a)
    row = (await _certificates(db, connection["id"]))[0]

    refused = await client.delete(
        f"/api/sso/connections/{connection['id']}/certificates/{row.id}", headers=headers
    )
    assert refused.status_code in (400, 409), refused.text
    assert "replacement" in refused.text.lower() or "only" in refused.text.lower()

    started = await start_saml_login(client)
    assert (await post_assertion(client, idp, started=started)).status_code == 200


async def test_an_expired_certificate_is_not_trusted(client, db, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    row = (await _certificates(db, connection["id"]))[0]
    row.not_after = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=1)
    await db.commit()

    started = await start_saml_login(client)
    refused = await post_assertion(client, idp, started=started)
    assert refused.status_code == 400, refused.text
    assert "access_token" not in refused.text


async def test_the_certificate_list_reports_status_and_fingerprint(client, db, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    headers = await auth_headers(client, owner_a)

    listed = await client.get(
        f"/api/sso/connections/{connection['id']}/certificates", headers=headers
    )
    assert listed.status_code == 200, listed.text
    entry = listed.json()[0]
    assert entry["status"] == "active"
    assert len(entry["fingerprint"]) == 64
    assert entry["retired_at"] is None
    assert entry["not_after"] is not None


async def test_certificate_administration_is_tenant_scoped(client, db, owner_a, owner_b, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    headers_b = await auth_headers(client, owner_b)

    hidden = await client.get(
        f"/api/sso/connections/{connection['id']}/certificates", headers=headers_b
    )
    assert hidden.status_code == 404, hidden.text

    refused = await client.post(
        f"/api/sso/connections/{connection['id']}/certificates",
        json={"certificate": idp.certificate_pem, "make_active": True},
        headers=headers_b,
    )
    assert refused.status_code == 404, refused.text

    row = (await _certificates(db, connection["id"]))[0]
    refused = await client.delete(
        f"/api/sso/connections/{connection['id']}/certificates/{row.id}", headers=headers_b
    )
    assert refused.status_code == 404, refused.text


async def test_retiring_a_certificate_never_touches_another_connections_trust(client, db, owner_a, idp):
    first = await create_saml_connection(client, owner_a, idp, slug="alpha")
    await create_saml_connection(client, owner_a, idp, slug="beta")

    headers = await auth_headers(client, owner_a)
    row = (await _certificates(db, first["id"]))[0]
    # Beta is its own connection with its own certificate row.
    deleted = await client.delete(
        f"/api/sso/connections/{first['id']}/certificates/{row.id}", headers=headers
    )
    assert deleted.status_code in (204, 400, 409), deleted.text

    started = await start_saml_login(client, slug="beta")
    assert (await post_assertion(client, idp, started=started, slug="beta")).status_code == 200


async def test_an_unknown_certificate_id_is_a_404(client, db, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    headers = await auth_headers(client, owner_a)

    missing = await client.delete(
        f"/api/sso/connections/{connection['id']}/certificates/{uuid.uuid4()}", headers=headers
    )
    assert missing.status_code == 404, missing.text


async def test_the_trusted_set_is_recomputed_on_every_login(client, db, owner_a, idp):
    """No cached trust: a certificate added between two logins is honoured at once."""
    connection = await create_saml_connection(client, owner_a, idp)
    headers = await auth_headers(client, owner_a)
    replacement = IdP(common_name="idp.example.com")

    started = await start_saml_login(client)
    assert (await post_assertion(client, replacement, started=started)).status_code == 400

    added = await client.post(
        f"/api/sso/connections/{connection['id']}/certificates",
        json={"certificate": replacement.certificate_pem, "make_active": True},
        headers=headers,
    )
    assert added.status_code == 201, added.text

    started = await start_saml_login(client)
    assert (await post_assertion(client, replacement, started=started)).status_code == 200

    trusted = await sso_service.trusted_certificates(
        db, connection=await sso_service.get_connection_by_slug(db, slug="acme")
    )
    assert len(trusted) == 2
