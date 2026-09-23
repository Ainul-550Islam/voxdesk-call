"""The identity policy engine, driven end to end.

Three of the product's controls only mean something when several modules agree:
a domain claim is worth nothing unless it changes how a login is handled, an
identity policy is worth nothing unless it reaches a credential that is already
in circulation, and MFA is worth nothing unless the privileged routes consult
it. Each test here turns a control on through the API and then observes the
*other* module's behaviour, which is the only way to catch a policy that is
stored and displayed but never enforced.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.auth.identity import domains as domain_service
from app.auth.identity import policies, sessions as identity_sessions
from app.auth.permissions import Permission
from app.core.config import settings
from app.db.models import AuditAction, AuditLog, UserRole
from tests.conftest import (
    TEST_PASSWORD,
    TOTPClock,
    auth_headers,
    enroll_totp,
    login,
    make_user,
    mfa_auth_headers,
    require_mfa,
)

pytestmark = pytest.mark.asyncio

ANALYTICS = "/api/analytics/overview"
POLICY_ACTION = "/api/identity/policy"


class _StubAnswer:
    """One DNS-over-HTTPS answer, shaped like the resolver's JSON."""

    def __init__(self, records: list[str]) -> None:
        self.status_code = 200
        self._records = records

    def json(self) -> dict:
        return {
            "Status": 0,
            "Answer": [{"type": 16, "data": f'"{record}"'} for record in self._records],
        }


class _StubDoH:
    """A resolver that answers from a script, and records what was asked."""

    answers: list[str] = []
    asked: list[tuple[str, str]] = []

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    async def __aenter__(self) -> "_StubDoH":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def get(self, url, params=None, headers=None):
        del url, headers
        params = params or {}
        type(self).asked.append((params.get("name", ""), params.get("type", "")))
        return _StubAnswer(list(type(self).answers))


def _install_resolver(monkeypatch) -> type[_StubDoH]:
    monkeypatch.setattr(domain_service.httpx, "AsyncClient", _StubDoH)
    _StubDoH.answers = []
    _StubDoH.asked = []
    return _StubDoH


async def _add_domain(client, owner, domain: str) -> dict:
    headers = await auth_headers(client, owner)
    created = await client.post("/api/domains", json={"domain": domain}, headers=headers)
    assert created.status_code == 201, created.text
    return created.json()


async def _verify(client, owner, domain: dict, monkeypatch) -> dict:
    """Publish the challenge as a TXT record and settle it."""
    resolver = _install_resolver(monkeypatch)
    resolver.answers = [domain["verification_record_value"]]
    headers = await auth_headers(client, owner)
    verified = await client.post(f"/api/domains/{domain['id']}/verify", headers=headers)
    assert verified.status_code == 200, verified.text
    assert verified.json()["verified"] is True, verified.text
    return verified.json()


async def _verified_domain(client, owner, monkeypatch, domain: str = "acme.example") -> dict:
    created = await _add_domain(client, owner, domain)
    assert created["verified"] is False
    assert created["verification_record_value"]
    await _verify(client, owner, created, monkeypatch)
    return created


async def test_a_domain_is_verified_through_a_real_dns_answer(client, owner_a, monkeypatch):
    """The challenge travels through the DNS-over-HTTPS client the service uses,
    so a quoted TXT answer is parsed rather than stubbed out."""
    resolver = _install_resolver(monkeypatch)
    headers = await auth_headers(client, owner_a)
    created = await client.post("/api/domains", json={"domain": "acme.example"}, headers=headers)
    assert created.status_code == 201, created.text
    domain = created.json()

    # A near miss must not verify: the record is compared, not pattern-matched.
    resolver.answers = ["something-else"]
    near_miss = await client.post(f"/api/domains/{domain['id']}/verify", headers=headers)
    assert near_miss.status_code == 200, near_miss.text
    assert near_miss.json()["verified"] is False

    resolver.answers = [domain["verification_record_value"]]
    accepted = await client.post(f"/api/domains/{domain['id']}/verify", headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["verified"] is True
    assert accepted.json()["records_seen"] >= 1

    assert (domain["verification_record_name"], "TXT") in resolver.asked
    listed = await client.get("/api/domains", headers=headers)
    assert [row["domain"] for row in listed.json()] == ["acme.example"]
    assert listed.json()[0]["verified"] is True


async def test_enforcement_cannot_be_switched_on_for_an_unverified_domain(client, owner_a):
    headers = await auth_headers(client, owner_a)
    created = await client.post("/api/domains", json={"domain": "unverified.example"}, headers=headers)
    domain_id = created.json()["id"]

    refused = await client.patch(
        f"/api/domains/{domain_id}", json={"enforcement": "require_sso"}, headers=headers
    )
    assert refused.status_code == 400, refused.text

    still = await client.get(f"/api/domains/{domain_id}", headers=headers)
    assert still.json()["enforcement"] == "off"
    assert still.json()["verified"] is False


async def test_a_verified_domain_can_require_sso_and_the_audit_says_so(
    client, db, owner_a, tenant_a, monkeypatch
):
    domain = await _verified_domain(client, owner_a, monkeypatch)
    headers = await auth_headers(client, owner_a)

    updated = await client.patch(
        f"/api/domains/{domain['id']}",
        json={"enforcement": "require_sso", "block_password_login": True},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["enforcement"] == "require_sso"
    assert updated.json()["block_password_login"] is True

    changes = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_a.id,
                AuditLog.action == AuditAction.DOMAIN_ENFORCEMENT_CHANGED,
            )
        )
    ).scalars().all()
    assert len(changes) == 1
    assert changes[0].detail["enforcement"] == "require_sso"
    assert changes[0].detail["previous"] == "off"


async def test_a_policy_that_blocks_password_login_actually_blocks_it(
    client, db, tenant_a, owner_a, monkeypatch
):
    """The end of the chain: an administrator's decision about a domain reaches
    the password login of a person whose address is in it."""
    domain = await _verified_domain(client, owner_a, monkeypatch)
    headers = await auth_headers(client, owner_a)
    await client.patch(
        f"/api/domains/{domain['id']}",
        json={"enforcement": "require_sso", "block_password_login": True},
        headers=headers,
    )

    employee = await make_user(
        db, tenant_a, UserRole.AGENT, email="blocked@acme.example"
    )
    refused = await login(client, employee.email, TEST_PASSWORD)
    assert refused.status_code == 401, refused.text
    assert "sso" not in refused.text.lower(), "the refusal must not describe the policy"

    # Someone outside the domain is unaffected.
    outsider = await make_user(db, tenant_a, UserRole.AGENT, email="outsider@example.com")
    assert (await login(client, outsider.email, TEST_PASSWORD)).status_code == 200


async def test_an_api_key_stops_working_the_moment_the_policy_disables_keys(client, owner_a):
    """A credential already in circulation is re-checked per request: turning API
    keys off cannot wait for the next expiry."""
    from app.auth.identity import api_keys as key_service

    headers = await auth_headers(client, owner_a)
    scopes = key_service.scopes_from_permissions([Permission.ANALYTICS_READ])
    created = await client.post(
        "/api/api-keys", json={"name": "exporter", "scopes": scopes}, headers=headers
    )
    assert created.status_code == 201, created.text
    bearer = {"Authorization": f"Bearer {created.json()['secret']}"}

    assert (await client.get(ANALYTICS, headers=bearer)).status_code == 200

    off = await client.patch(POLICY_ACTION, json={"api_keys_allowed": False}, headers=headers)
    assert off.status_code == 200, off.text
    assert (await client.get(ANALYTICS, headers=bearer)).status_code == 401

    on = await client.patch(POLICY_ACTION, json={"api_keys_allowed": True}, headers=headers)
    assert on.status_code == 200, on.text
    assert (await client.get(ANALYTICS, headers=bearer)).status_code == 200


async def test_only_an_identity_writer_can_change_the_policy(client, db, tenant_a, owner_a):
    """An agent cannot edit the policy, so the only way they meet it is through
    their own login — which is where the change has to show up."""
    agent = await make_user(db, tenant_a, UserRole.AGENT)
    agent_headers = await auth_headers(client, agent)

    refused = await client.patch(POLICY_ACTION, json={"mfa_required": True}, headers=agent_headers)
    assert refused.status_code == 403, refused.text

    owner_headers = await auth_headers(client, owner_a)
    changed = await client.patch(POLICY_ACTION, json={"mfa_required": True}, headers=owner_headers)
    assert changed.status_code == 200, changed.text
    assert changed.json()["mfa_required"] is True

    # The agent has no second factor. Refusing the login would lock them out of
    # the only page where they could enrol, so the login succeeds and says what
    # is owed.
    agent_login = await login(client, agent.email, TEST_PASSWORD)
    assert agent_login.status_code == 200, agent_login.text
    assert agent_login.json().get("mfa_enrollment_required") is True


async def test_a_privileged_write_needs_a_fresh_proof_of_presence(client, db, owner_a):
    """A session that was good enough for a read is not good enough for a
    privileged write once the reauthentication window has closed — and proving
    presence again is what reopens it."""
    from app.auth.identity.models import UserSession

    headers = await auth_headers(client, owner_a)
    enabled = await client.patch(
        POLICY_ACTION,
        json={"privileged_reauth_required": True, "privileged_reauth_minutes": 5},
        headers=headers,
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["privileged_reauth_required"] is True

    first = await client.post("/api/domains", json={"domain": "fresh.example"}, headers=headers)
    assert first.status_code == 201, first.text

    # Age this session's proof of presence past the window the policy allows.
    sessions = (
        await db.execute(
            select(UserSession)
            .where(UserSession.user_id == owner_a.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert sessions, "the login created a session"
    for row in sessions:
        row.password_confirmed_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        row.mfa_verified_at = None
    await db.commit()

    stale = await client.post("/api/domains", json={"domain": "stale.example"}, headers=headers)
    assert stale.status_code == 428, stale.text
    assert stale.json()["detail"]["code"] == "reauth_required"

    step_up = await client.post(
        "/api/identity/reauth", json={"password": TEST_PASSWORD}, headers=headers
    )
    assert step_up.status_code in (200, 201), step_up.text

    allowed = await client.post("/api/domains", json={"domain": "stale.example"}, headers=headers)
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["domain"] == "stale.example"


async def test_a_verified_login_ends_with_an_mfa_verified_session(client, db, owner_a):
    """The second-factor path from a standing start: the tenant turns the
    requirement on, the account enrols a real factor, and the login that follows
    lands on a session the rest of the product accepts as MFA-verified."""
    await require_mfa(client, owner_a)

    clock = TOTPClock(settings.mfa_totp_period_seconds)
    secret = await enroll_totp(db, owner_a, clock)
    headers = await mfa_auth_headers(client, owner_a, secret, clock)

    me = await client.get("/auth/me", headers=headers)
    assert me.status_code == 200, me.text

    live = await identity_sessions.live_sessions(db, user_id=owner_a.id)
    assert live, "the login created a session"
    assert any(session.mfa_verified for session in live), "the session records the second factor"

    # A step-up challenge is available to that session, and is bound to it.
    challenge = await client.post("/api/mfa/challenge", json={}, headers=headers)
    assert challenge.status_code in (200, 201), challenge.text
    assert challenge.json()["purpose"] == "reauth"
    assert challenge.json()["challenge_token"]


async def test_an_unverified_domain_never_refuses_a_login(client, db, tenant_a, owner_a):
    """The guard against a typo locking out a workforce: a claim nobody has
    proved has no effect, whatever the row says."""
    issued = await domain_service.add_domain(
        db, tenant_id=tenant_a.id, actor=owner_a, domain="halfdone.example", commit=True
    )
    domain = await domain_service.get_domain(db, tenant_id=tenant_a.id, domain_id=issued.verification.domain_id)
    assert domain is not None

    headers = await auth_headers(client, owner_a)
    refused = await client.patch(
        f"/api/domains/{domain.id}", json={"enforcement": "require_sso"}, headers=headers
    )
    assert refused.status_code == 400, refused.text

    # Even with the columns forced, an unverified domain imposes nothing.
    domain.enforcement = "require_sso"
    domain.block_password_login = True
    await db.commit()

    evaluation = await policies.load_domain_evaluation(db, "anyone@halfdone.example")
    assert evaluation.known is True
    assert evaluation.verified is False
    assert evaluation.requires_sso is False
    assert evaluation.blocks_password_login is False

    employee = await make_user(
        db, tenant_a, UserRole.AGENT, email="anyone@halfdone.example"
    )
    assert (await login(client, employee.email, TEST_PASSWORD)).status_code == 200
