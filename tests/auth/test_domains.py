"""Enterprise domains and DNS-TXT verification, driven through the HTTP surface.

A domain claim is a tenant saying "everyone at this address is mine", so the
rules are about what that claim may and may not do:

* adding a domain is a claim, not a fact — nothing is enforced until a TXT
  record proves it, which is what stops a typo locking a workforce out;
* the claim is globally unique, and a conflict does not say who holds it;
* a challenge is invalidated by re-issuing it, so an old record cannot be
  replayed after an operator moves the domain;
* verification distinguishes "the record is wrong" from "DNS is unreachable",
  because the two need different actions from the operator;
* enforcement (require SSO, block password login) is switchable only after
  verification, and every change is audited with the previous value.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.auth.identity import domains as domain_service
from app.auth.identity.models import DomainVerification
from app.db.models import AuditAction, AuditLog, UserRole
from tests.conftest import auth_headers, make_user

pytestmark = pytest.mark.asyncio


class _Answer:
    def __init__(self, records: list[str], *, status: int = 0) -> None:
        self.status_code = 200
        self._records = records
        self._status = status

    def json(self) -> dict:
        return {
            "Status": self._status,
            "Answer": [{"type": 16, "data": f'"{record}"'} for record in self._records],
        }


class _Resolver:
    """A DNS-over-HTTPS resolver that answers from a script."""

    answers: list[str] = []
    status: int = 0
    failure: Exception | None = None
    asked: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    async def __aenter__(self) -> "_Resolver":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def get(self, url, params=None, headers=None):
        del url, headers
        if type(self).failure is not None:
            raise type(self).failure
        type(self).asked.append((params or {}).get("name", ""))
        return _Answer(list(type(self).answers), status=type(self).status)


@pytest.fixture
def resolver(monkeypatch):
    monkeypatch.setattr(domain_service.httpx, "AsyncClient", _Resolver)
    _Resolver.answers = []
    _Resolver.status = 0
    _Resolver.failure = None
    _Resolver.asked = []
    return _Resolver


async def _add(client, headers, domain: str = "acme.example"):
    return await client.post("/api/domains", json={"domain": domain}, headers=headers)


async def _verified(client, headers, resolver, domain: str = "acme.example") -> dict:
    created = await _add(client, headers, domain)
    assert created.status_code == 201, created.text
    body = created.json()
    resolver.answers = [body["verification_record_value"]]
    confirmed = await client.post(f"/api/domains/{body['id']}/verify", headers=headers)
    assert confirmed.status_code == 200 and confirmed.json()["verified"] is True, confirmed.text
    return body


async def test_adding_a_domain_opens_a_challenge_and_normalizes_the_name(client, owner_a):
    headers = await auth_headers(client, owner_a)
    created = await _add(client, headers, "ACME.Example.")
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["domain"] == "acme.example", "the stored claim is normalized"
    assert body["verified"] is False
    assert body["verified_at"] is None
    assert body["enforcement"] == "off"
    assert body["block_password_login"] is False
    assert body["verification_record_value"]
    assert body["verification_record_name"].endswith("acme.example")
    assert body["verification_status"]

    listed = await client.get("/api/domains", headers=headers)
    assert [row["domain"] for row in listed.json()] == ["acme.example"]

    # The value is returned once, at creation; a read does not repeat it.
    again = await client.get(f"/api/domains/{body['id']}", headers=headers)
    assert again.status_code == 200
    assert "verification_record_value" not in again.json()


async def test_a_name_that_cannot_be_a_domain_is_refused(client, owner_a):
    headers = await auth_headers(client, owner_a)
    for candidate in ("not a domain", "127.0.0.1", "a", "", "acme", "-bad-.example"):
        response = await _add(client, headers, candidate)
        assert response.status_code in (400, 422), f"{candidate!r}: {response.text}"

    # A pasted URL is accepted as the host it names, which is what an operator
    # copying out of a browser bar will paste.
    pasted = await _add(client, headers, "https://pasted.example/sso/callback?next=1")
    assert pasted.status_code == 201, pasted.text
    assert pasted.json()["domain"] == "pasted.example"


async def test_a_claimed_domain_is_globally_unique_and_the_conflict_says_nothing(
    client, db, owner_a, owner_b
):
    mine = await auth_headers(client, owner_a)
    theirs = await auth_headers(client, owner_b)

    first = await _add(client, mine, "shared.example")
    assert first.status_code == 201, first.text

    taken = await _add(client, theirs, "shared.example")
    assert taken.status_code in (400, 409), taken.text
    assert taken.json()["detail"]["code"] == "domain_conflict"
    assert str(owner_a.tenant_id) not in taken.text, "the holder is not named"
    assert owner_a.email not in taken.text
    assert "shared.example" not in taken.text, "the conflict does not confirm the claim either"

    # The same workspace asking twice is refused as well.
    again = await _add(client, mine, "shared.example")
    assert again.status_code in (400, 409), again.text


async def test_verification_succeeds_only_for_the_published_value(client, owner_a, resolver):
    headers = await auth_headers(client, owner_a)
    created = await _add(client, headers, "verify.example")
    body = created.json()

    resolver.answers = ["something-else"]
    wrong = await client.post(f"/api/domains/{body['id']}/verify", headers=headers)
    assert wrong.status_code == 200, wrong.text
    assert wrong.json()["verified"] is False
    assert wrong.json()["records_seen"] == 1

    still = await client.get(f"/api/domains/{body['id']}", headers=headers)
    assert still.json()["verified"] is False
    assert still.json()["failed_attempts"] >= 1

    resolver.answers = [body["verification_record_value"]]
    right = await client.post(f"/api/domains/{body['id']}/verify", headers=headers)
    assert right.status_code == 200, right.text
    assert right.json()["verified"] is True

    resolved = await client.get(f"/api/domains/{body['id']}", headers=headers)
    assert resolved.json()["verified"] is True
    assert resolved.json()["verified_at"] is not None


async def test_an_unreachable_resolver_is_not_reported_as_a_wrong_record(client, owner_a, resolver):
    import httpx

    headers = await auth_headers(client, owner_a)
    created = await _add(client, headers, "outage.example")
    body = created.json()

    resolver.failure = httpx.ConnectError("resolver down")
    response = await client.post(f"/api/domains/{body['id']}/verify", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["verified"] is False

    row = await client.get(f"/api/domains/{body['id']}", headers=headers)
    assert row.json()["verification_status"] in ("pending", "unavailable", "resolver_unavailable")
    assert row.json()["verified"] is False


async def test_re_issuing_a_challenge_invalidates_the_previous_value(client, db, owner_a, resolver):
    headers = await auth_headers(client, owner_a)
    created = await _add(client, headers, "rotate.example")
    first = created.json()

    second = await client.post(f"/api/domains/{first['id']}/challenge", headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["verification_record_value"] != first["verification_record_value"]

    # The retired challenge is a terminal state, not a deleted row: what was
    # replaced stays on the record as superseded.
    statuses = sorted(
        row.status
        for row in (
            await db.execute(
                select(DomainVerification).where(DomainVerification.domain_id == uuid.UUID(first["id"]))
            )
        ).scalars().all()
    )
    assert statuses == ["pending", "superseded"]

    resolver.answers = [first["verification_record_value"]]
    stale = await client.post(f"/api/domains/{first['id']}/verify", headers=headers)
    assert stale.status_code == 200
    assert stale.json()["verified"] is False, "the superseded record must not verify"

    resolver.answers = [second.json()["verification_record_value"]]
    fresh = await client.post(f"/api/domains/{first['id']}/verify", headers=headers)
    assert fresh.json()["verified"] is True


async def test_enforcement_needs_a_verified_domain_and_is_audited(client, db, owner_a, tenant_a, resolver):
    headers = await auth_headers(client, owner_a)
    created = await _add(client, headers, "enforce.example")
    body = created.json()

    refused = await client.patch(
        f"/api/domains/{body['id']}", json={"enforcement": "require_sso"}, headers=headers
    )
    assert refused.status_code == 400, refused.text

    resolver.answers = [body["verification_record_value"]]
    assert (
        await client.post(f"/api/domains/{body['id']}/verify", headers=headers)
    ).json()["verified"] is True

    allowed = await client.patch(
        f"/api/domains/{body['id']}",
        json={"enforcement": "require_sso", "block_password_login": True},
        headers=headers,
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["enforcement"] == "require_sso"
    assert allowed.json()["block_password_login"] is True

    bogus = await client.patch(
        f"/api/domains/{body['id']}", json={"enforcement": "whatever"}, headers=headers
    )
    assert bogus.status_code == 400, bogus.text

    changes = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_a.id,
                AuditLog.action == AuditAction.DOMAIN_ENFORCEMENT_CHANGED,
            )
        )
    ).scalars().all()
    assert len(changes) == 1
    assert changes[0].detail["previous"] == "off"
    assert changes[0].detail["enforcement"] == "require_sso"


async def test_a_connection_can_be_attached_only_if_it_is_ours(client, db, owner_a, owner_b, idp, resolver):
    from tests.auth.sso.providers import create_saml_connection

    theirs = await create_saml_connection(client, owner_b, idp, slug="theirs")
    headers = await auth_headers(client, owner_a)
    body = (await _add(client, headers, "attach.example")).json()
    resolver.answers = [body["verification_record_value"]]
    assert (
        await client.post(f"/api/domains/{body['id']}/verify", headers=headers)
    ).json()["verified"] is True

    refused = await client.patch(
        f"/api/domains/{body['id']}",
        json={"enforcement": "require_sso", "sso_connection_id": theirs["id"]},
        headers=headers,
    )
    assert refused.status_code in (400, 404), refused.text

    mine = await create_saml_connection(client, owner_a, idp, slug="mine")
    attached = await client.patch(
        f"/api/domains/{body['id']}",
        json={"enforcement": "require_sso", "sso_connection_id": mine["id"]},
        headers=headers,
    )
    assert attached.status_code == 200, attached.text
    assert attached.json()["sso_connection_id"] == mine["id"]


async def test_removing_a_domain_takes_its_challenge_with_it(client, db, owner_a, tenant_a, resolver):
    headers = await auth_headers(client, owner_a)
    body = (await _add(client, headers, "gone.example")).json()

    removed = await client.delete(f"/api/domains/{body['id']}", headers=headers)
    assert removed.status_code == 204, removed.text
    assert (await client.get(f"/api/domains/{body['id']}", headers=headers)).status_code == 404
    assert (await client.get("/api/domains", headers=headers)).json() == []

    # The name is free again, and the old challenge cannot be used.
    before = len((await db.execute(select(AuditLog))).scalars().all())
    replacement = await _add(client, headers, "gone.example")
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["verification_record_value"] != body["verification_record_value"]
    assert len((await db.execute(select(AuditLog))).scalars().all()) > before

    actions = {
        row.action
        for row in (
            await db.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_a.id))
        ).scalars().all()
    }
    assert AuditAction.DOMAIN_ADDED in actions
    assert AuditAction.DOMAIN_REMOVED in actions


async def test_another_workspace_cannot_touch_the_domain(client, owner_a, owner_b, resolver):
    owner_headers = await auth_headers(client, owner_a)
    body = (await _add(client, owner_headers, "private.example")).json()

    foreign = await auth_headers(client, owner_b)
    probes = [
        ("GET", f"/api/domains/{body['id']}", None),
        ("POST", f"/api/domains/{body['id']}/challenge", None),
        ("POST", f"/api/domains/{body['id']}/verify", None),
        ("PATCH", f"/api/domains/{body['id']}", {"enforcement": "require_sso"}),
        ("DELETE", f"/api/domains/{body['id']}", None),
    ]
    for method, path, payload in probes:
        response = await client.request(method, path, json=payload, headers=foreign)
        assert response.status_code == 404, f"{method} {path}: {response.text}"

    # Untouched, still unverified, still the owner's.
    still = await client.get(f"/api/domains/{body['id']}", headers=owner_headers)
    assert still.json()["verified"] is False
    assert still.json()["enforcement"] == "off"


async def test_a_domain_claim_needs_an_identity_writer(client, db, tenant_a, owner_a):
    agent = await make_user(db, tenant_a, UserRole.AGENT)
    headers = await auth_headers(client, agent)

    assert (await client.get("/api/domains", headers=headers)).status_code == 403
    assert (await _add(client, headers, "nope.example")).status_code == 403

    owner_headers = await auth_headers(client, owner_a)
    body = (await _add(client, owner_headers, "guarded.example")).json()
    assert (
        await client.patch(
            f"/api/domains/{body['id']}", json={"enforcement": "off"}, headers=headers
        )
    ).status_code == 403
    assert (await client.delete(f"/api/domains/{body['id']}", headers=headers)).status_code == 403


async def test_an_unverified_domain_is_inert_in_the_evaluation(db, tenant_a, owner_a):
    """The rule the whole feature rests on: nothing unproven may impose."""
    issued = await domain_service.add_domain(
        db, tenant_id=tenant_a.id, actor=owner_a, domain="inert.example", commit=True
    )
    row = await domain_service.get_domain(
        db, tenant_id=tenant_a.id, domain_id=issued.verification.domain_id
    )
    assert row is not None
    row.enforcement = "require_sso"
    row.block_password_login = True
    await db.commit()

    from app.auth.identity import policies

    evaluation = await policies.load_domain_evaluation(db, "someone@inert.example")
    assert evaluation.known is True
    assert evaluation.verified is False
    assert evaluation.requires_sso is False
    assert evaluation.blocks_password_login is False
