"""Whether a factor is required, and how fresh a proof has to be.

Implementation: :mod:`app.auth.identity.policies` (the evaluator) and
:mod:`app.auth.identity.mfa` (the state it is evaluated against).

The precedence rule is the whole point of this module, and it is the rule that
gets mis-implemented when policy is scattered across handlers:

* a **per-user override wins in both directions** — an administrator can require
  a factor for one user who would otherwise not need one, and can exempt one
  user from a tenant-wide requirement;
* a tenant-wide requirement applies to everybody else;
* ``sso`` counts as a satisfied factor when the connection asserts a second
  factor, so a customer who signs in through their IdP is not asked for a TOTP
  code they do not have;
* enrollment alone does not force a challenge at login. The caller asks
  :func:`mfa_required_for` first; the presence of a factor row is not consent to
  be challenged.
"""
from __future__ import annotations

from app.auth.identity.policies import (
    AuthMethod,
    ResolvedPolicy,
    default_policy,
    load_policy,
    mfa_required_for,
    resolve_policy,
)

__all__ = [
    "AuthMethod",
    "ResolvedPolicy",
    "default_policy",
    "load_policy",
    "mfa_required_for",
    "resolve_policy",
]
