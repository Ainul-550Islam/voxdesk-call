"""What a verified domain changes about signing in — and what it must not.

Implementation: :mod:`app.auth.identity.policies` (the evaluator) and
:mod:`app.auth.identity.domains` (enforcement state).

Enforcement is a three-valued setting per domain, and only one value refuses
anything:

``off``          the domain is decoration; nobody's login changes.
``warn``         password sign-in still works, and the login is flagged so the
                 workspace can nudge the customer towards SSO.
``require_sso``  password sign-in for addresses at that domain is refused
                 (``sso_required``), and the caller is told which connection to
                 use. This is the only mode that can lock somebody out, and it
                 cannot be set while the domain is unverified.

Independently, a verified domain may set ``block_password_login``. The policy
evaluator reads both, so a ``require_sso`` domain whose connection has been
disabled fails *closed* rather than falling back to passwords — and the
identity settings endpoint refuses to disable a connection while a verified
domain still requires it (409 ``no_active_sso_connection``).
"""
from __future__ import annotations

from app.auth.identity.models import DomainEnforcement, VerificationStatus
from app.auth.identity.policies import (
    DomainEvaluation,
    evaluate_domain_policy,
    load_domain_evaluation,
)
from app.auth.identity.domains import set_enforcement

__all__ = [
    "DomainEnforcement",
    "DomainEvaluation",
    "VerificationStatus",
    "evaluate_domain_policy",
    "load_domain_evaluation",
    "set_enforcement",
]
