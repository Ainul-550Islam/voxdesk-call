"""Signing certificates: import, rotation, retirement.

Implementation: :mod:`app.auth.identity.sso.service` (lifecycle) and
:mod:`app.auth.identity.sso.saml` (parsing and fingerprints).

Rotation on a SAML connection is the operation most likely to lock a customer
out, so the rules are asymmetrical on purpose:

* adding a certificate seals the PEM with the tenant-bound key ring and records
  its SHA-256 fingerprint. Adding the same certificate twice is a no-op
  (idempotent by fingerprint), so a retried request cannot create a duplicate
  that a later "retire" call would leave behind;
* a provider may have **two** certificates in force at once, which is exactly
  what a rollover looks like: the IdP signs with the new key while some
  in-flight assertions are still signed with the old one;
* retiring keeps the certificate usable for a 24-hour overlap window and then
  stops trusting it, so no assertion that was legitimately generated before the
  rollover is refused;
* retiring the **last** active certificate of an active connection is refused.
  That is the "do not silently lock the customer out" rule, enforced in the
  service rather than in a UI confirmation dialog.
"""
from __future__ import annotations

from app.auth.identity.sso.saml import (
    CertificateInfo,
    certificate_fingerprint,
    read_certificate,
)
from app.auth.identity.sso.service import (
    RETIRED_OVERLAP,
    active_certificate_count,
    add_certificate,
    list_certificates,
    retire_certificate,
    trusted_certificates,
)

__all__ = [
    "RETIRED_OVERLAP",
    "CertificateInfo",
    "active_certificate_count",
    "add_certificate",
    "certificate_fingerprint",
    "list_certificates",
    "read_certificate",
    "retire_certificate",
    "trusted_certificates",
]
