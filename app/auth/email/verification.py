"""Email ownership verification.

Implementation: :mod:`app.auth.identity.email`.

The token is issued against the address it was sent to and consumed once.
Consumption marks the address verified (a ``user_emails`` row, and the user's
``email_verified`` flag) and refuses a token that has already been used, has
expired, or belongs to a different user — the check is on the row, not on
whatever the caller says.

If SMTP is not configured the transport reports that plainly instead of throwing
into a login path, and the debug-token path that hands the token back to a
developer exists only under development configuration: it is refused outright in
production, because "the API returned the reset link" is a backdoor.
"""
from __future__ import annotations

from app.auth.identity.email import (
    DeliveryResult,
    OneTimeToken,
    RenderedEmail,
    consume_verification_token,
    delivery_is_configured,
    expiry_for,
    issue_verification_token,
    new_one_time_token,
    render_email_verification,
    render_security_notice,
    send,
    send_security_notice,
    verification_url,
)

__all__ = [
    "DeliveryResult",
    "OneTimeToken",
    "RenderedEmail",
    "consume_verification_token",
    "delivery_is_configured",
    "expiry_for",
    "issue_verification_token",
    "new_one_time_token",
    "render_email_verification",
    "render_security_notice",
    "send",
    "send_security_notice",
    "verification_url",
]
