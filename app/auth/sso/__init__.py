"""Federated sign-in: OIDC and SAML 2.0.

The implementation lives in :mod:`app.auth.identity.sso` — one module per
protocol plus a service layer that owns every decision touching a user. This
package maps the names the rest of the codebase imports onto that
implementation, grouped by concern:

===========================  =================================================
Module                       Concern
===========================  =================================================
``base``                     the vocabulary both protocols share
``oidc``                     discovery, authorization code, ID-token checks
``discovery``                the well-known document and its trust rules
``saml``                     AuthnRequest, response parsing, signature checks
``metadata``                 SP metadata and IdP metadata handling
``certificates``             certificate import, rotation and retirement
``claims``                   claim normalization and the role decision
``mapping``                  subject/email/group mapping and JIT rules
``service``                  connections, login attempts and completion
===========================  =================================================

Nothing here re-implements a check. A second implementation of "is this
signature valid?" is a second chance to get it wrong, and the point of the
hand-written verifier in ``saml`` is that it is the *only* thing that decides.
"""
from __future__ import annotations

from app.auth.sso import (
    base,
    certificates,
    claims,
    discovery,
    mapping,
    metadata,
    oidc,
    saml,
    service,
)

__all__ = [
    "base",
    "certificates",
    "claims",
    "discovery",
    "mapping",
    "metadata",
    "oidc",
    "saml",
    "service",
]
