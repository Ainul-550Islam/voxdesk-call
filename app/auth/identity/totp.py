"""RFC 6238 time-based one-time passwords, on the standard library.

Why not ``pyotp``: this is forty lines of ``hmac``, the dependency is not in
``requirements.txt`` (so it is not in the deployment image), and a second-factor
verifier is a poor place to add a package whose update cadence we do not
control. The implementation below is the algorithm from RFC 4226 §5.3 with the
RFC 6238 dynamic truncation, checked against the RFC 6238 Appendix B vectors in
``tests/auth/test_totp.py`` — the same vectors every other implementation is
checked against, so interoperability is a test result rather than a hope.

Two behaviours worth stating outright, because they are what make a TOTP check
safe rather than merely correct:

* **Constant-time comparison.** ``hmac.compare_digest``, never ``==``. A short
  circuit on a six-digit code is not a theoretical problem once an attacker can
  submit thousands of guesses — which is exactly what an online second-factor
  endpoint allows.
* **One step of skew, and a used-step guard.** Clocks drift; accepting ±1 step
  (±30 s) is standard. Accepting a step that has *already* been spent would let
  a code observed by a shoulder-surfer be replayed inside its window, so the
  caller records the consumed step (``mfa_factors.last_totp_step``) and this
  module reports which step matched so it can be refused on reuse.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import urllib.parse

#: Base32 without padding, which is what every authenticator app expects.
_B32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def generate_secret(length: int = 20) -> str:
    """A fresh base32 secret. 20 bytes = 160 bits, the RFC 4226 §4.1 floor."""
    if length < 20:
        raise ValueError("TOTP secrets must be at least 160 bits (20 bytes)")
    return base64.b32encode(secrets.token_bytes(length)).decode().rstrip("=")


def _decode_secret(secret: str) -> bytes:
    normalized = (secret or "").strip().replace(" ", "").upper()
    padding = "=" * (-len(normalized) % 8)
    try:
        return base64.b32decode(normalized + padding)
    except Exception as exc:  # binascii.Error and friends
        raise ValueError("TOTP secret is not valid base32") from exc


def hotp(secret: str, counter: int, digits: int = 6) -> str:
    """RFC 4226 §5.3 HMAC-based one-time password."""
    if digits not in (6, 8):
        raise ValueError("TOTP digits must be 6 or 8")
    if counter < 0:
        raise ValueError("counter must be non-negative")
    key = _decode_secret(secret)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**digits)).zfill(digits)


def totp(secret: str, timestamp: float, period: int = 30, digits: int = 6) -> str:
    """RFC 6238 TOTP: the HOTP value for the current time step."""
    if period <= 0:
        raise ValueError("TOTP period must be positive")
    return hotp(secret, int(timestamp // period), digits)


def step_for(timestamp: float, period: int = 30) -> int:
    return int(timestamp // period)


def verify(
    secret: str,
    code: str,
    *,
    timestamp: float,
    period: int = 30,
    digits: int = 6,
    window: int = 1,
    min_step: int | None = None,
) -> int | None:
    """Verify a code. Returns the matching time step, or ``None``.

    Returning the step (rather than a bool) is what lets the caller refuse
    replay: it stores the step it just consumed, and passes it back as
    ``min_step`` on the next attempt.

    ``window`` is the number of steps of clock skew accepted on each side.
    ``window=1`` therefore accepts the previous, current and next step.
    """
    if period <= 0:
        # Validated here rather than only inside ``totp()``: this function
        # computes the current step itself, so a bad period would otherwise
        # surface as a ZeroDivisionError from inside the window loop.
        raise ValueError("TOTP period must be positive")

    candidate = (code or "").strip().replace(" ", "")
    if not candidate.isdigit() or len(candidate) != digits:
        return None

    current = step_for(timestamp, period)
    matched: int | None = None
    # Iterate the whole window even after a match so the loop's running time
    # does not depend on which step matched.
    for offset in range(-window, window + 1):
        step = current + offset
        if step < 0:
            continue
        if hmac.compare_digest(hotp(secret, step, digits), candidate):
            matched = step
    if matched is None:
        return None
    if min_step is not None and matched <= min_step:
        # Correct code, already spent. ``<=`` and not ``<``: the stored value is
        # the step that was *consumed*, so replaying a code inside the same
        # 30-second window must fail too -- accepting it would make every code
        # valid twice, which is exactly the replay this guard exists for. The
        # outcome is reported as "no match" so it is indistinguishable from a
        # wrong code and the caller counts it as a failure.
        return None
    return matched


def provisioning_uri(
    secret: str,
    *,
    account_name: str,
    issuer: str,
    digits: int = 6,
    period: int = 30,
) -> str:
    """The ``otpauth://`` URI an authenticator app consumes.

    Returned as text rather than a QR image: the dashboard renders the QR in
    the browser, which keeps a QR encoder out of the backend dependency list
    and keeps the secret off any third-party image service.
    """
    label = urllib.parse.quote(f"{issuer}:{account_name}", safe="")
    query = urllib.parse.urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": digits,
            "period": period,
        }
    )
    return f"otpauth://totp/{label}?{query}"


def format_secret_for_display(secret: str, group: int = 4) -> str:
    """Grouped base32, because humans transcribe this by hand."""
    return " ".join(secret[i : i + group] for i in range(0, len(secret), group))
