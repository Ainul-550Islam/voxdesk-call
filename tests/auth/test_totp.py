"""TOTP, checked against the published test vectors.

The point of this file is interoperability: if our second factor answers the
RFC 4226 Appendix D and RFC 6238 Appendix B vectors correctly, then an
authenticator app that follows the RFC — which is all of them — will agree with
us. Anything asserted here from memory would be worthless, so every expected
value below is the one printed in the RFC.

The RFC's seeds are written as ASCII strings. Authenticator apps carry them as
base32, so the vectors are encoded in the test rather than typed in: a
transcription error in a 32-character base32 blob is otherwise invisible.

SHA-1 only: RFC 6238 also lists SHA-256 and SHA-512 vectors, but no widely used
authenticator app emits those, and ``totp.hotp`` deliberately implements the
SHA-1 algorithm (RFC 4226 §5.3) that clients require. The vectors below are the
SHA-1 column of the RFC, which is the column that has to be right.
"""
from __future__ import annotations

import base64
import inspect
import pathlib

import pytest

from app.auth.identity import totp

#: RFC 4226 Appendix D / RFC 6238 Appendix B: the ASCII seed.
RFC_SEED = b"12345678901234567890"
RFC_SEED_B32 = base64.b32encode(RFC_SEED).decode().rstrip("=")

#: RFC 4226 Appendix D, counters 0..9 with 6 digits.
HOTP_VECTORS = [
    (0, "755224"),
    (1, "287082"),
    (2, "359152"),
    (3, "969429"),
    (4, "338314"),
    (5, "254676"),
    (6, "287922"),
    (7, "162583"),
    (8, "399871"),
    (9, "520489"),
]

#: RFC 6238 Appendix B, SHA-1 column, 8 digits.
TOTP_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


def test_the_rfc_seed_encodes_to_the_expected_base32():
    assert RFC_SEED_B32 == "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.mark.unit
@pytest.mark.parametrize("counter,expected", HOTP_VECTORS)
def test_hotp_matches_rfc_4226_appendix_d(counter, expected):
    assert totp.hotp(RFC_SEED_B32, counter, 6) == expected


@pytest.mark.unit
@pytest.mark.parametrize("timestamp,expected", TOTP_VECTORS)
def test_totp_matches_rfc_6238_appendix_b(timestamp, expected):
    assert totp.totp(RFC_SEED_B32, timestamp, period=30, digits=8) == expected


@pytest.mark.unit
def test_a_generated_secret_is_base32_and_at_least_160_bits():
    secret = totp.generate_secret()
    assert len(secret) % 8 == 0
    assert len(secret) > 0
    raw = base64.b32decode(secret + "=" * (-len(secret) % 8))
    assert len(raw) >= 20, "RFC 4226 section 4.1 requires at least 160 bits"
    assert secret != totp.generate_secret()


@pytest.mark.unit
def test_a_short_secret_is_refused():
    with pytest.raises(ValueError):
        totp.generate_secret(10)


@pytest.mark.unit
def test_verify_finds_the_current_step():
    now = 1111111109.0
    code = totp.totp(RFC_SEED_B32, now, digits=8)
    assert totp.verify(RFC_SEED_B32, code, timestamp=now, digits=8) == totp.step_for(
        now, 30
    )


@pytest.mark.unit
def test_verify_accepts_one_step_of_skew_on_each_side():
    now = 1111111111.0
    previous = totp.totp(RFC_SEED_B32, now - 30, digits=8)
    following = totp.totp(RFC_SEED_B32, now + 30, digits=8)
    assert totp.verify(RFC_SEED_B32, previous, timestamp=now, digits=8) is not None
    assert totp.verify(RFC_SEED_B32, following, timestamp=now, digits=8) is not None


@pytest.mark.unit
def test_verify_refuses_a_code_from_outside_the_window():
    now = 1111111111.0
    for offset in (-90, -60, 60, 90):
        code = totp.totp(RFC_SEED_B32, now + offset, digits=8)
        assert totp.verify(RFC_SEED_B32, code, timestamp=now, digits=8) is None


@pytest.mark.unit
def test_verify_refuses_a_spent_step_even_while_it_is_still_in_the_window():
    """RFC 6238 section 5.2: a validated code must not be accepted twice."""
    now = 1111111111.0
    code = totp.totp(RFC_SEED_B32, now, digits=8)
    step = totp.verify(RFC_SEED_B32, code, timestamp=now, digits=8)
    assert step is not None

    # Same code, same window: the caller passes the step it consumed.
    assert totp.verify(RFC_SEED_B32, code, timestamp=now, digits=8, min_step=step) is None
    # And an *earlier* step is refused too, because the counter may not go back.
    earlier = totp.totp(RFC_SEED_B32, now - 30, digits=8)
    assert totp.verify(RFC_SEED_B32, earlier, timestamp=now, digits=8, min_step=step) is None
    # A later step is still accepted, so a clock that runs fast keeps working.
    later = totp.totp(RFC_SEED_B32, now + 30, digits=8)
    assert totp.verify(RFC_SEED_B32, later, timestamp=now, digits=8, min_step=step) is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    "code",
    ["", "12345", "1234567", "abcdef", "12345a", "9428708 ", "  94287  08"],
)
def test_verify_refuses_anything_that_is_not_a_code_of_the_right_length(code):
    # The one well-spaced form is stripped and accepted: users paste codes with
    # a space in them and refusing that would be user-hostile, not secure.
    result = totp.verify(RFC_SEED_B32, code, timestamp=59, digits=8)
    if code.replace(" ", "") == "9428708":
        assert result is None  # seven digits: still the wrong length
    else:
        assert result is None


@pytest.mark.unit
def test_verify_strips_spaces_inside_a_code():
    padded = " ".join(["94287082"[i : i + 2] for i in range(0, 8, 2)])
    assert totp.verify(RFC_SEED_B32, padded, timestamp=59, digits=8) is not None


@pytest.mark.unit
def test_a_code_from_a_different_secret_is_refused():
    other = totp.generate_secret()
    code = totp.totp(other, 59, digits=8)
    assert totp.verify(RFC_SEED_B32, code, timestamp=59, digits=8) is None


@pytest.mark.unit
def test_an_invalid_secret_is_rejected_rather_than_silently_accepted():
    with pytest.raises(ValueError):
        totp.hotp("not/base32!!", 0)
    with pytest.raises(ValueError):
        totp.verify("not/base32!!", "123456", timestamp=59)


@pytest.mark.unit
def test_arguments_are_validated():
    with pytest.raises(ValueError):
        totp.hotp(RFC_SEED_B32, -1)
    with pytest.raises(ValueError):
        totp.hotp(RFC_SEED_B32, 0, digits=7)
    with pytest.raises(ValueError):
        totp.totp(RFC_SEED_B32, 59, period=0)
    with pytest.raises(ValueError):
        totp.verify(RFC_SEED_B32, "123456", timestamp=59, period=0)


@pytest.mark.unit
def test_the_code_comparison_is_constant_time():
    """A six-digit secret compared with ``==`` leaks the matching prefix.

    This is a source-level assertion on purpose: timing is not measurable
    reliably in CI, but "somebody replaced ``compare_digest`` with ``==``" is
    exactly the regression this has to catch.
    """
    source = pathlib.Path(inspect.getsourcefile(totp)).read_text()
    assert "hmac.compare_digest(hotp(" in source
    assert "== candidate" not in source
    assert "candidate ==" not in source


@pytest.mark.unit
def test_the_provisioning_uri_is_what_an_authenticator_app_expects():
    uri = totp.provisioning_uri(
        RFC_SEED_B32, account_name="ada@acme.test", issuer="VoxDesk", digits=6, period=30
    )
    assert uri.startswith("otpauth://totp/VoxDesk%3Aada%40acme.test?")
    assert f"secret={RFC_SEED_B32}" in uri
    assert "issuer=VoxDesk" in uri
    assert "digits=6" in uri
    assert "period=30" in uri
    assert "algorithm=SHA1" in uri


@pytest.mark.unit
def test_a_secret_is_never_returned_with_padding_or_spaces():
    secret = totp.generate_secret()
    assert "=" not in secret and " " not in secret
    assert secret == secret.upper()
