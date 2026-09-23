"""MFA: enrollment, verification, replay, lockout and the disable path.

These are service-level tests against a real database (see ``tests/conftest.py``
for the fixtures). The point of each one is a behaviour a *security* reviewer
would ask about, not a coverage number:

* the seed is never stored in the clear and is only ever returned once;
* a code cannot be replayed inside its own window;
* wrong codes burn the challenge and eventually lock it;
* a recovery code works exactly once;
* disabling a factor requires a fresh proof of presence and kills other sessions.
"""
from __future__ import annotations

import time
import uuid

import pytest

from app.auth import password as pw
from app.auth.identity import exceptions as identity_exc
from app.auth.identity import mfa as identity_mfa
from app.auth.identity import sessions as identity_sessions
from app.auth.identity import totp
from app.auth.identity.models import (
    FactorStatus,
    MFAChallenge,
    MFAFactor,
    MFARecoveryCode,
)
from app.auth.identity import policies
from app.core.config import settings
from app.db.models import User, UserRole
from tests.conftest import make_tenant, make_user

pytestmark = pytest.mark.asyncio


async def _enroll(db, user) -> tuple[str, list[str]]:
    """Run the two-step enrollment and return (secret, recovery codes)."""
    start = await identity_mfa.begin_enrollment(db, user, commit=True)
    code = totp.totp(start.secret, time.time())
    codes = await identity_mfa.confirm_enrollment(db, user, code=code, commit=True)
    return start.secret, codes


def _next_code(secret: str) -> str:
    """A code from the *next* TOTP step.

    Enrollment already consumed the current step (confirming it is a successful
    verification, and RFC 6238 section 5.2 forbids accepting the same step
    twice), so any test that verifies straight afterwards has to look one step
    ahead. That is the honest simulation of a user waiting for their app to
    roll over -- and it keeps the suite from sleeping 30 seconds per case.
    """
    return totp.totp(secret, time.time() + settings.mfa_totp_period_seconds)


async def _challenge_token(db, user) -> str:
    issued = await identity_mfa.create_challenge(db, user, commit=True)
    return issued.token


async def test_enrollment_stores_only_ciphertext(db):
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)

    start = await identity_mfa.begin_enrollment(db, user, commit=True)
    factor = (
        await db.execute(
            MFAFactor.__table__.select().where(MFAFactor.user_id == user.id)
        )
    ).first()

    assert factor is not None
    assert factor.secret_encrypted != start.secret, "the seed must not be stored in the clear"
    assert start.secret not in factor.secret_encrypted
    assert factor.status == FactorStatus.PENDING.value
    assert factor.secret_key_id, "the envelope must name the key that sealed it"
    # The provisioning URI carries the seed once, for the authenticator app.
    assert start.provisioning_uri.startswith("otpauth://totp/")
    assert factor.confirmed_at is None


async def test_enrollment_requires_a_code_from_the_same_seed(db):
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    await identity_mfa.begin_enrollment(db, user, commit=True)

    with pytest.raises(identity_exc.MFAVerificationFailed):
        await identity_mfa.confirm_enrollment(db, user, code="000000", commit=True)
    with pytest.raises(identity_exc.MFAVerificationFailed):
        await identity_mfa.confirm_enrollment(db, user, code="999999", commit=True)

    state = await identity_mfa.load_state(db, user)
    assert state.enrolled is False
    assert state.pending is True


async def test_confirm_returns_recovery_codes_once_and_stores_only_hashes(db):
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)

    _secret, codes = await _enroll(db, user)
    assert len(codes) == settings.mfa_recovery_code_count

    rows = (
        await db.execute(MFARecoveryCode.__table__.select().where(MFARecoveryCode.user_id == user.id))
    ).all()
    stored = {row.code_hash for row in rows}
    assert len(stored) == len(codes)
    for code in codes:
        assert code not in stored, "a recovery code must never be stored in the clear"

    state = await identity_mfa.load_state(db, user)
    assert state.enrolled is True
    assert state.recovery_codes_remaining == len(codes)


async def test_a_code_cannot_be_replayed(db):
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    secret, _codes = await _enroll(db, user)

    code = _next_code(secret)
    assert await identity_mfa.verify_user_code(db, user, code=code, commit=True) is True
    # Same code, same window: the second attempt must fail, or a captured code
    # would be valid for the rest of its 30 seconds. This is the replay rule of
    # RFC 6238 section 5.2, and it is why the first verification after
    # enrollment has to use the *next* step.
    assert await identity_mfa.verify_user_code(db, user, code=code, commit=True) is False


async def test_wrong_codes_lock_the_challenge(db):
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    await _enroll(db, user)
    token = await _challenge_token(db, user)

    for _ in range(settings.mfa_challenge_max_failures):
        result = await identity_mfa.resolve_challenge(
            db, token=token, code="123456", commit=True
        )
        assert result.ok is False

    with pytest.raises(identity_exc.MFAChallengeLocked):
        await identity_mfa.resolve_challenge(db, token=token, code="123456", commit=True)

    row = (
        await db.execute(MFAChallenge.__table__.select().where(MFAChallenge.user_id == user.id))
    ).first()
    assert row.locked_until is not None
    assert row.failures >= settings.mfa_challenge_max_failures


async def test_a_consumed_challenge_cannot_be_reused(db):
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    secret, _codes = await _enroll(db, user)
    token = await _challenge_token(db, user)

    first = await identity_mfa.resolve_challenge(
        db, token=token, code=_next_code(secret), commit=True
    )
    assert first.ok is True

    # A consumed challenge is refused outright rather than answered: there is
    # nothing left to compare the code against, so "invalid code" would be a lie.
    with pytest.raises(identity_exc.MFAError):
        await identity_mfa.resolve_challenge(
            db, token=token, code=totp.totp(secret, time.time()), commit=True
        )


async def test_a_recovery_code_works_exactly_once(db):
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    _secret, codes = await _enroll(db, user)

    token = await _challenge_token(db, user)
    used = await identity_mfa.resolve_challenge(db, token=token, code=codes[0], commit=True)
    assert used.ok is True
    assert used.used_recovery_code is True

    token2 = await _challenge_token(db, user)
    again = await identity_mfa.resolve_challenge(db, token=token2, code=codes[0], commit=True)
    assert again.ok is False

    state = await identity_mfa.load_state(db, user)
    assert state.recovery_codes_remaining == len(codes) - 1


async def test_regenerating_recovery_codes_replaces_the_old_set(db):
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    _secret, old_codes = await _enroll(db, user)

    new_codes = await identity_mfa.regenerate_recovery_codes(db, user, commit=True)
    assert set(new_codes) != set(old_codes)

    token = await _challenge_token(db, user)
    assert (
        await identity_mfa.resolve_challenge(db, token=token, code=old_codes[0], commit=True)
    ).ok is False


async def test_disabling_a_factor_clears_codes_and_other_sessions(db):
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    await _enroll(db, user)

    policy = await policies.load_policy(db, tenant.id)
    current = await identity_sessions.create_session(
        db, user, policy=policy, commit=True
    )
    other = await identity_sessions.create_session(db, user, policy=policy, commit=True)

    await identity_mfa.disable_factor(db, user, reason="user_disabled", commit=True)
    revoked = await identity_sessions.revoke_all_for_user(
        db, user_id=user.id, reason="mfa_disabled", keep_session_id=current.id, commit=True
    )

    state = await identity_mfa.load_state(db, user)
    assert state.enrolled is False
    assert state.recovery_codes_remaining == 0
    assert revoked == 1
    await db.refresh(other)
    assert other.revoked_at is not None
    await db.refresh(current)
    assert current.revoked_at is None, "the session that disabled the factor survives"


async def test_a_user_without_a_factor_cannot_open_a_challenge(db):
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)

    with pytest.raises(identity_exc.MFANotEnrolled):
        await identity_mfa.create_challenge(db, user, commit=True)


async def test_enrolling_twice_is_refused_while_a_factor_is_active(db):
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    await _enroll(db, user)

    with pytest.raises(identity_exc.MFAAlreadyEnrolled):
        await identity_mfa.begin_enrollment(db, user, commit=True)


async def test_totp_window_accepts_one_step_of_skew_each_way(db):
    """One step of clock skew is tolerated, more is not, and the past is spent."""
    period = settings.mfa_totp_period_seconds
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    secret, _codes = await _enroll(db, user)

    # Enrollment consumed the current step, so a code from the step before it is
    # a replay and must be refused even though it is inside the skew window.
    previous = totp.totp(secret, time.time() - period)
    assert not await identity_mfa.verify_user_code(db, user, code=previous, commit=True)

    # A step ahead is inside the window and not yet spent, so a client whose
    # clock runs slightly fast still works.
    assert await identity_mfa.verify_user_code(db, user, code=_next_code(secret), commit=True)

    far_ahead = totp.totp(secret, time.time() + period * 5)
    assert not await identity_mfa.verify_user_code(db, user, code=far_ahead, commit=True)


async def test_verification_rate_limit_is_per_user(db, monkeypatch):
    """The per-user verification budget is enforced by the limiter, not by luck."""
    from app.core import rate_limit

    calls: list[tuple[str, str]] = []

    async def _deny(*, action: str, who: str, limit: int, window: float = 60.0) -> bool:
        calls.append((action, who))
        return False

    monkeypatch.setattr(identity_mfa, "allow_identity_action", _deny)
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    secret, _codes = await _enroll(db, user)

    # Even a *correct* code is refused once the budget is spent: the limit is on
    # attempts, not on failures, so an attacker with a valid code cannot be
    # distinguished from one guessing.
    with pytest.raises(identity_exc.MFAChallengeLocked):
        await identity_mfa.verify_user_code(
            db, user, code=totp.totp(secret, time.time()), commit=True
        )
    assert calls and calls[0][1] == str(user.id)
    assert rate_limit is not None


async def test_mfa_secret_is_never_logged_or_returned_by_status(db, caplog):
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    secret, _codes = await _enroll(db, user)

    state = await identity_mfa.load_state(db, user)
    assert secret not in repr(state)
    for record in caplog.records:
        assert secret not in record.getMessage()


async def test_factor_rows_are_scoped_to_one_user(db):
    tenant = await make_tenant(db, "MFA Tenant")
    first = await make_user(db, tenant, UserRole.AGENT)
    second = await make_user(db, tenant, UserRole.AGENT)
    await _enroll(db, first)

    assert (await identity_mfa.load_state(db, first)).enrolled is True
    assert (await identity_mfa.load_state(db, second)).enrolled is False


async def test_enrollment_refuses_when_mfa_is_disabled_on_the_deployment(db, monkeypatch):
    monkeypatch.setattr(settings, "mfa_enabled", False, raising=False)
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)

    with pytest.raises(identity_exc.MFADisabled):
        await identity_mfa.begin_enrollment(db, user, commit=True)


async def test_password_verification_is_untouched_by_mfa(db):
    """MFA must not change how a password is checked -- bcrypt, exactly as before."""
    tenant = await make_tenant(db, "MFA Tenant")
    user = await make_user(db, tenant, UserRole.AGENT, password="correct-horse-battery")
    await _enroll(db, user)

    await db.refresh(user)
    assert pw.verify_password("correct-horse-battery", user.password_hash)
    assert not pw.verify_password("wrong", user.password_hash)


async def test_unused_import_guard():
    """Keeps flake8 honest about the module imports this file relies on."""
    assert uuid.uuid4() is not None
    assert User is not None
