"""Outbound email, and the one-time tokens that travel in it.

Two things live here because they are inseparable: the message and the secret
inside it. Password-reset and email-verification tokens are generated, hashed
and stored by this module, and the same call that stores one sends it — so
there is no code path that can mail a link whose token was never recorded, and
no path that records a token without telling the user about it.

Transport is pluggable and defaults to ``log``:

* ``log`` renders the message and writes *metadata only* to the structured log
  (recipient, purpose, token expiry). The token itself is not logged; in
  development the operator reads it from the response of the request that
  created it, which is why the dev-mode response includes it and production's
  does not. This is the one place the design deliberately trades a little
  convenience for the rule that tokens never reach a log file.
* ``smtp`` delivers through ``SMTP_HOST``, with STARTTLS unless disabled. The
  send runs in a worker thread (``asyncio.to_thread``) because ``smtplib`` is
  blocking, and a login that waits on a mail server is a login that times out.
  Credentials come from the settings object and are never rendered.

Production refuses to boot with ``EMAIL_TRANSPORT=log`` (``validate_security``),
so the convenient default cannot follow a deployment into production.
"""
from __future__ import annotations

import asyncio
import smtplib
import ssl
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.identity import tokens as identity_tokens
from app.auth.identity.exceptions import EmailDeliveryError
from app.core.config import settings

log = structlog.get_logger()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class OneTimeToken:
    """A single-use token: the plaintext (returned once) and its row."""

    plaintext: str
    token_id: uuid.UUID
    expires_at: datetime


# --------------------------------------------------------------- generation ---


def new_one_time_token(*, prefix: str = "vd1t", nbytes: int = 32) -> tuple[str, str]:
    """``(plaintext, sha256)``. Only the digest is ever stored."""
    plaintext = identity_tokens.new_token(prefix, nbytes)
    return plaintext, identity_tokens.hash_token(plaintext)


def expiry_for(*, minutes: int = 0, hours: int = 0) -> datetime:
    return _now() + timedelta(minutes=minutes, hours=hours)


# ---------------------------------------------------------------- rendering ---


@dataclass(frozen=True)
class RenderedEmail:
    to: str
    subject: str
    text_body: str
    html_body: str


def render_password_reset(
    *, to: str, reset_url: str, ttl_minutes: int, tenant_name: str = "VoxDesk"
) -> RenderedEmail:
    text = (
        f"Someone asked to reset the password for your {tenant_name} account.\n\n"
        f"Open this link to choose a new password:\n{reset_url}\n\n"
        f"The link works once and expires in {ttl_minutes} minutes. "
        "If you did not ask for this, you can ignore this message -- your "
        "password has not changed.\n"
    )
    html = _wrap_html(
        tenant_name,
        "Reset your password",
        [
            f"Someone asked to reset the password for your {tenant_name} account.",
            "Open the link below to choose a new password. It works once and "
            f"expires in {ttl_minutes} minutes.",
            "If you did not ask for this, you can ignore this message — your "
            "password has not changed.",
        ],
        reset_url,
        "Choose a new password",
    )
    return RenderedEmail(to=to, subject=f"Reset your {tenant_name} password",
                         text_body=text, html_body=html)


def render_email_verification(
    *, to: str, verify_url: str, ttl_hours: int, tenant_name: str = "VoxDesk"
) -> RenderedEmail:
    text = (
        f"Confirm this address to finish setting up your {tenant_name} account.\n\n"
        f"Open this link to verify {to}:\n{verify_url}\n\n"
        f"The link works once and expires in {ttl_hours} hours. "
        "Until it is confirmed, password resets and security notices for this "
        "address stay disabled.\n"
    )
    html = _wrap_html(
        tenant_name,
        "Confirm your email address",
        [
            f"Confirm this address to finish setting up your {tenant_name} account.",
            f"Open the link below to verify {to}. It works once and expires in "
            f"{ttl_hours} hours.",
            "Until it is confirmed, password resets and security notices for "
            "this address stay disabled.",
        ],
        verify_url,
        "Confirm email address",
    )
    return RenderedEmail(to=to, subject=f"Confirm your {tenant_name} email address",
                         text_body=text, html_body=html)


def render_security_notice(
    *, to: str, title: str, lines: list[str], tenant_name: str = "VoxDesk"
) -> RenderedEmail:
    text = f"{title}\n\n" + "\n".join(lines) + f"\n\n-- {tenant_name}\n"
    html = _wrap_html(tenant_name, title, lines, "", "")
    return RenderedEmail(to=to, subject=f"{title} - {tenant_name}", text_body=text, html_body=html)


def _wrap_html(
    tenant_name: str, title: str, paragraphs: list[str], link: str, link_label: str
) -> str:
    """A minimal, dependency-free HTML body.

    Inline styles only: an email client is not going to fetch a stylesheet, and
    a template engine would be a new dependency for four paragraphs of text.
    Every interpolated value is escaped -- the tenant name comes from the
    database and the URL is built by us, but a security email is exactly the
    wrong place to be relaxed about that.
    """
    import html as html_module

    body = "".join(
        f'<p style="margin:0 0 14px;line-height:1.55">{html_module.escape(p)}</p>'
        for p in paragraphs
    )
    button = ""
    if link:
        button = (
            '<p style="margin:22px 0">'
            f'<a href="{html_module.escape(link, quote=True)}" '
            'style="background:#1f6feb;color:#ffffff;padding:11px 18px;'
            'border-radius:6px;text-decoration:none;display:inline-block">'
            f"{html_module.escape(link_label)}</a></p>"
            f'<p style="margin:0 0 14px;font-size:13px;color:#57606a;'
            f'word-break:break-all">{html_module.escape(link)}</p>'
        )
    return (
        '<!doctype html><html><body style="margin:0;background:#f6f8fa;'
        'padding:24px;font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        'color:#1f2328">'
        '<div style="max-width:520px;margin:0 auto;background:#ffffff;'
        'border:1px solid #d0d7de;border-radius:10px;padding:26px">'
        f'<h1 style="margin:0 0 16px;font-size:19px">{html_module.escape(title)}</h1>'
        f"{body}{button}</div></body></html>"
    )


# ---------------------------------------------------------------- delivery ---


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    transport: str
    detail: str = ""


async def send(message: RenderedEmail) -> DeliveryResult:
    """Deliver a rendered message using the configured transport.

    Raises ``EmailDeliveryError`` when delivery is attempted and fails, so the
    caller can decide whether that matters (it does for a reset link, it does
    not for a courtesy notice).
    """
    transport = (settings.email_transport or "log").strip().lower()
    if transport == "log":
        log.info(
            "identity.email_rendered",
            transport="log",
            to=_mask_address(message.to),
            subject=message.subject,
            body_chars=len(message.text_body),
        )
        return DeliveryResult(delivered=True, transport="log", detail="rendered")

    if transport == "smtp":
        try:
            await asyncio.to_thread(_send_smtp, message)
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed failure
            # The exception text can contain the SMTP server's response, which
            # is safe, but not always the URL we passed; log the type only and
            # keep the message out of the structured log.
            log.warning(
                "identity.email_delivery_failed",
                transport="smtp",
                to=_mask_address(message.to),
                error_type=type(exc).__name__,
            )
            raise EmailDeliveryError("Could not send the message.") from exc
        log.info(
            "identity.email_delivered", transport="smtp", to=_mask_address(message.to)
        )
        return DeliveryResult(delivered=True, transport="smtp")

    raise EmailDeliveryError(f"Unknown email transport: {transport!r}")


def _send_smtp(message: RenderedEmail) -> None:
    payload = EmailMessage()
    payload["From"] = settings.email_from
    payload["To"] = message.to
    payload["Subject"] = message.subject
    payload.set_content(message.text_body)
    payload.add_alternative(message.html_body, subtype="html")

    if settings.smtp_starttls:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as client:
            client.ehlo()
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
            if settings.smtp_username:
                client.login(settings.smtp_username, settings.smtp_password)
            client.send_message(payload)
        return

    with smtplib.SMTP_SSL(
        settings.smtp_host, settings.smtp_port, timeout=15, context=ssl.create_default_context()
    ) as client:
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(payload)


def _mask_address(address: str) -> str:
    """``a***@example.com``. Addresses are personal data; logs are long-lived."""
    local, _, domain = (address or "").partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


async def send_security_notice(
    *, to: str, title: str, lines: list[str], tenant_name: str = "VoxDesk"
) -> DeliveryResult:
    """Best-effort notice. Never raises: a notice must not fail an action."""
    try:
        return await send(render_security_notice(to=to, title=title, lines=lines,
                                                tenant_name=tenant_name))
    except EmailDeliveryError as exc:
        log.warning("identity.notice_failed", reason=exc.code)
        return DeliveryResult(delivered=False, transport=settings.email_transport)


# ------------------------------------------------------------ token storage ---


async def issue_password_reset_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    commit: bool = False,
) -> OneTimeToken:
    """Create a single-use reset token, invalidating any earlier one.

    Invalidating the earlier ones is not housekeeping: two live reset links for
    one account means an attacker who obtained the first still has a way in
    after the user asks for a second.
    """
    from app.auth.identity.models import PasswordResetToken

    existing = (
        await session.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.used_at.is_(None),
            )
        )
    ).scalars().all()
    now = _now()
    for row in existing:
        row.used_at = now

    plaintext, digest = new_one_time_token(prefix="vd1r")
    row = PasswordResetToken(
        tenant_id=tenant_id,
        user_id=user_id,
        token_hash=digest,
        expires_at=expiry_for(minutes=settings.password_reset_ttl_minutes),
    )
    session.add(row)
    await session.flush()
    if commit:
        await session.commit()
    return OneTimeToken(plaintext=plaintext, token_id=row.id, expires_at=row.expires_at)


async def consume_password_reset_token(
    session: AsyncSession, *, plaintext: str, tenant_id_hint: uuid.UUID | None = None
) -> uuid.UUID:
    """Atomically spend a reset token and return the user it belonged to.

    The claim is a conditional UPDATE (``used_at IS NULL``) rather than a read
    followed by a write, so two requests presenting the same link cannot both
    succeed — the second reports the same "invalid or expired" failure a stale
    link would.
    """
    from sqlalchemy import update

    from app.auth.identity.exceptions import TokenError
    from app.auth.identity.models import PasswordResetToken

    digest = identity_tokens.hash_token(plaintext)
    row = (
        await session.execute(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == digest)
        )
    ).scalar_one_or_none()
    if row is None:
        raise TokenError("That reset link is not valid.")
    if row.expires_at <= _now():
        raise TokenError("That reset link has expired.")
    if tenant_id_hint is not None and row.tenant_id != tenant_id_hint:
        raise TokenError("That reset link is not valid.")

    result = await session.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.id == row.id, PasswordResetToken.used_at.is_(None))
        .values(used_at=_now())
    )
    if not result.rowcount:
        raise TokenError("That reset link has already been used.")
    return row.user_id


async def issue_verification_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    email: str,
    commit: bool = False,
) -> OneTimeToken:
    from app.auth.identity.models import EmailVerificationToken

    existing = (
        await session.execute(
            select(EmailVerificationToken).where(
                EmailVerificationToken.user_id == user_id,
                EmailVerificationToken.used_at.is_(None),
            )
        )
    ).scalars().all()
    now = _now()
    for row in existing:
        row.used_at = now

    plaintext, digest = new_one_time_token(prefix="vd1v")
    row = EmailVerificationToken(
        tenant_id=tenant_id,
        user_id=user_id,
        email=email[:320],
        token_hash=digest,
        expires_at=expiry_for(hours=settings.email_verification_ttl_hours),
    )
    session.add(row)
    await session.flush()
    if commit:
        await session.commit()
    return OneTimeToken(plaintext=plaintext, token_id=row.id, expires_at=row.expires_at)


async def consume_verification_token(session: AsyncSession, *, plaintext: str) -> uuid.UUID:
    from sqlalchemy import update

    from app.auth.identity.exceptions import TokenError
    from app.auth.identity.models import EmailVerificationToken

    digest = identity_tokens.hash_token(plaintext)
    row = (
        await session.execute(
            select(EmailVerificationToken).where(EmailVerificationToken.token_hash == digest)
        )
    ).scalar_one_or_none()
    if row is None:
        raise TokenError("That verification link is not valid.")
    if row.expires_at <= _now():
        raise TokenError("That verification link has expired.")

    result = await session.execute(
        update(EmailVerificationToken)
        .where(
            EmailVerificationToken.id == row.id,
            EmailVerificationToken.used_at.is_(None),
        )
        .values(used_at=_now())
    )
    if not result.rowcount:
        raise TokenError("That verification link has already been used.")
    return row.user_id


def reset_url(plaintext: str, *, locale: str = "") -> str:
    """The link that goes in the mail. Built from ``PUBLIC_BASE_URL``."""
    base = settings.public_base_url.rstrip("/")
    return f"{base}/reset-password?token={plaintext}"


def verification_url(plaintext: str) -> str:
    base = settings.public_base_url.rstrip("/")
    return f"{base}/verify-email?token={plaintext}"


def delivery_is_configured() -> bool:
    """Whether mail can actually leave the process.

    Used to decide if a *request* for a reset is plausible. It never changes
    the response, which is uniform either way.
    """
    transport = (settings.email_transport or "").strip().lower()
    if transport == "smtp":
        return bool(settings.smtp_host)
    return transport == "log"
