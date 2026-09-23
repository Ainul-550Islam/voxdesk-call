"""SAML 2.0: the AuthnRequest, the response, and the signature.

Implementation: :mod:`app.auth.identity.sso.saml`. The XML-DSig verification is
hand-written (``verify_signature``) and the certificate parsing is
``read_certificate``; the only dependency in the path is ``lxml`` for parsing,
and parsing is where the first check happens:

* a document type declaration is refused *before* the tree is built, so entity
  expansion never gets a chance to run;
* exactly one signature, over exactly one reference, using an allowed
  canonicalization, digest and signature algorithm — a second reference is the
  signature-wrapping attack and it is refused rather than resolved;
* the signing certificate must be one that is on file **for that connection**
  and currently trusted. A certificate carried inside the assertion is not
  evidence of anything; the fingerprint is compared against the stored rows;
* the signed element must be the element the claims are read from, so a valid
  signature over a decoy assertion does not lend authority to the real one.

The claim-level checks — audience, recipient, destination, ``InResponseTo``,
``NotBefore``/``NotOnOrAfter`` with skew, subject confirmation, issuer, and the
single-use assertion id — are in ``validate_assertion`` and are what
``tests/auth/sso/test_saml_*.py`` attack one at a time.
"""
from __future__ import annotations

from app.auth.identity.sso.saml import (
    CertificateInfo,
    ParsedAssertion,
    VerifiedSignature,
    build_authn_request,
    certificate_fingerprint,
    find,
    findall,
    new_request_id,
    parse_instant,
    parse_response,
    read_certificate,
    safe_parse,
    signature_of,
    sp_metadata,
    validate_assertion,
    verify_signature,
)

__all__ = [
    "CertificateInfo",
    "ParsedAssertion",
    "VerifiedSignature",
    "build_authn_request",
    "certificate_fingerprint",
    "find",
    "findall",
    "new_request_id",
    "parse_instant",
    "parse_response",
    "read_certificate",
    "safe_parse",
    "signature_of",
    "sp_metadata",
    "validate_assertion",
    "verify_signature",
]
