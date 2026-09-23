"""The two guards every machine-credential route needs.

``require_human`` and ``guard`` were private helpers inside
``app/api/machine_routes.py``. They are shared by
:mod:`app.api.api_key_routes` and :mod:`app.api.service_account_routes`, so they
live here rather than being copied into both — a copy is how one of the two ends
up missing the re-authentication step.

Both rules exist because machine credentials are powerful enough to be worth
stealing and easy to leave lying around:

* a **machine credential cannot manage machine credentials.** Minting an API key
  with another API key turns one leaked key into unbounded persistence, so the
  caller must be a signed-in human;
* the action must pass ``assert_privileged`` — permission, a fresh proof of
  presence, and the tenant's policy — and the session is rolled back before the
  refusal is translated, so a half-applied change cannot be left behind.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.identity_errors import translate
from app.auth.identity.exceptions import IdentityError
from app.auth.identity.service import IdentityContext, assert_privileged


def require_human(ctx: IdentityContext) -> None:
    """Refuse a machine principal acting on credentials."""
    if ctx.is_machine:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "human_session_required",
                "message": (
                    "Machine credentials cannot manage other machine credentials. "
                    "Use a signed-in user session."
                ),
            },
        )


async def guard(session: AsyncSession, ictx: IdentityContext, action: str) -> None:
    """Require a human, a permission and a fresh proof of presence."""
    require_human(ictx)
    try:
        await assert_privileged(session, ictx, action=action)
    except IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None


__all__ = ["guard", "require_human"]
