"""Claim normalization and the role decision.

Implementation: :mod:`app.auth.identity.sso.claims`.

An IdP sends whatever it likes, under whatever names it likes. Normalizing that
into the four things this product needs — email, display name, external subject,
group list — is :func:`normalize_claims`, driven by the per-connection claim
names an administrator configured, with alias fallbacks for the common cases.

The role decision is the security-critical half. Precedence is
``role_claim`` → ``role_mapping`` → ``group_mapping`` → ``default_role``, and
anything that does not resolve is **refused, not clamped**, when the connection
sets ``deny_unmapped_roles``. Two properties hold as a result:

* a user cannot gain a privilege by choosing a claim value, because only
  administrator-configured mappings are ever applied;
* an unmapped value cannot quietly demote a user either — refusal keeps the
  person's existing role, and the attempt is audited.
"""
from __future__ import annotations

from app.auth.identity.sso.claims import (
    NormalizedClaims,
    RoleDecision,
    decide_role,
    normalize_claims,
    parse_role,
    parse_role_mapping,
    safe_username,
)

__all__ = [
    "NormalizedClaims",
    "RoleDecision",
    "decide_role",
    "normalize_claims",
    "parse_role",
    "parse_role_mapping",
    "safe_username",
]
