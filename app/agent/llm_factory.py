"""Multi-provider LLM factory.

একই কোডে তিনটা AI চলে — ক্লায়েন্ট প্রতি আলাদা করে বেছে নেওয়া যায়:

    openai     -> gpt-4o-mini        সবচেয়ে দ্রুত, সস্তা, tool calling নিখুঁত
    anthropic  -> claude-haiku-4-5   সবচেয়ে natural কথা বলে, কম hallucinate করে
    google     -> gemini-2.0-flash   সবচেয়ে সস্তা, বহু ভাষা, লম্বা context

কেন তিনটাই দরকার?
  1. একটা provider ডাউন হলে fallback লাগে (ফোন কল অপেক্ষা করে না)
  2. ক্লায়েন্টভেদে খরচ/মান আলাদা — ডেন্টাল ক্লিনিকে haiku, রেস্টুরেন্টে flash
  3. Upwork-এ "multi-LLM, provider-agnostic" লিখলে রেট বাড়ে

Failure behaviour (Step 3)
--------------------------
* An unknown provider name is an ``UnsupportedProviderFeatureError`` — it
  fails clearly instead of silently falling back to whichever provider has a
  key, so a tenant with a typo'd provider does not get a different AI without
  anyone noticing.
* A known provider whose key is missing falls back to the next configured
  provider (existing behaviour). Only when **no** provider has a key do we
  raise ``ProviderConfigurationError``.

The credential contract (Step 17, P0)
-------------------------------------
Selection and credentials are two different things and live in two different
places:

* :class:`LLMChoice` (what :func:`resolve` returns) is **selection only** —
  provider, model, expected latency, notes. It is safe to log and to show in a
  dashboard, and it therefore must never carry an ``api_key``.
* The credentials are deployment configuration
  (``app.core.config.settings``), read through :func:`api_key_for` and never
  from a tenant row.

:func:`select_for_use` and :func:`validate_llm_config` are the two consumer
contracts built on top: the first applies the documented fallback (voice
behaviour), the second refuses to answer when the chosen provider has no key
(the text agent's behaviour). Both are total functions — a missing key can
never surface as an ``AttributeError``/``TypeError`` inside an agent
constructor.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.agent.errors import (
    ProviderConfigurationError,
    UnsupportedProviderFeatureError,
)
from app.core.config import settings
from app.core.logging import log


@dataclass(frozen=True)
class LLMChoice:
    provider: str          # openai | anthropic | google
    model: str
    est_latency_ms: int    # first-token, রেফারেন্স হিসেবে
    notes: str


# ক্লায়েন্টকে দেখানোর জন্য প্রিসেট
PRESETS: dict[str, LLMChoice] = {
    "fast": LLMChoice("openai", "gpt-4o-mini", 250, "সবচেয়ে দ্রুত, tool calling নিখুঁত"),
    "natural": LLMChoice("anthropic", "claude-haiku-4-5", 300, "সবচেয়ে মানুষের মতো কথা"),
    "cheap": LLMChoice("google", "gemini-2.0-flash", 280, "সবচেয়ে সস্তা, বহুভাষী"),
    "smart": LLMChoice("anthropic", "claude-sonnet-4-5", 600, "জটিল কথোপকথন, ধীর"),
}

#: The providers this factory can actually construct a service for.
SUPPORTED_PROVIDERS = ("openai", "anthropic", "google")

#: What each LLM provider integration can and cannot express. The pipeline
#: consults this before registering tools: the agent's booking/escalation
#: tools are business-critical, so a provider that cannot do tool-calling
#: must fail clearly rather than silently ignore the tool schemas. Streaming
#: and interruptions are declared here for the same reason — an "unsupported"
#: entry means the pipeline must not silently promise barge-in it cannot give.
LLM_CAPABILITIES: dict[str, dict[str, bool]] = {
    "openai": {
        "supports_tool_calling": True,
        "supports_streaming": True,
        "supports_interruptions": True,
    },
    "anthropic": {
        "supports_tool_calling": True,
        "supports_streaming": True,
        "supports_interruptions": True,
    },
    "google": {
        "supports_tool_calling": True,
        "supports_streaming": True,
        "supports_interruptions": True,
    },
}

# provider ডাউন হলে এই ক্রমে চেষ্টা হবে
FALLBACK_ORDER = ["openai", "anthropic", "google"]


#: Which provider a fallback lands on, and the preset that names its model.
#: Single source of truth: ``build_llm`` (voice) and ``credentials_for`` (text)
#: must agree, or the same tenant would get a different model depending on
#: whether they called or texted.
FALLBACK_PRESETS: dict[str, str] = {
    "openai": "fast",
    "anthropic": "natural",
    "google": "cheap",
}


def _api_key(provider: str) -> str:
    """The configured API key for a provider, or ``""`` when it is unset.

    Keys come from ``app.core.config.settings`` (environment / secret config)
    and from nowhere else: a tenant's row selects *which* provider to use, it
    can never supply *the credential* for one.

    Surrounding whitespace is stripped: a trailing newline pasted into a
    deployment's secret store is a missing key, not a credential, and it should
    fail as "not configured" rather than as an authentication error from the
    provider.
    """
    return ({
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
        "google": settings.google_api_key,
    }.get(provider, "") or "").strip()


def _has_key(provider: str) -> bool:
    return bool(_api_key(provider))


def llm_capabilities(provider: str) -> dict[str, bool]:
    """The capability contract for a provider; empty dict when unknown.

    An unknown provider has no capability entry, and callers treat "absent"
    the same as "cannot do it" — never the same as "can do it".
    """
    return LLM_CAPABILITIES.get(provider, {})


def build_llm(provider: str, model: str, temperature: float = 0.6, max_tokens: int = 120):
    """Pipecat LLM service বানায়। key না থাকলে নিজে থেকেই fallback নেয়।"""

    if provider not in SUPPORTED_PROVIDERS:
        # A provider we cannot construct is a configuration error in the
        # tenant's settings — never a silent fallback to an unrelated AI.
        raise UnsupportedProviderFeatureError(
            f"Unknown LLM provider {provider!r}; supported: "
            + ", ".join(SUPPORTED_PROVIDERS),
            provider=provider,
        )

    # Capability validation: the agent always registers its booking/
    # escalation tools, so a provider without tool-calling must fail here,
    # loudly, instead of silently dropping the tools and producing an agent
    # that cannot do its job.
    if not llm_capabilities(provider).get("supports_tool_calling", False):
        raise UnsupportedProviderFeatureError(
            f"LLM provider {provider!r} does not support tool calling",
            provider=provider,
        )

    if not _has_key(provider):
        for alt in FALLBACK_ORDER:
            if _has_key(alt):
                log.warning("llm.fallback", requested=provider, using=alt)
                provider, model = alt, PRESETS[FALLBACK_PRESETS[alt]].model
                break
        else:
            raise ProviderConfigurationError(
                "No LLM provider API key is configured",
                provider="llm",
            )

    # ---- OpenAI (ChatGPT) ------------------------------------------------
    if provider == "openai":
        from pipecat.services.openai.llm import OpenAILLMService

        return OpenAILLMService(
            api_key=settings.openai_api_key,
            model=model,
            params=OpenAILLMService.InputParams(
                temperature=temperature,
                max_tokens=max_tokens,
                # ফোনে লম্বা উত্তর = মৃত্যু। শক্ত করে বেঁধে দিলাম।
                frequency_penalty=0.3,
                presence_penalty=0.3,
            ),
        )

    # ---- Anthropic (Claude) ---------------------------------------------
    if provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        return AnthropicLLMService(
            api_key=settings.anthropic_api_key,
            model=model,
            params=AnthropicLLMService.InputParams(
                temperature=temperature,
                max_tokens=max_tokens,
            ),
        )

    # ---- Google (Gemini) -------------------------------------------------
    if provider == "google":
        from pipecat.services.google.llm import GoogleLLMService

        return GoogleLLMService(
            api_key=settings.google_api_key,
            model=model,
            params=GoogleLLMService.InputParams(
                temperature=temperature,
                max_tokens=max_tokens,
            ),
        )

    # Unreachable: the SUPPORTED_PROVIDERS guard above already rejected
    # anything else. Kept so the function is total.
    raise UnsupportedProviderFeatureError(
        f"Unknown LLM provider {provider!r}",
        provider=provider,
    )


def resolve(tenant) -> LLMChoice:
    """Tenant-এর সেটিং থেকে provider/model ঠিক করে।

    This returns **selection only**: which AI the tenant asked for. It
    deliberately carries no credential — see :func:`api_key_for` and
    :func:`select_for_use` for the credential half of the contract, and
    :func:`validate_llm_config` for the strict variant used when constructing
    an agent.
    """
    if tenant.llm_preset and tenant.llm_preset in PRESETS:
        return PRESETS[tenant.llm_preset]
    if tenant.llm_provider and tenant.llm_model:
        return LLMChoice(tenant.llm_provider, tenant.llm_model, 300, "custom")
    return PRESETS["natural"]      # ডিফল্ট: সবচেয়ে মানুষের মতো


# ------------------------------------------------------- credential contract ---
#
# ``LLMChoice`` answers "which AI did the tenant ask for?" and is safe to log,
# show in the dashboard and store on the tenant row. It must never grow an
# ``api_key`` field: the key is deployment configuration, not tenant data, and
# a selection object that carries one ends up in logs and API responses.
#
# The functions below are therefore the whole credential contract:
#
#   api_key_for(provider)      -> the configured key ("" when unset)
#   select_for_use(choice)     -> the choice to actually use, applying the
#                                 documented fallback (never raises for a missing
#                                 key; ProviderConfigurationError only when the
#                                 provider is unknown — existing voice behaviour)
#   validate_llm_config(choice)-> the same resolution, but a missing credential
#                                 is a ProviderConfigurationError instead of a
#                                 silent switch (for the request/response text
#                                 agent, where a silent switch would answer a
#                                 paying customer on a provider nobody chose)
#
# Both take an ``LLMChoice`` (or anything with ``provider``/``model``),
# because a plain object is what ``resolve()`` returns.

#: ``LLMChoice``-shaped things (``resolve()`` returns one; tests pass stubs).
LLMSelection = LLMChoice


def api_key_for(provider: str) -> str:
    """Alias kept for callers that want the credential half by name.

    Same source as :func:`_api_key`: ``app.core.config.settings``. Never logged,
    never returned in an API response, never read from a tenant row.
    """
    return _api_key(provider)


def _choice(provider: str, model: str) -> LLMChoice:
    """Preset entry for a provider, for the model used when falling back."""
    preset = PRESETS[FALLBACK_PRESETS[provider]]
    return LLMChoice(provider, model or preset.model, preset.est_latency_ms, preset.notes)


def select_for_use(selection) -> LLMChoice:
    """Which provider/model will actually be called — existing voice behaviour.

    * unknown provider -> :class:`UnsupportedProviderFeatureError` (never a
      silent fallback to an unrelated AI);
    * known provider with a key -> that provider, unchanged;
    * known provider without a key -> the first provider in
      :data:`FALLBACK_ORDER` that has one, logged as ``llm.fallback``;
    * nobody has a key -> :class:`ProviderConfigurationError`.

    This mirrors ``build_llm`` exactly (``build_llm`` is the pipecat service
    builder and cannot be reused for a plain HTTP completion), so the voice and
    text paths cannot drift apart.
    """
    provider = selection.provider
    if provider not in SUPPORTED_PROVIDERS:
        raise UnsupportedProviderFeatureError(
            f"Unknown LLM provider {provider!r}; supported: "
            + ", ".join(SUPPORTED_PROVIDERS),
            provider=provider,
        )
    if _has_key(provider):
        return _choice(provider, selection.model)
    for alt in FALLBACK_ORDER:
        if _has_key(alt):
            log.warning("llm.fallback", requested=provider, using=alt)
            return _choice(alt, "")
    raise ProviderConfigurationError(
        "No LLM provider API key is configured",
        provider="llm",
    )


def validate_llm_config(selection) -> LLMChoice:
    """The agent-construction contract: never a silent provider switch.

    Raises :class:`ProviderConfigurationError` when the selected provider has
    no key, instead of falling back. A text agent answers a paying customer
    directly and is constructed per message, so "no API key for the provider
    this tenant chose" is a configuration fault that has to be visible (it is
    logged with its category and counted by ``record_provider_error``), not a
    quiet switch to a different vendor's AI.
    """
    if selection.provider not in SUPPORTED_PROVIDERS:
        raise UnsupportedProviderFeatureError(
            f"Unknown LLM provider {selection.provider!r}; supported: "
            + ", ".join(SUPPORTED_PROVIDERS),
            provider=selection.provider,
        )
    if not _has_key(selection.provider):
        raise ProviderConfigurationError(
            f"LLM API key for provider {selection.provider!r} is not configured",
            provider=selection.provider,
        )
    return _choice(selection.provider, selection.model)
