"""One place that turns an identity failure into an HTTP response.

Routes raise domain errors (``app.auth.identity.exceptions``) and call
:func:`translate`, so the status code and the body for "your code was wrong" are
identical on every endpoint that can produce one. That matters for more than
tidiness: if one endpoint said ``401 {"detail": "invalid code"}`` and another
said ``400 {"detail": "expired"}``, the difference would be an oracle an
attacker could use to tell a wrong code from an expired challenge.

The bodies carry a stable ``code`` and a human ``message``. They never carry a
reason that depends on a secret, and never echo what was submitted.
"""
from __future__ import annotations

from fastapi import HTTPException, status

from app.auth.identity import exceptions as identity_exc


def translate(exc: identity_exc.IdentityError) -> HTTPException:
    """Map a domain error to the response a client should see."""

    # ---- reauthentication and policy -----------------------------------
    if isinstance(exc, identity_exc.ReauthenticationRequired):
        # 428 Precondition Required: the request was understood and authorized,
        # and cannot proceed until the caller proves presence again.
        return HTTPException(
            status_code=428,
            detail={"code": exc.code, "message": str(exc), "reason": exc.reason},
        )
    if isinstance(exc, identity_exc.PolicyDenied):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": exc.code, "message": str(exc), "reason": exc.reason},
        )

    # ---- MFA -----------------------------------------------------------
    if isinstance(exc, identity_exc.MFAChallengeLocked):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": exc.code, "message": str(exc)},
            headers={"Retry-After": "60"},
        )
    if isinstance(exc, identity_exc.MFAVerificationFailed):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": "That code is not valid."},
        )
    if isinstance(exc, identity_exc.MFAAlreadyEnrolled):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, identity_exc.MFANotEnrolled):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, identity_exc.MFADisabled):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, identity_exc.MFAError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc)},
        )

    # ---- sessions ------------------------------------------------------
    if isinstance(exc, identity_exc.SessionError):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": str(exc)},
        )

    # ---- SSO -----------------------------------------------------------
    if isinstance(exc, identity_exc.SSOStateExpired):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, identity_exc.SSOReplayDetected):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, identity_exc.SSOAccountLinkError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, (identity_exc.SSOConfigurationError, identity_exc.SSOValidationError)):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, identity_exc.SSOError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc)},
        )

    # ---- machine credentials -------------------------------------------
    if isinstance(exc, identity_exc.CredentialError):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ---- domains -------------------------------------------------------
    if isinstance(exc, identity_exc.DomainConflict):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, identity_exc.DomainError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc)},
        )

    # ---- one-time tokens ------------------------------------------------
    if isinstance(exc, identity_exc.TokenError):
        # 400 rather than 404: the *link* is the problem, and saying so does not
        # reveal whether the account it belonged to exists.
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc)},
        )

    # ---- delivery and configuration --------------------------------------
    if isinstance(exc, identity_exc.EmailDeliveryError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, identity_exc.IdentitySecretsUnavailable):
        # The deployment is missing its encryption key ring. That is an operator
        # problem, and 503 is the honest answer: nothing the caller did or can
        # do changes it, and the feature is unavailable until it is fixed.
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, identity_exc.IdentityError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc)},
        )

    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"code": "identity_error", "message": "The request could not be completed."},
    )


def as_http(exc: identity_exc.IdentityError) -> HTTPException:
    """Alias kept for readability at call sites that read `raise as_http(exc)`."""
    return translate(exc)
