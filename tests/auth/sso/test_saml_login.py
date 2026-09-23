"""The SAML login, end to end, through the real HTTP endpoints.

``POST /auth/sso/{slug}/start`` builds an ``AuthnRequest`` and returns the URL
the browser visits; the IdP answers by POSTing an assertion to
``POST /auth/sso/{slug}/acs``. This file plays the IdP: it reads the request id
out of the deflated AuthnRequest — so the assertion answers *this* attempt — and
signs the assertion with a real key pair, using ``signxml`` as an independent
implementation of XML-DSig.

Every refusal is asserted to be the same generic ``sso_failed`` body, because a
difference between "bad signature" and "unknown state" is a probe.

The single-use rules (an assertion id can only ever be spent once) are enforced
by the database, not by an in-memory check, so they are asserted by looking at
the row after the refusal.
"""
from __future__ import annotations

import base64
import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from app.auth.identity.models import SSOLoginAttempt, UserSession
from app.db.models import User
from tests.auth.sso.providers import (
    create_saml_connection,
    post_assertion,
    start_saml_login,
)

pytestmark = pytest.mark.asyncio


def _refused(response) -> None:
    """Every public refusal looks the same, and carries no token."""
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "sso_failed"
    assert "access_token" not in response.text
    assert "detail" not in response.text, "the reason is logged, never returned"


async def test_a_signed_assertion_creates_a_user_and_a_session(client, db, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    completed = await post_assertion(client, idp, started=started)
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["access_token"]
    assert body["created"] is True
    assert body["linked"] is False

    user = (
        await db.execute(select(User).where(User.email == "ada@acme.test"))
    ).scalar_one()
    assert user.tenant_id == owner_a.tenant_id
    assert user.role.value == "agent"
    assert user.is_active is True

    sessions = (
        await db.execute(select(UserSession).where(UserSession.user_id == user.id))
    ).scalars().all()
    assert len(sessions) == 1
    assert sessions[0].auth_method == "sso"
    assert sessions[0].mfa_verified is False, "the IdP asserted no second factor"
    assert str(sessions[0].sso_connection_id) == connection["id"]


async def test_a_second_assertion_reuses_the_same_account(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)

    first = await start_saml_login(client)
    assert (await post_assertion(client, idp, started=first)).status_code == 200

    second = await start_saml_login(client)
    again = await post_assertion(client, idp, started=second)
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False

    users = (await db.execute(select(User).where(User.email == "ada@acme.test"))).scalars().all()
    assert len(users) == 1


async def test_an_unsigned_assertion_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    _refused(await post_assertion(client, idp, started=started, response=idp.response(
        request_id=started["request_id"], sign=False
    )))


async def test_an_assertion_signed_by_an_untrusted_certificate_is_refused(
    client, db, owner_a, idp, other_idp
):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    _refused(await post_assertion(client, other_idp, started=started))


async def test_an_assertion_for_another_request_is_refused(client, db, owner_a, idp):
    """The classic replay: an assertion minted for one attempt used for another."""
    await create_saml_connection(client, owner_a, idp)
    first = await start_saml_login(client)

    second = await start_saml_login(client)
    stale = idp.response(request_id=first["request_id"])

    _refused(await post_assertion(client, idp, started=second, response=stale))


async def test_a_tampered_assertion_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    signed = idp.response(request_id=started["request_id"], email="ada@acme.test")
    tampered = signed.replace(b"ada@acme.test", b"evil@acme.test")
    assert tampered != signed

    _refused(await post_assertion(client, idp, started=started, response=tampered))
    users = (await db.execute(select(User).where(User.email == "evil@acme.test"))).scalars().all()
    assert users == []


async def test_an_assertion_cannot_be_used_twice(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)
    assertion_id = f"_{uuid.uuid4().hex}"
    signed = idp.response(request_id=started["request_id"], assertion_id=assertion_id)

    first = await post_assertion(client, idp, started=started, response=signed)
    assert first.status_code == 200, first.text

    # The same assertion, in a fresh attempt that has its own valid state.
    second = await start_saml_login(client)
    replay = idp.response(request_id=second["request_id"], assertion_id=assertion_id)
    _refused(await post_assertion(client, idp, started=second, response=replay))

    attempts = (
        await db.execute(
            select(SSOLoginAttempt).execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert any(attempt.outcome == "failed" for attempt in attempts)


async def test_an_expired_assertion_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)

    _refused(await post_assertion(client, idp, started=started, response=idp.response(
        request_id=started["request_id"], not_on_or_after=past, subject_not_on_or_after=past
    )))


async def test_an_assertion_from_the_future_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)

    _refused(await post_assertion(client, idp, started=started, response=idp.response(
        request_id=started["request_id"], not_before=later, not_on_or_after=later + dt.timedelta(minutes=5)
    )))


async def test_an_assertion_whose_subject_confirmation_expired_is_refused(client, db, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)
    # Well outside the allowed clock skew (settings.sso_clock_skew_seconds), so
    # the refusal is about expiry and not about tolerance.
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)

    _refused(await post_assertion(client, idp, started=started, response=idp.response(
        request_id=started["request_id"], subject_not_on_or_after=past
    )))
    assert connection["id"]


async def test_an_assertion_for_another_audience_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    _refused(await post_assertion(client, idp, started=started, response=idp.response(
        request_id=started["request_id"], audience="https://someone-else.example.net"
    )))


async def test_an_assertion_delivered_to_another_recipient_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    _refused(await post_assertion(client, idp, started=started, response=idp.response(
        request_id=started["request_id"], recipient="https://evil.example.net/acs"
    )))


async def test_an_assertion_addressed_to_another_destination_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    _refused(await post_assertion(client, idp, started=started, response=idp.response(
        request_id=started["request_id"], destination="https://evil.example.net/acs"
    )))


async def test_an_assertion_from_another_issuer_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    _refused(await post_assertion(client, idp, started=started, response=idp.response(
        request_id=started["request_id"], entity="https://someone-else.example.net"
    )))


async def test_a_failed_status_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    _refused(await post_assertion(client, idp, started=started, response=idp.response(
        request_id=started["request_id"],
        status="urn:oasis:names:tc:SAML:2.0:status:Responder",
    )))


async def test_an_unknown_relay_state_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)
    started["state"] = "vdsso_not-a-real-state"

    _refused(await post_assertion(client, idp, started=started))


async def test_a_state_from_another_connection_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp, slug="alpha")
    await create_saml_connection(client, owner_a, idp, slug="beta")
    started = await start_saml_login(client, slug="alpha")

    refused = await post_assertion(client, idp, started=started, slug="beta")
    assert refused.status_code == 400, refused.text
    assert "access_token" not in refused.text


async def test_a_malformed_response_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    not_base64 = await client.post(
        "/auth/sso/acme/acs",
        data={"SAMLResponse": "this is not base64", "RelayState": started["state"]},
    )
    assert not_base64.status_code == 400, not_base64.text

    not_xml = await client.post(
        "/auth/sso/acme/acs",
        data={
            "SAMLResponse": base64.b64encode(b"not xml at all").decode(),
            "RelayState": started["state"],
        },
    )
    assert not_xml.status_code == 400, not_xml.text
    assert "access_token" not in not_xml.text


async def test_a_doctype_declaration_is_refused(client, db, owner_a, idp):
    """No DTD means no entity expansion and no external fetch, ever."""
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "boom">]><samlp:Response/>'
    )
    refused = await client.post(
        "/auth/sso/acme/acs",
        data={
            "SAMLResponse": base64.b64encode(bomb).decode(),
            "RelayState": started["state"],
        },
    )
    assert refused.status_code == 400, refused.text


async def test_a_disabled_connection_refuses_the_start_and_the_acs(client, db, owner_a, idp):
    from tests.conftest import auth_headers

    connection = await create_saml_connection(client, owner_a, idp)
    headers = await auth_headers(client, owner_a)
    disabled = await client.post(
        f"/api/sso/connections/{connection['id']}/status",
        json={"status": "disabled"},
        headers=headers,
    )
    assert disabled.status_code == 200, disabled.text

    started = await client.post("/auth/sso/acme/start")
    assert started.status_code == 404, started.text
    assert started.json()["code"] == "sso_unavailable"

    acs = await client.post(
        "/auth/sso/acme/acs", data={"SAMLResponse": "AAAA", "RelayState": "vdsso_x"}
    )
    assert acs.status_code == 404, acs.text


async def test_the_name_id_format_can_be_pinned(client, db, owner_a, idp):
    await create_saml_connection(
        client,
        owner_a,
        idp,
        name_id_format="urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
    )
    started = await start_saml_login(client)

    # The fixture always mints an emailAddress NameID, which is now unexpected.
    _refused(await post_assertion(client, idp, started=started))

