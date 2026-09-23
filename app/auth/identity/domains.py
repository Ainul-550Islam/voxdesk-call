"""Enterprise domain claiming, DNS proof, and enforcement.

Flow: an administrator adds a domain, the service generates a one-time token
and tells them the TXT record to publish, they publish it, and someone presses
"verify". Nothing about a user's ability to log in changes until the domain is
*verified* **and** an administrator explicitly turns enforcement on.

That two-key design is the point. A single setting that both proves ownership
and starts refusing password logins is a setting that will eventually be
flipped by accident, and the failure mode is a whole company unable to sign in.
Here:

* ``verified_at is None`` → the domain is a claim, and imposes nothing. This is
  enforced in ``policies.evaluate_domain_policy``, not only here.
* ``enforcement = "require_sso"`` → password logins for that domain are refused
  **only** when the domain is verified.

DNS is read over DNS-over-HTTPS with ``httpx`` (already a dependency) rather
than through a resolver library, so there is nothing new to install and the
lookup is a plain HTTPS call that tests can replace by patching one function.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.identity import tokens as identity_tokens
from app.auth.identity.exceptions import (
    DomainConflict,
    DomainDNSUnavailable,
    DomainError,
    DomainNotVerified,
)
from app.auth.identity.models import (
    DomainEnforcement,
    DomainVerification,
    EnterpriseDomain,
    VerificationStatus,
)
from app.core.config import settings
from app.db.models import AuditAction, User

log = structlog.get_logger()

#: A label sequence, each 1–63 characters, no leading/trailing hyphen, at least
#: one dot. Deliberately does not try to validate TLDs: the list changes and a
#: wrong rejection is worse than a permissive one, since ownership is proven by
#: DNS anyway.
_DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$"
)

#: Reserved for the operator; a tenant cannot claim the platform's own names.
RESERVED_SUFFIXES = ("voxdesk.local", "localhost", "invalid", "example.com", "example.org")

#: How many times one challenge may be checked before it must be re-issued.
MAX_ATTEMPTS = 20


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_domain(raw: str) -> str:
    """Lowercase, strip a leading ``@``/scheme, drop a trailing dot.

    ``https://Acme.example.com/`` and ``acme.example.com.`` are the same claim,
    and treating them as different would let one tenant take a domain another
    tenant already proved.
    """
    value = (raw or "").strip().lower()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    value = value.split("/", 1)[0]
    if value.startswith("@"):
        value = value[1:]
    if value.endswith("."):
        value = value[:-1]
    return value


def validate_domain(raw: str) -> str:
    value = normalize_domain(raw)
    # The last label must be a name, not a number. That one rule is what keeps
    # ``127.0.0.1`` and other address literals out: there is no zone there to
    # publish a TXT record in, and an SSO enforcement rule keyed on an address
    # would mean nothing.
    if not _DOMAIN_RE.match(value) or not value.rsplit(".", 1)[-1].isalpha():
        raise DomainError(
            "That is not a valid domain name. Enter it without a scheme or path, "
            "for example acme.example.com"
        )
    if value.endswith(RESERVED_SUFFIXES) or value in {"localhost"}:
        raise DomainError("That domain is reserved and cannot be claimed.")
    return value


@dataclass(frozen=True)
class ChallengeIssue:
    verification: DomainVerification
    token: str
    record_name: str
    record_value: str


def _record_name(domain: str) -> str:
    return f"{settings.domain_verification_prefix}.{domain}"


def _record_value(token: str) -> str:
    # Prefixed so an operator looking at their zone file can see at a glance
    # what the record is for, and so a TXT lookup cannot be satisfied by an
    # unrelated SPF or verification record.
    return f"{settings.domain_verification_prefix}={token}"


# ------------------------------------------------------------------- DNS ---


async def lookup_txt_records(name: str) -> list[str]:
    """TXT values for a name, via the configured DNS-over-HTTPS resolver.

    Raises ``DomainDNSUnavailable`` when the resolver cannot be reached or
    answers with an error, which the caller records against the challenge --
    "DNS is down" must not be presented to the operator as "your record is
    wrong", because the two need different actions.
    """
    url = settings.domain_dns_resolver_url
    if not url:
        raise DomainDNSUnavailable("No DNS resolver is configured.")
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                url,
                params={"name": name, "type": "TXT"},
                headers={"accept": "application/dns-json"},
            )
    except httpx.HTTPError as exc:
        raise DomainDNSUnavailable("The DNS resolver could not be reached.") from exc

    if response.status_code != 200:
        raise DomainDNSUnavailable(
            f"The DNS resolver returned HTTP {response.status_code}."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise DomainDNSUnavailable("The DNS resolver returned an unreadable answer.") from exc

    if int(payload.get("Status", 0)) != 0:
        # NXDOMAIN / SERVFAIL: a legitimate answer meaning "no such record",
        # which is a verification failure rather than a resolver failure.
        return []

    values: list[str] = []
    for answer in payload.get("Answer", []) or []:
        if int(answer.get("type", 0)) != 16:  # TXT
            continue
        data = str(answer.get("data", ""))
        # DoH returns TXT chunks quoted and possibly split: "a" "b".
        parts = re.findall(r'"([^"]*)"', data)
        values.append("".join(parts) if parts else data)
    return values


def matches(expected: str, records: list[str]) -> bool:
    """Whether any published TXT value equals the challenge value."""
    wanted = expected.strip()
    return any((record or "").strip() == wanted for record in records)


# ----------------------------------------------------------------- writes ---


async def add_domain(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: User,
    domain: str,
    commit: bool = False,
) -> ChallengeIssue:
    """Claim a domain and open its first challenge.

    Called by ``POST /api/domains``. The unique constraint on ``domain`` is
    global, and the check below returns a *conflict* rather than a 404: a
    tenant learning that a domain is already claimed elsewhere is acceptable
    (it is public information who owns a domain), and hiding it would produce
    an unexplainable failure.
    """
    normalized = validate_domain(domain)

    existing = (
        await session.execute(
            select(EnterpriseDomain).where(EnterpriseDomain.domain == normalized)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.tenant_id == tenant_id:
            raise DomainConflict("That domain is already claimed by this workspace.")
        raise DomainConflict("That domain is already claimed by another workspace.")

    row = EnterpriseDomain(
        tenant_id=tenant_id,
        domain=normalized,
        enforcement=DomainEnforcement.OFF.value,
        block_password_login=False,
        created_by_user_id=actor.id,
    )
    session.add(row)
    await session.flush()

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.DOMAIN_ADDED,
        tenant_id=tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={"domain_id": str(row.id), "domain": normalized},
        commit=False,
    )
    issue = await issue_challenge(session, row, actor=actor, commit=False)
    if commit:
        await session.commit()
    return issue


async def issue_challenge(
    session: AsyncSession,
    domain_row: EnterpriseDomain,
    *,
    actor: User,
    commit: bool = False,
) -> ChallengeIssue:
    """Supersede any open challenge with a fresh token.

    The token is returned once, here; only its digest is stored. Re-issuing
    invalidates the previous token, so an old TXT record cannot be left in a
    zone and used to re-verify later.
    """
    now = _now()
    open_rows = (
        await session.execute(
            select(DomainVerification).where(
                DomainVerification.domain_id == domain_row.id,
                DomainVerification.status == VerificationStatus.PENDING.value,
            )
        )
    ).scalars().all()
    for row in open_rows:
        row.status = VerificationStatus.SUPERSEDED.value

    token = identity_tokens.new_human_code(groups=3, group_len=8)
    verification = DomainVerification(
        tenant_id=domain_row.tenant_id,
        domain_id=domain_row.id,
        method="dns_txt",
        record_name=_record_name(domain_row.domain),
        token_hash=identity_tokens.hash_token(token),
        status=VerificationStatus.PENDING.value,
        expires_at=now + timedelta(hours=settings.domain_verification_ttl_hours),
        created_by_user_id=actor.id,
    )
    session.add(verification)
    await session.flush()
    if commit:
        await session.commit()
    return ChallengeIssue(
        verification=verification,
        token=token,
        record_name=verification.record_name,
        record_value=_record_value(token),
    )


@dataclass(frozen=True)
class VerificationOutcome:
    verified: bool
    status: str
    detail: str
    records_seen: list[str]


async def check_challenge(
    session: AsyncSession,
    domain_row: EnterpriseDomain,
    *,
    actor: User,
    commit: bool = False,
) -> VerificationOutcome:
    """Look the TXT record up and settle the challenge.

    Called by ``POST /api/domains/{id}/verify``. A failure is recorded, not
    raised: the operator needs the attempt count and the resolver's answer, and
    a 500 would lose both.
    """
    verification = (
        await session.execute(
            select(DomainVerification)
            .where(
                DomainVerification.domain_id == domain_row.id,
                DomainVerification.status == VerificationStatus.PENDING.value,
            )
            .order_by(DomainVerification.created_at.desc())
        )
    ).scalars().first()
    if verification is None:
        raise DomainError("There is no pending verification for this domain. Start a new one.")

    now = _now()
    if verification.expires_at <= now:
        verification.status = VerificationStatus.EXPIRED.value
        if commit:
            await session.commit()
        raise DomainError("That verification challenge has expired. Start a new one.")

    if verification.attempts >= MAX_ATTEMPTS:
        verification.status = VerificationStatus.FAILED.value
        verification.last_error = "too_many_attempts"
        if commit:
            await session.commit()
        raise DomainError("Too many verification attempts. Start a new challenge.")

    verification.attempts += 1
    verification.last_checked_at = now

    from app.auth.identity.events import emit

    try:
        records = await lookup_txt_records(verification.record_name)
    except DomainDNSUnavailable as exc:
        verification.last_error = "resolver_unavailable"
        domain_row.failed_attempts += 1
        await emit(
            session,
            AuditAction.DOMAIN_VERIFICATION_FAILED,
            tenant_id=domain_row.tenant_id,
            actor_user_id=actor.id,
            actor_email=actor.email,
            detail={
                "domain_id": str(domain_row.id),
                "reason": "resolver_unavailable",
                "attempts": verification.attempts,
            },
            commit=False,
        )
        if commit:
            await session.commit()
        return VerificationOutcome(
            verified=False, status=verification.status, detail=str(exc), records_seen=[]
        )

    matched = _record_value_from_hash_check(verification, records)
    if not matched:
        verification.last_error = "record_not_found"
        domain_row.failed_attempts += 1
        await emit(
            session,
            AuditAction.DOMAIN_VERIFICATION_FAILED,
            tenant_id=domain_row.tenant_id,
            actor_user_id=actor.id,
            actor_email=actor.email,
            detail={
                "domain_id": str(domain_row.id),
                "reason": "record_not_found",
                "attempts": verification.attempts,
                "records_seen": len(records),
            },
            commit=False,
        )
        if commit:
            await session.commit()
        return VerificationOutcome(
            verified=False,
            status=verification.status,
            detail=(
                f"No matching TXT record found at {verification.record_name}. "
                "DNS changes can take a few minutes to become visible."
            ),
            records_seen=records,
        )

    verification.status = VerificationStatus.VERIFIED.value
    verification.verified_at = now
    verification.last_error = ""
    domain_row.verified_at = now
    domain_row.last_evidence_at = now
    domain_row.failed_attempts = 0

    await emit(
        session,
        AuditAction.DOMAIN_VERIFIED,
        tenant_id=domain_row.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "domain_id": str(domain_row.id),
            "domain": domain_row.domain,
            "record_name": verification.record_name,
            "attempts": verification.attempts,
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return VerificationOutcome(
        verified=True,
        status=verification.status,
        detail="Domain verified.",
        records_seen=records,
    )


def _record_value_from_hash_check(verification: DomainVerification, records: list[str]) -> bool:
    """Whether the published records contain a value hashing to the challenge.

    The token is not recoverable from the row, so the comparison goes the other
    way: every candidate value is hashed and compared to the stored digest. The
    value is compared with its ``prefix=`` form first (what we told the
    operator to publish), then on its own, so a provider that strips the
    separator still works.
    """
    for record in records:
        candidate = (record or "").strip()
        if not candidate:
            continue
        stripped = candidate.split("=", 1)[-1] if "=" in candidate else candidate
        for value in {candidate, stripped}:
            if identity_tokens.hash_token(value) == verification.token_hash:
                return True
    return False


async def set_enforcement(
    session: AsyncSession,
    domain_row: EnterpriseDomain,
    *,
    actor: User,
    enforcement: str,
    block_password_login: bool | None = None,
    sso_connection_id: uuid.UUID | None = None,
    commit: bool = False,
) -> EnterpriseDomain:
    """Change what a verified domain does. Never changes verification.

    Refused while the domain is unverified: an unverified domain imposing
    policy is exactly how a typo locks out a workforce.
    """
    try:
        target = DomainEnforcement(enforcement)
    except ValueError:
        raise DomainError(
            "Enforcement must be one of: off, warn, require_sso"
        ) from None
    if target is not DomainEnforcement.OFF and domain_row.verified_at is None:
        raise DomainNotVerified("Verify the domain before enabling enforcement.")

    if sso_connection_id is not None:
        from app.auth.identity.models import SSOConnection

        connection = await session.get(SSOConnection, sso_connection_id)
        if connection is None or connection.tenant_id != domain_row.tenant_id:
            raise DomainError("That SSO connection does not belong to this workspace.")
        domain_row.sso_connection_id = connection.id

    changes = {
        "enforcement": target.value,
        "previous": domain_row.enforcement,
    }
    domain_row.enforcement = target.value
    if block_password_login is not None:
        domain_row.block_password_login = bool(block_password_login)
        changes["block_password_login"] = domain_row.block_password_login

    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.DOMAIN_ENFORCEMENT_CHANGED,
        tenant_id=domain_row.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={"domain_id": str(domain_row.id), "domain": domain_row.domain, **changes},
        commit=False,
    )
    if commit:
        await session.commit()
    return domain_row


async def remove_domain(
    session: AsyncSession,
    domain_row: EnterpriseDomain,
    *,
    actor: User,
    reason: str = "removed_by_admin",
    commit: bool = False,
) -> None:
    """Delete the claim. Deleting an enforcing domain restores password login.

    Recorded explicitly, because "the domain disappeared and logins changed" is
    otherwise impossible to reconstruct from the audit trail.
    """
    from app.auth.identity.events import emit

    await emit(
        session,
        AuditAction.DOMAIN_REMOVED,
        tenant_id=domain_row.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "domain_id": str(domain_row.id),
            "domain": domain_row.domain,
            "was_verified": domain_row.verified_at is not None,
            "enforcement": domain_row.enforcement,
            "reason": reason,
        },
        commit=False,
    )
    await session.delete(domain_row)
    if commit:
        await session.commit()


async def list_domains(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[EnterpriseDomain]:
    return list(
        (
            await session.execute(
                select(EnterpriseDomain)
                .where(EnterpriseDomain.tenant_id == tenant_id)
                .order_by(EnterpriseDomain.domain.asc())
            )
        ).scalars().all()
    )


async def get_domain(
    session: AsyncSession, *, tenant_id: uuid.UUID, domain_id: uuid.UUID
) -> EnterpriseDomain | None:
    row = await session.get(EnterpriseDomain, domain_id)
    if row is None or row.tenant_id != tenant_id:
        return None
    return row


async def pending_challenge(
    session: AsyncSession, *, domain_id: uuid.UUID
) -> DomainVerification | None:
    return (
        await session.execute(
            select(DomainVerification)
            .where(
                DomainVerification.domain_id == domain_id,
                DomainVerification.status == VerificationStatus.PENDING.value,
            )
            .order_by(DomainVerification.created_at.desc())
        )
    ).scalars().first()


async def verified_domains(
    session: AsyncSession, *, domain: str
) -> list[EnterpriseDomain]:
    """Every *verified* enterprise-domain row for a domain name.

    Called by ``app.auth.identity.sso.service.connection_for_email`` before it
    routes a human to an identity provider, so the filter is deliberately
    narrow: only rows with a ``verified_at`` timestamp count. A row somebody
    merely typed into the dashboard is not evidence of ownership, and sending a
    user to an IdP chosen by whoever typed the name is the classic pre-hijack.

    More than one row can come back (two tenants claiming one domain is
    refused, not guessed, by the caller), so this returns a list. The name is
    normalised here rather than at the call site so an email domain in any case
    or with a trailing dot still matches.
    """
    normalized = normalize_domain(domain)
    if not normalized:
        return []
    rows = (
        await session.execute(
            select(EnterpriseDomain)
            .where(
                EnterpriseDomain.domain == normalized,
                EnterpriseDomain.verified_at.is_not(None),
            )
            .order_by(EnterpriseDomain.created_at.asc())
        )
    ).scalars().all()
    return list(rows)


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower() if "@" in (email or "") else ""
