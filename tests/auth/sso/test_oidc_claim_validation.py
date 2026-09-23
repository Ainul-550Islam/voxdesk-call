"""What an ID token has to prove before a single claim is believed.

Each test hands ``verify_id_token`` a token that is wrong in exactly one way and
asserts the refusal. The list is the whole point of doing this by hand rather
than trusting a provider to behave:

* signed by an unknown key, or not signed at all;
* minted for another client (``aud``) or for several audiences without a
  matching ``azp``;
* expired, or not yet valid beyond the clock-skew allowance (both directions);
* carrying a different nonce than the attempt that asked for it;
* sealed with a symmetric algorithm, which a public client must never accept.

The nonce is passed as a *hash*, because that is all the attempt row keeps.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.auth.identity import tokens as identity_tokens
from app.auth.identity.exceptions import SSOValidationError
from app.auth.identity.sso import oidc
from tests.auth.sso.providers import (
    CLIENT_ID,
    discovered_provider,
    oidc_connection,
    token_claims,
)

pytestmark = pytest.mark.asyncio

NONCE = "nonce-for-this-attempt"


def nonce_hash(value: str = NONCE) -> str:
    return identity_tokens.hash_token(value)


async def verify(idp, token: str, *, nonce: str = NONCE, **connection_overrides):
    return await oidc.verify_id_token(
        oidc_connection(**connection_overrides),
        discovered_provider(),
        id_token=token,
        nonce_hash=nonce_hash(nonce),
    )


async def test_a_correctly_signed_token_is_accepted(idp):
    token = idp.id_token(token_claims(nonce=NONCE, email="ada@acme.test"))
    claims = await verify(idp, token)
    assert claims["email"] == "ada@acme.test"
    assert claims["sub"] == "user-1"


async def test_a_token_from_another_issuer_is_refused(idp):
    token = idp.id_token(token_claims(iss="https://elsewhere.example.net", nonce=NONCE))
    with pytest.raises(SSOValidationError):
        await verify(idp, token)


async def test_a_token_for_another_client_is_refused(idp):
    token = idp.id_token(token_claims(aud="another-client", nonce=NONCE))
    with pytest.raises(SSOValidationError):
        await verify(idp, token)


async def test_a_multi_audience_token_needs_a_matching_authorized_party(idp):
    token = idp.id_token(token_claims(aud=[CLIENT_ID, "an-integrator"], azp="an-integrator", nonce=NONCE))
    with pytest.raises(SSOValidationError):
        await verify(idp, token)

    proper = idp.id_token(token_claims(aud=[CLIENT_ID, "an-integrator"], azp=CLIENT_ID, nonce=NONCE))
    assert (await verify(idp, proper))["azp"] == CLIENT_ID


async def test_an_expired_token_is_refused(idp):
    past = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).timestamp())
    token = idp.id_token(token_claims(exp=past, nonce=NONCE))
    with pytest.raises(SSOValidationError):
        await verify(idp, token)


async def test_a_token_inside_the_clock_skew_is_accepted_and_outside_it_is_not(idp):
    now = dt.datetime.now(dt.timezone.utc)
    slightly_old = int((now - dt.timedelta(seconds=30)).timestamp())
    much_older = int((now - dt.timedelta(minutes=30)).timestamp())

    accepted = idp.id_token(token_claims(exp=slightly_old, nonce=NONCE))
    assert await verify(idp, accepted)

    refused = idp.id_token(token_claims(exp=much_older, nonce=NONCE))
    with pytest.raises(SSOValidationError):
        await verify(idp, refused)


async def test_a_token_from_the_future_is_refused(idp):
    later = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).timestamp())
    token = idp.id_token(token_claims(iat=later, nbf=later, nonce=NONCE))
    with pytest.raises(SSOValidationError):
        await verify(idp, token)


async def test_a_token_without_a_nonce_is_refused(idp):
    token = idp.id_token(token_claims(email="ada@acme.test"))
    with pytest.raises(SSOValidationError):
        await verify(idp, token)


async def test_a_token_for_another_attempt_is_refused(idp):
    token = idp.id_token(token_claims(nonce="a-different-attempt"))
    with pytest.raises(SSOValidationError):
        await verify(idp, token)


async def test_an_unsigned_token_is_refused(idp):
    unsigned = idp.id_token(token_claims(nonce=NONCE), algorithm="none")
    with pytest.raises(SSOValidationError):
        await verify(idp, unsigned)


async def test_a_token_signed_by_an_unknown_key_is_refused(idp, other_idp):
    token = other_idp.id_token(token_claims(nonce=NONCE))
    with pytest.raises(SSOValidationError):
        await verify(idp, token)


async def test_a_symmetric_token_is_refused(idp):
    token = idp.id_token(token_claims(nonce=NONCE), algorithm="HS256")
    with pytest.raises(SSOValidationError):
        await verify(idp, token)


async def test_a_token_missing_a_required_claim_is_refused(idp):
    """``sub`` is what the account is keyed on; a token without one proves nothing."""
    claims = token_claims(nonce=NONCE)
    claims.pop("sub")
    with pytest.raises(SSOValidationError):
        await verify(idp, idp.id_token(claims))


async def test_a_missing_verified_email_claim_does_not_count_as_verified():
    """Absence is not consent: an unstated ``email_verified`` normalizes to False.

    This is what stops a provider that simply omits the claim from being treated
    as if it had asserted ownership of the address.
    """
    from types import SimpleNamespace

    from app.auth.identity.sso.claims import normalize_claims

    connection = SimpleNamespace(
        email_claim="email",
        name_claim="name",
        subject_claim="sub",
        group_claim="groups",
        role_claim="",
    )
    normalized = normalize_claims(
        connection,
        {"sub": "user-1", "email": "ada@acme.test"},
        default_email_verified=False,
    )
    assert normalized.email == "ada@acme.test"
    assert normalized.email_verified is False
    assert normalized.subject == "user-1"
