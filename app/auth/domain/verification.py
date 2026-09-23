"""Domain ownership: the challenge, the lookup, the verdict.

Implementation: :mod:`app.auth.identity.domains`.

The mechanism is a DNS TXT record:

* ``voxdesk-verify.<domain>`` must contain ``voxdesk-verify=<token>``;
* the token is random and stored **as a digest** — a leaked database does not
  hand anybody a domain;
* the record is read over DNS-over-HTTPS from a configured resolver, so the
  check does not depend on the container's resolver and cannot be poisoned by a
  local hosts file;
* the challenge expires (72 hours) and the attempt count is bounded (20), so a
  stale challenge cannot be brute-forced forever and an abandoned one does not
  sit around indefinitely;
* verification is *superseded*, not deleted: re-issuing a challenge invalidates
  the previous one, and the history stays.

Names are normalized and validated first — no wildcards, no ports, no paths, and
a reserved-suffix list — so the thing that gets verified is the thing the policy
will later match on.
"""
from __future__ import annotations

from app.auth.identity.domains import (
    MAX_ATTEMPTS,
    RESERVED_SUFFIXES,
    ChallengeIssue,
    VerificationOutcome,
    add_domain,
    check_challenge,
    issue_challenge,
    lookup_txt_records,
    matches,
    normalize_domain,
    validate_domain,
)

__all__ = [
    "MAX_ATTEMPTS",
    "RESERVED_SUFFIXES",
    "ChallengeIssue",
    "VerificationOutcome",
    "add_domain",
    "check_challenge",
    "issue_challenge",
    "lookup_txt_records",
    "matches",
    "normalize_domain",
    "validate_domain",
]
