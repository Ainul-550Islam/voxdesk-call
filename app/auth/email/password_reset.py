"""Password reset: issue, deliver, consume.

Implementation: :mod:`app.auth.identity.email` (tokens and delivery) and
:mod:`app.api.password_routes` (the HTTP flow, including the rate limits and the
uniform responses).

Order of operations on completion, and why:

1. the token row is consumed with a conditional UPDATE — a second use finds
   nothing to update and is refused;
2. the password is written through the repository's existing hasher
   (:mod:`app.auth.password`), so the reset path cannot drift from the signup
   path's policy;
3. **every other session is revoked** and the user's ``token_version`` is
   bumped, so a session an attacker already holds does not survive the reset;
4. a security notice goes out, and the event is audited.

An expired or unknown token produces the same refusal, and a request for an
address that has no account produces the same response as one that does.
"""
from __future__ import annotations

from app.auth.identity.email import (
    RenderedEmail,
    consume_password_reset_token,
    issue_password_reset_token,
    render_password_reset,
    reset_url,
    send_security_notice,
)

__all__ = [
    "RenderedEmail",
    "consume_password_reset_token",
    "issue_password_reset_token",
    "render_password_reset",
    "reset_url",
    "send_security_notice",
]
