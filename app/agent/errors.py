"""Provider error taxonomy for the live voice layer.

The voice pipeline talks to three families of external systems — STT
(Deepgram), TTS (ElevenLabs) and LLMs (OpenAI / Anthropic / Google). Each can
fail in a small, well-understood set of ways, and the rest of the system needs
to answer three questions about any failure:

* is it *safe to show / log* (never a secret)?
* is it *retryable* (transient) or permanent?
* what *category* does it fall into, so state handling, metrics and logs agree?

This mirrors the taxonomy already used by the CRM adapters
(``app/integrations/crm/errors.py``) — one small hierarchy, each class carrying
a ``retryable`` flag and a ``category`` label — but is deliberately independent
of it: CRM errors wrap HTTP responses, while these wrap *live provider
behaviour* before and during a call.

Every message is constructed by the raising code from fixed, safe text (no
provider payloads, no keys, no customer data). ``ProviderError`` subclasses
``RuntimeError`` so that existing callers which treat provider failures as
ordinary runtime failures keep working unchanged.
"""
from __future__ import annotations

from typing import Any

#: Bounded set of categories. Metrics and dashboards group by these, so the
#: set is deliberately closed — a new category is a deliberate change.
CATEGORIES = (
    "configuration_error",
    "authentication_error",
    "authorization_error",
    "rate_limit",
    "timeout",
    "unavailable",
    "invalid_request",
    "unsupported_feature",
    "provider_error",
)


class ProviderError(RuntimeError):
    """Base class for every voice-provider failure.

    ``category`` is one of :data:`CATEGORIES`. ``retryable`` is True only for
    transient failures where a bounded retry is safe (never for invalid
    credentials, invalid requests or unsupported features).
    """

    category: str = "provider_error"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        provider: str = "unknown",
        category: str | None = None,
        retryable: bool | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.detail = detail
        if category is not None:
            self.category = category
        if retryable is not None:
            self.retryable = retryable

    @property
    def safe_message(self) -> str:
        """The message, guaranteed by construction to be safe to log/show."""
        return self.message

    def __str__(self) -> str:
        return f"{self.provider}/{self.category}: {self.message}"


class ProviderConfigurationError(ProviderError):
    """The provider is not configured (missing key, empty URL, bad setting).

    Permanent by definition: retrying without a config change cannot help.
    """

    category = "configuration_error"
    retryable = False


class ProviderAuthenticationError(ProviderError):
    """The provider rejected our credentials (401 / bad key)."""

    category = "authentication_error"
    retryable = False


class ProviderAuthorizationError(ProviderError):
    """The credentials are valid but lack permission for the request (403)."""

    category = "authorization_error"
    retryable = False


class ProviderRateLimitedError(ProviderError):
    """The provider rate-limited us (429). Carries an optional retry delay."""

    category = "rate_limit"
    retryable = True

    def __init__(
        self,
        message: str = "Provider rate limited",
        *,
        provider: str = "unknown",
        retry_after: float | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message, provider=provider, detail=detail)
        self.retry_after = retry_after


class ProviderTimeoutError(ProviderError):
    """A provider call exceeded its bounded time budget."""

    category = "timeout"
    retryable = True


class ProviderUnavailableError(ProviderError):
    """The provider is unreachable or returned 5xx. Transient."""

    category = "unavailable"
    retryable = True


class ProviderInvalidRequestError(ProviderError):
    """We sent something the provider rejected as malformed. Permanent."""

    category = "invalid_request"
    retryable = False


class UnsupportedProviderFeatureError(ProviderError):
    """We asked for something the provider (or our SDK) cannot express.

    Raised instead of silently ignoring an unsupported setting — an
    unsupported parameter must fail clearly, never disappear.
    """

    category = "unsupported_feature"
    retryable = False


class ProviderRuntimeError(ProviderError):
    """Any other provider failure with no more specific category."""

    category = "provider_error"
    retryable = False


# ---------------------------------------------------------- classification ---
#
# SDK exceptions are provider-specific (``openai.APITimeoutError``,
# ``anthropic.RateLimitError``, ``httpx.ConnectError``, ...). The taxonomy
# above is not: it is the closed vocabulary that metrics, logs and state
# handling agree on. ``classify_provider_exception`` is the single translation
# layer between them, so every call site classifies the same failure the same
# way — the same shape as
# ``app/integrations/crm/errors.py::classify_status`` for the CRM adapters.
#
# It is name- and status-based rather than ``isinstance``-based on purpose: it
# has to work when only one of the three LLM SDKs is installed, and importing a
# provider SDK into the error taxonomy would make this module (imported by the
# telephony layer, the text agent and the pipeline) depend on every SDK.

#: Attribute names an SDK may expose an HTTP status under.
_STATUS_ATTRS = ("status_code", "http_status", "status")

#: Exception-class-name fragment -> category. Ordered, and the first match
#: wins: "timeout" is checked before "connection" because the OpenAI SDK's
#: ``APITimeoutError`` is a subclass of its ``APIConnectionError``.
_CATEGORY_BY_NAME: tuple[tuple[str, type[ProviderError]], ...] = (
    ("timeout", ProviderTimeoutError),
    ("timedout", ProviderTimeoutError),
    ("ratelimit", ProviderRateLimitedError),
    ("toomanyrequests", ProviderRateLimitedError),
    ("authentication", ProviderAuthenticationError),
    ("unauthenticated", ProviderAuthenticationError),
    ("permissiondenied", ProviderAuthorizationError),
    ("authorization", ProviderAuthorizationError),
    ("forbidden", ProviderAuthorizationError),
    ("badrequest", ProviderInvalidRequestError),
    ("invalidrequest", ProviderInvalidRequestError),
    ("unprocessable", ProviderInvalidRequestError),
    ("notfound", ProviderInvalidRequestError),
    ("conflict", ProviderInvalidRequestError),
    ("unavailable", ProviderUnavailableError),
    ("overloaded", ProviderUnavailableError),
    ("internalserver", ProviderUnavailableError),
    ("serviceerror", ProviderUnavailableError),
    ("apiconnection", ProviderUnavailableError),
    ("connection", ProviderUnavailableError),
    ("connecterror", ProviderUnavailableError),
)


def _status_code(exc: BaseException) -> int | None:
    """The HTTP status an SDK exception carries, if it carries one.

    Checked on the exception itself (``status_code``/``http_status``/``status``)
    and then on a nested ``response`` (httpx, and therefore openai/anthropic,
    keep it there). ``bool`` is excluded because it is an ``int`` subclass.
    """
    for attr in _STATUS_ATTRS:
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _classified(
    cls: type[ProviderError], provider: str, *, status: int | None, detail: dict
) -> ProviderError:
    """Build the instance with fixed, payload-free text.

    The SDK's own message is deliberately **not** copied: it can quote the
    request, and a request can contain the API key or a customer's words. The
    raised error chains the original (``raise ... from exc``), so the traceback
    is still there for a human — while the logged message stays safe.
    """
    suffix = f" (HTTP {status})" if status is not None else ""
    if cls is ProviderRateLimitedError:
        return ProviderRateLimitedError(provider=provider, detail=detail)
    if cls is ProviderTimeoutError:
        return ProviderTimeoutError(f"{provider} timed out", provider=provider, detail=detail)
    if cls is ProviderAuthenticationError:
        return ProviderAuthenticationError(
            f"{provider} rejected the credentials{suffix}", provider=provider, detail=detail
        )
    if cls is ProviderAuthorizationError:
        return ProviderAuthorizationError(
            f"{provider} denied the request{suffix}", provider=provider, detail=detail
        )
    if cls is ProviderInvalidRequestError:
        return ProviderInvalidRequestError(
            f"{provider} rejected the request{suffix}", provider=provider, detail=detail
        )
    if cls is ProviderUnavailableError:
        return ProviderUnavailableError(
            f"{provider} is unavailable{suffix}", provider=provider, detail=detail
        )
    return ProviderRuntimeError(
        f"{provider} call failed ({detail.get('exception_type', 'unknown')})",
        provider=provider, detail=detail,
    )


def classify_provider_exception(
    exc: BaseException, *, provider: str = "unknown"
) -> ProviderError:
    """Translate a provider SDK exception into this taxonomy.

    Rules, in order:

    1. an already-classified :class:`ProviderError` is returned unchanged, so
       classifying twice is a no-op;
    2. ``TimeoutError`` (which covers ``asyncio.TimeoutError`` on 3.11+) is a
       timeout;
    3. an HTTP status wins over the class name: 408/504 timeout, 429 rate
       limit, 401 authentication, 403 authorization, other 4xx invalid request,
       5xx unavailable;
    4. otherwise the exception class name is matched against a small ordered
       table of fragments (``"ratelimit"``, ``"authentication"``,
       ``"connection"``, ...);
    5. anything else is a :class:`ProviderRuntimeError` — still typed, never
       silently swallowed.

    Callers are expected to ``raise ... from exc`` (the helper does not raise),
    so the original exception stays in the chain.
    """
    if isinstance(exc, ProviderError):
        return exc

    name = type(exc).__name__
    status = _status_code(exc)
    detail: dict[str, Any] = {"exception_type": name}
    if status is not None:
        detail["status"] = status

    if isinstance(exc, TimeoutError):
        return _classified(ProviderTimeoutError, provider, status=status, detail=detail)

    if status is not None:
        if status in (408, 504):
            # HTTP's own "the other side did not answer in time" statuses:
            # 408 Request Timeout and 504 Gateway Timeout. Retryable, and the
            # same operational category as a client-side deadline.
            return _classified(ProviderTimeoutError, provider, status=status, detail=detail)
        if status == 429:
            return _classified(ProviderRateLimitedError, provider, status=status, detail=detail)
        if status == 401:
            return _classified(ProviderAuthenticationError, provider, status=status, detail=detail)
        if status == 403:
            return _classified(ProviderAuthorizationError, provider, status=status, detail=detail)
        if 400 <= status < 500:
            return _classified(ProviderInvalidRequestError, provider, status=status, detail=detail)
        if status >= 500:
            return _classified(ProviderUnavailableError, provider, status=status, detail=detail)

    lowered = name.lower()
    for fragment, cls in _CATEGORY_BY_NAME:
        if fragment in lowered:
            return _classified(cls, provider, status=status, detail=detail)

    return _classified(ProviderRuntimeError, provider, status=status, detail=detail)
