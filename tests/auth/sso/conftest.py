"""Fixtures shared by the federated-login tests.

The provider is expensive to build (a 2048-bit key pair and a self-signed
certificate), so it is module-scoped; the caches inside the OIDC module are
process-global, so they are reset around every test — a discovery document
cached by one test would otherwise answer for the next one and hide a
regression in the fetch path.
"""
from __future__ import annotations

import pytest

from app.auth.identity.sso import oidc
from tests.auth.sso.providers import StubOidcTransport, default_documents


@pytest.fixture(autouse=True)
def _provider_answers_by_default(idp, monkeypatch):
    """Give the provider a transport by default.

    The caches are process-global, so they are also reset around every test — a
    discovery document cached by one test would otherwise answer for the next
    one and hide a regression in the fetch path.

    Tests that need different documents (a rotated key set, a slower endpoint)
    install their own transport inside the test body, which simply replaces this
    one.
    """
    oidc.reset_caches()
    StubOidcTransport(**default_documents(idp)).install(monkeypatch)
    yield
    oidc.reset_caches()


@pytest.fixture
def oidc_provider(idp, monkeypatch) -> StubOidcTransport:
    """The provider's HTTP surface, with a handle for scripting it mid-test.

    A test that needs a rotated key set or a token for a specific attempt uses
    this and mutates ``documents``; the default installation above is replaced,
    not duplicated.
    """
    return StubOidcTransport(**default_documents(idp)).install(monkeypatch)
