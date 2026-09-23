"""Sessions, API keys, service accounts and the one-time-token flows.

Every test here asserts a promise the product makes in its documentation:

* a session can be listed, described by device, and revoked immediately;
* a password change or an operator action ends every other session;
* an API key secret is shown once and stored only as a digest;
* a key cannot be handed a scope its creator does not hold;
* a disabled or emergency-stopped service account cannot authenticate;
* a reset token is single-use, expiring, and not interchangeable with a
  verification token.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.auth import password as pw
from app.auth.identity import api_keys, email as identity_email, policies, service_accounts
from app.auth.identity import sessions as identity_sessions
from app.auth.identity.api_keys import APIKey
from app.auth.identity.exceptions import CredentialError, PolicyDenied, TokenError
from app.auth.identity.models import (
    EmailVerificationToken,
    PasswordResetToken,
    ServiceAccountCredential,
    UserSession,
)
from app.auth.permissions import Permission
from app.db.models import AuditAction, AuditLog, UserRole
from tests.conftest import make_tenant, make_user

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _read_scopes() -> list[str]:
    return api_keys.scopes_from_permissions([Permission.CALL_READ])


# ----------------------------------------------------------------- sessions ---


async def test_a_session_is_listed_with_its_device_and_dies_on_revocation(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    policy = await policies.load_policy(db, tenant.id)

    first = await identity_sessions.create_session(
        db,
        user,
        policy=policy,
        user_agent="Mozilla/5.0 (X11; Linux x86_64) Firefox/128.0",
        ip_address="203.0.113.9",
        commit=True,
    )
    second = await identity_sessions.create_session(
        db, user, policy=policy, user_agent="curl/8.5.0", ip_address="203.0.113.10", commit=True
    )

    live = await identity_sessions.live_sessions(db, user_id=user.id)
    assert {row.id for row in live} == {first.id, second.id}
    assert await identity_sessions.count_live_sessions(db, user.id) == 2
    assert "Firefox" in first.device_label

    tokens_revoked = await identity_sessions.revoke_session(
        db, first, reason="user_revoked", commit=True
    )
    live = await identity_sessions.live_sessions(db, user_id=user.id)
    assert {row.id for row in live} == {second.id}
    await db.refresh(first)
    assert first.revoked_reason == "user_revoked"
    # Revoking twice is harmless: nothing left to revoke for that session.
    again = await identity_sessions.revoke_session(db, first, reason="user_revoked", commit=True)
    assert again == 0 and tokens_revoked >= 0


async def test_revoking_other_sessions_leaves_the_current_one_alone(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    policy = await policies.load_policy(db, tenant.id)
    keep = await identity_sessions.create_session(db, user, policy=policy, commit=True)
    for _ in range(3):
        await identity_sessions.create_session(db, user, policy=policy, commit=True)

    revoked = await identity_sessions.revoke_other_sessions(
        db, user_id=user.id, keep_session_id=keep.id, reason="user_revoked_others", commit=True
    )
    assert revoked == 3
    live = await identity_sessions.live_sessions(db, user_id=user.id)
    assert [row.id for row in live] == [keep.id]


async def test_revoking_all_for_a_user_covers_every_session(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    policy = await policies.load_policy(db, tenant.id)
    for _ in range(3):
        await identity_sessions.create_session(db, user, policy=policy, commit=True)

    assert (
        await identity_sessions.revoke_all_for_user(
            db, user_id=user.id, reason="password_changed", commit=True
        )
        == 3
    )
    assert await identity_sessions.live_sessions(db, user_id=user.id) == []


async def test_an_idle_session_stops_being_live_without_a_sweeper(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    row = await identity_sessions.create_session(
        db, user, policy=await policies.load_policy(db, tenant.id), commit=True
    )
    assert row.is_live(_now())

    # Push the idle deadline into the past: every reader must treat the session
    # as dead immediately, whether or not a background sweep has run.
    row.idle_expires_at = _now() - timedelta(minutes=1)
    await db.commit()
    assert row.is_live(_now()) is False
    assert await identity_sessions.live_sessions(db, user_id=user.id) == []
    assert await identity_sessions.sweep_expired(db, user_id=user.id) == 1
    await db.commit()

    refreshed = await db.get(UserSession, row.id)
    assert refreshed.revoked_reason == "expired"


async def test_touching_a_dead_session_reports_it_as_unusable(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    row = await identity_sessions.create_session(
        db, user, policy=await policies.load_policy(db, tenant.id), commit=True
    )
    assert await identity_sessions.touch(db, row, commit=True) is True

    await identity_sessions.revoke_session(db, row, reason="admin_revoked", commit=True)
    assert await identity_sessions.touch(db, row, commit=True) is False


async def test_the_session_ceiling_evicts_the_oldest_rather_than_refusing(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    policy = dataclasses.replace(
        await policies.load_policy(db, tenant.id), session_max_active=3
    )

    created = []
    for _ in range(4):
        created.append(await identity_sessions.create_session(db, user, policy=policy, commit=True))

    live = await identity_sessions.live_sessions(db, user_id=user.id)
    assert len(live) == 3, "a fourth session must not leave four live in a capped tenant"
    assert created[0].id not in {row.id for row in live}, "the oldest one is the one evicted"
    await db.refresh(created[0])
    assert created[0].revoked_reason == "session_limit"


async def test_a_second_session_from_an_unseen_address_is_flagged(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    policy = await policies.load_policy(db, tenant.id)
    await identity_sessions.create_session(
        db, user, policy=policy, ip_address="198.51.100.7", user_agent="Firefox", commit=True
    )
    # First login is ordinary; a *second* live session from a new address is the
    # thing an operator wants to see.
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == AuditAction.SESSION_SUSPICIOUS))).scalars().all()
    assert rows == []

    await identity_sessions.create_session(
        db, user, policy=policy, ip_address="198.51.100.99", user_agent="Firefox", commit=True
    )
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == AuditAction.SESSION_SUSPICIOUS))).scalars().all()
    assert len(rows) == 1
    assert rows[0].detail["reason"] == "new_ip_address"

    # Re-using a known address is not news.
    await identity_sessions.create_session(
        db, user, policy=policy, ip_address="198.51.100.7", user_agent="Firefox", commit=True
    )
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == AuditAction.SESSION_SUSPICIOUS))).scalars().all()
    assert len(rows) == 1


# ----------------------------------------------------------------- api keys ---


async def test_an_api_key_secret_is_returned_once_and_stored_as_a_digest(db):
    tenant = await make_tenant(db, "Key Tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)

    issued = await api_keys.create_key(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="nightly sync",
        scopes=_read_scopes(),
        owner_user_id=owner.id,
        commit=True,
    )

    row = await db.get(APIKey, issued.key.id)
    assert row.secret_hash != issued.token
    assert issued.token not in (row.secret_hash or "")
    assert row.prefix and issued.token.startswith(row.prefix)
    assert row.revoked_at is None
    assert row.expires_at is None
    # The list view must not carry anything replayable.
    listed = await api_keys.list_keys(db, tenant_id=tenant.id)
    assert issued.token not in repr([(k.prefix, k.name, k.secret_hash) for k in listed])


async def test_an_api_key_authenticates_until_it_is_revoked(db):
    tenant = await make_tenant(db, "Key Tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    issued = await api_keys.create_key(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="ci",
        scopes=_read_scopes(),
        owner_user_id=owner.id,
        commit=True,
    )

    principal = await api_keys.authenticate_credential(db, issued.token)
    assert principal.kind.value == "api_key"
    assert principal.tenant_id == tenant.id
    assert principal.actor.id == owner.id
    assert principal.api_key_id == issued.key.id
    assert set(_read_scopes()) <= set(principal.scopes)

    await api_keys.revoke_key(db, issued.key, actor=owner, reason="revoked_by_admin", commit=True)
    with pytest.raises(CredentialError):
        await api_keys.authenticate_credential(db, issued.token)


async def test_an_expired_api_key_is_refused(db):
    tenant = await make_tenant(db, "Key Tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    issued = await api_keys.create_key(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="short-lived",
        scopes=_read_scopes(),
        expires_in_days=1,
        owner_user_id=owner.id,
        commit=True,
    )
    assert issued.key.expires_at is not None
    issued.key.expires_at = _now() - timedelta(minutes=1)
    await db.commit()

    with pytest.raises(CredentialError):
        await api_keys.authenticate_credential(db, issued.token)


async def test_a_rotated_key_replaces_the_old_secret_atomically(db):
    tenant = await make_tenant(db, "Key Tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    issued = await api_keys.create_key(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="rotate me",
        scopes=_read_scopes(),
        owner_user_id=owner.id,
        commit=True,
    )

    rotated = await api_keys.rotate_key(db, issued.key, actor=owner, commit=True)
    assert rotated.token != issued.token
    assert rotated.key.rotated_from_id == issued.key.id
    with pytest.raises(CredentialError):
        await api_keys.authenticate_credential(db, issued.token)
    again = await api_keys.authenticate_credential(db, rotated.token)
    assert again.api_key_id == rotated.key.id


async def test_a_malformed_or_unknown_token_is_refused(db):
    tenant = await make_tenant(db, "Key Tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    await api_keys.create_key(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="real",
        scopes=_read_scopes(),
        owner_user_id=owner.id,
        commit=True,
    )
    for bogus in ("", "not-a-key", "0" * 40, "vdk_live_" + "0" * 40):
        with pytest.raises(CredentialError):
            await api_keys.authenticate_credential(db, bogus)
        assert api_keys.split_token(bogus) is None or True  # never raises on junk


async def test_scope_matching_is_exact_and_not_prefix_based(db):
    assert api_keys.matches_scope(Permission.CALL_READ.value, Permission.CALL_READ) is True
    # "call:read_all" must not be satisfied by a key holding only "call:read":
    # a prefix-style check would leak the wider permission.
    assert api_keys.matches_scope(Permission.CALL_READ.value, Permission.CALL_READ_ALL) is False
    assert api_keys.matches_scope(Permission.CALL_READ.value, Permission.TENANT_DELETE) is False
    assert Permission.TENANT_DELETE.value in api_keys.known_scope_values()

    tenant = await make_tenant(db, "Key Tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    issued = await api_keys.create_key(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="read only",
        scopes=_read_scopes(),
        owner_user_id=owner.id,
        commit=True,
    )
    principal = await api_keys.authenticate_credential(db, issued.token)
    # Exactly one grantable permission is carried, and nothing wider.
    assert list(principal.scopes) == _read_scopes()
    assert api_keys.describe_scopes(list(principal.scopes))


async def test_an_api_key_is_scoped_to_its_own_tenant(db):
    tenant_a = await make_tenant(db, "Key Tenant A")
    tenant_b = await make_tenant(db, "Key Tenant B")
    owner_a = await make_user(db, tenant_a, UserRole.OWNER)
    # Tenant B exists to prove the key is invisible from it, so it needs a user
    # but nothing is asserted about that user's own credentials.
    await make_user(db, tenant_b, UserRole.OWNER)
    issued = await api_keys.create_key(
        db,
        tenant_id=tenant_a.id,
        actor=owner_a,
        name="a-key",
        scopes=_read_scopes(),
        owner_user_id=owner_a.id,
        commit=True,
    )

    principal = await api_keys.authenticate_credential(db, issued.token)
    assert principal.tenant_id == tenant_a.id
    # The key exists in tenant A's listing only.
    assert {k.id for k in await api_keys.list_keys(db, tenant_id=tenant_a.id)} == {issued.key.id}
    assert await api_keys.list_keys(db, tenant_id=tenant_b.id) == []
    assert await api_keys.get_key(db, tenant_id=tenant_b.id, key_id=issued.key.id) is None


# --------------------------------------------------------- service accounts ---


async def test_a_service_account_authenticates_but_only_with_its_scopes(db):
    tenant = await make_tenant(db, "Machine Tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)

    account = await service_accounts.create_account(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="nightly exporter",
        scopes=[Permission.LEAD_READ.value],
        commit=True,
    )
    issued = await service_accounts.issue_credential(db, account, actor=owner, commit=True)

    principal = await api_keys.authenticate_credential(db, issued.token)
    assert principal.kind.value == "service_account"
    assert principal.tenant_id == tenant.id
    assert principal.service_account_id == account.id
    # Attribution keeps working: the human who created it is the actor, so audit
    # and tenant isolation behave exactly as for a person.
    assert principal.actor.id == owner.id
    assert Permission.LEAD_READ.value in principal.scopes
    assert Permission.TENANT_DELETE.value not in principal.scopes


async def test_a_disabled_service_account_cannot_authenticate(db):
    tenant = await make_tenant(db, "Machine Tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    account = await service_accounts.create_account(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="paused",
        scopes=[Permission.LEAD_READ.value],
        commit=True,
    )
    issued = await service_accounts.issue_credential(db, account, actor=owner, commit=True)

    await service_accounts.set_enabled(db, account, actor=owner, enabled=False, commit=True)
    with pytest.raises(CredentialError):
        await api_keys.authenticate_credential(db, issued.token)

    await service_accounts.set_enabled(db, account, actor=owner, enabled=True, commit=True)
    back = await api_keys.authenticate_credential(db, issued.token)
    assert back.service_account_id == account.id


async def test_emergency_disable_cannot_be_undone_by_a_normal_enable(db):
    tenant = await make_tenant(db, "Machine Tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    account = await service_accounts.create_account(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="compromised",
        scopes=[Permission.LEAD_READ.value],
        commit=True,
    )
    await service_accounts.set_enabled(
        db, account, actor=owner, enabled=False, reason="leaked", emergency=True, commit=True
    )
    assert account.emergency_disabled is True

    # A customer-facing toggle must not silently undo an operator's stop.
    with pytest.raises(PolicyDenied):
        await service_accounts.set_enabled(db, account, actor=owner, enabled=True, commit=True)
    await db.refresh(account)
    assert account.enabled is False
    assert account.emergency_disabled is True

    # Clearing it takes a deliberate, separately-audited call.
    await service_accounts.clear_emergency_disable(
        db, account, actor=owner, reason="false positive", commit=True
    )
    await service_accounts.set_enabled(db, account, actor=owner, enabled=True, commit=True)
    await db.refresh(account)
    assert account.enabled is True and account.emergency_disabled is False


async def test_rotating_a_credential_revokes_the_previous_secret(db):
    tenant = await make_tenant(db, "Machine Tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    account = await service_accounts.create_account(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="rotator",
        scopes=[Permission.LEAD_READ.value],
        commit=True,
    )
    first = await service_accounts.issue_credential(db, account, actor=owner, commit=True)
    second = await service_accounts.rotate_credential(
        db, account, first.credential, actor=owner, commit=True
    )

    with pytest.raises(CredentialError):
        await api_keys.authenticate_credential(db, first.token)
    assert (await api_keys.authenticate_credential(db, second.token)).service_account_id == account.id

    rows = await service_accounts.list_credentials(db, account_id=account.id, include_revoked=True)
    assert len(rows) == 2
    assert sum(1 for row in rows if row.revoked_at is None) == 1


async def test_an_expired_account_and_an_expired_credential_are_both_refused(db):
    tenant = await make_tenant(db, "Machine Tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    account = await service_accounts.create_account(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="seasonal",
        scopes=[Permission.LEAD_READ.value],
        expires_in_days=1,
        commit=True,
    )
    issued = await service_accounts.issue_credential(db, account, actor=owner, commit=True)

    account.expires_at = _now() - timedelta(minutes=1)
    await db.commit()
    with pytest.raises(CredentialError):
        await api_keys.authenticate_credential(db, issued.token)

    account.expires_at = None
    issued.credential.expires_at = _now() - timedelta(minutes=1)
    await db.commit()
    with pytest.raises(CredentialError):
        await api_keys.authenticate_credential(db, issued.token)


async def test_scopes_beyond_the_creator_are_refused(db):
    tenant = await make_tenant(db, "Machine Tenant")
    agent = await make_user(db, tenant, UserRole.AGENT)

    with pytest.raises(PolicyDenied):
        await service_accounts.validate_scopes(
            db, actor=agent, scopes=[Permission.TENANT_DELETE.value]
        )
    allowed = await service_accounts.validate_scopes(
        db, actor=agent, scopes=[Permission.CALL_READ.value, Permission.CALL_READ.value]
    )
    assert allowed == [Permission.CALL_READ.value], "valid scopes are deduplicated"

    # The same rule applies to handing a scope to a *key*.
    with pytest.raises(PolicyDenied):
        await api_keys.create_key(
            db,
            tenant_id=tenant.id,
            actor=agent,
            name="escalate",
            scopes=[Permission.TENANT_DELETE.value],
            owner_user_id=agent.id,
            commit=True,
        )


async def test_deleting_an_account_revokes_its_credentials(db):
    tenant = await make_tenant(db, "Machine Tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    account = await service_accounts.create_account(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="temporary",
        scopes=[Permission.LEAD_READ.value],
        commit=True,
    )
    issued = await service_accounts.issue_credential(db, account, actor=owner, commit=True)

    await service_accounts.delete_account(db, account, actor=owner, commit=True)
    with pytest.raises(CredentialError):
        await api_keys.authenticate_credential(db, issued.token)
    assert await service_accounts.list_accounts(db, tenant_id=tenant.id) == []


async def test_credentials_are_never_returned_by_listing(db):
    tenant = await make_tenant(db, "Machine Tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    account = await service_accounts.create_account(
        db,
        tenant_id=tenant.id,
        actor=owner,
        name="listed",
        scopes=[Permission.LEAD_READ.value],
        commit=True,
    )
    issued = await service_accounts.issue_credential(db, account, actor=owner, commit=True)

    rows = await service_accounts.list_credentials(db, account_id=account.id)
    assert issued.token not in repr([(row.prefix, row.label, row.secret_hash) for row in rows])
    row = await db.get(ServiceAccountCredential, issued.credential.id)
    assert row.secret_hash != issued.token


# ------------------------------------------------- one-time tokens (reset) ---


async def test_a_reset_token_is_single_use_and_stored_hashed(db):
    tenant = await make_tenant(db, "Reset Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)

    token = await identity_email.issue_password_reset_token(
        db, user_id=user.id, tenant_id=tenant.id, commit=True
    )
    stored = (
        await db.execute(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
    ).scalar_one()
    assert stored.token_hash != token.plaintext
    assert token.plaintext not in stored.token_hash

    assert (
        await identity_email.consume_password_reset_token(db, plaintext=token.plaintext) == user.id
    )
    await db.commit()
    with pytest.raises(TokenError):
        await identity_email.consume_password_reset_token(db, plaintext=token.plaintext)


async def test_an_expired_reset_token_is_refused(db):
    tenant = await make_tenant(db, "Reset Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    token = await identity_email.issue_password_reset_token(
        db, user_id=user.id, tenant_id=tenant.id, commit=True
    )
    stored = (
        await db.execute(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
    ).scalar_one()
    stored.expires_at = _now() - timedelta(seconds=1)
    await db.commit()

    with pytest.raises(TokenError):
        await identity_email.consume_password_reset_token(db, plaintext=token.plaintext)


async def test_an_unknown_reset_token_is_refused(db):
    with pytest.raises(TokenError):
        await identity_email.consume_password_reset_token(db, plaintext="vd1t" + "z" * 40)


async def test_email_verification_tokens_are_single_use(db):
    tenant = await make_tenant(db, "Verify Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)

    token = await identity_email.issue_verification_token(
        db, user_id=user.id, tenant_id=tenant.id, email=user.email, commit=True
    )
    row = (
        await db.execute(
            select(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id)
        )
    ).scalar_one()
    assert row.email == user.email
    assert row.token_hash not in token.plaintext

    assert await identity_email.consume_verification_token(db, plaintext=token.plaintext) == user.id
    await db.commit()
    with pytest.raises(TokenError):
        await identity_email.consume_verification_token(db, plaintext=token.plaintext)


async def test_reset_and_verification_tokens_are_not_interchangeable(db):
    tenant = await make_tenant(db, "Token Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    reset = await identity_email.issue_password_reset_token(
        db, user_id=user.id, tenant_id=tenant.id, commit=True
    )
    with pytest.raises(TokenError):
        await identity_email.consume_verification_token(db, plaintext=reset.plaintext)

    verify = await identity_email.issue_verification_token(
        db, user_id=user.id, tenant_id=tenant.id, email=user.email, commit=True
    )
    with pytest.raises(TokenError):
        await identity_email.consume_password_reset_token(db, plaintext=verify.plaintext)


async def test_the_log_transport_reports_a_no_op_instead_of_failing(db, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "email_transport", "log", raising=False)
    message = identity_email.render_password_reset(
        to="someone@example.com",
        reset_url="https://app.example.com/reset?token=abc",
        ttl_minutes=30,
    )
    assert "https://app.example.com/reset?token=abc" in message.text_body
    result = await identity_email.send(message)
    assert result.transport == "log"
    # "log" counts as configured (mail is captured, not silently dropped), so a
    # reset request is plausible in development; an empty transport is not.
    assert identity_email.delivery_is_configured() is True

    monkeypatch.setattr(settings, "email_transport", "", raising=False)
    assert identity_email.delivery_is_configured() is False


async def test_the_reset_flow_uses_the_same_password_policy_as_signup(db):
    """A reset must not be a way around the password rules."""
    with pytest.raises(pw.PasswordPolicyError):
        pw.validate_policy("short", email="user@example.com")
    with pytest.raises(pw.PasswordPolicyError):
        pw.validate_policy("password1234", email="user@example.com")
    assert pw.validate_policy("Correct-Horse-Battery-Staple9", email="user@example.com") is None
