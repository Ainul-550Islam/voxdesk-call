"""তিনটা LLM provider বেছে নেওয়ার লজিক।"""
from types import SimpleNamespace

import pytest

from app.agent import llm_factory
from app.agent.errors import ProviderConfigurationError, UnsupportedProviderFeatureError
from app.agent.llm_factory import PRESETS, resolve


def _tenant(**kw):
    base = dict(llm_preset=None, llm_provider=None, llm_model=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_all_three_providers_have_presets():
    providers = {c.provider for c in PRESETS.values()}
    assert providers == {"openai", "anthropic", "google"}


def test_preset_fast_is_openai():
    assert PRESETS["fast"].provider == "openai"      # ChatGPT


def test_preset_natural_is_claude():
    assert PRESETS["natural"].provider == "anthropic"  # Claude


def test_preset_cheap_is_gemini():
    assert PRESETS["cheap"].provider == "google"     # Gemini


def test_resolve_uses_preset():
    assert resolve(_tenant(llm_preset="cheap")).provider == "google"


def test_resolve_uses_custom_pair():
    c = resolve(_tenant(llm_provider="openai", llm_model="gpt-4o"))
    assert (c.provider, c.model) == ("openai", "gpt-4o")


def test_resolve_defaults_to_natural():
    assert resolve(_tenant()).provider == "anthropic"


def test_unknown_preset_falls_back():
    assert resolve(_tenant(llm_preset="banana")).provider == "anthropic"


def test_build_llm_raises_without_any_key(monkeypatch):
    monkeypatch.setattr(llm_factory, "_has_key", lambda p: False)
    with pytest.raises(RuntimeError, match="API key"):
        llm_factory.build_llm("openai", "gpt-4o-mini")


def test_all_presets_are_low_latency():
    """ফোন কলে 600ms এর বেশি first-token = কল মরে যায়।"""
    for name, choice in PRESETS.items():
        if name == "smart":
            continue
        assert choice.est_latency_ms <= 350, f"{name} খুব ধীর"

# =============================================================================
# Step 17 (P0): the credential contract
# =============================================================================
#
# `resolve()` answers "which provider/model does this tenant want?" and answers
# only that: an LLMChoice carries no credential, by design — it is logged, shown
# in the dashboard and stored on the tenant row, and a key in a selection object
# ends up in all three. The credential is deployment configuration, read by the
# two functions below, which are the whole contract:
#
#   validate_llm_config(selection) -> the choice to use, or a typed
#                                     ProviderConfigurationError (text agent)
#   select_for_use(selection)      -> the choice to use, falling back to a
#                                     configured provider (the voice path's
#                                     existing rule)
#
# TextAgent used to unpack `resolve(tenant)` into three names, one of which
# (`api_key`) LLMChoice never had: every construction raised TypeError.

SENTINEL_KEY = "sk-test-sentinel-key-never-a-real-credential"
KEY_SETTINGS = ("openai_api_key", "anthropic_api_key", "google_api_key")


def _clear_keys(monkeypatch) -> None:
    for name in KEY_SETTINGS:
        monkeypatch.setattr(llm_factory.settings, name, "")


def _set_key(monkeypatch, name: str, value: str = SENTINEL_KEY) -> None:
    monkeypatch.setattr(llm_factory.settings, name, value)


def test_an_llm_choice_carries_no_credential():
    for choice in (*PRESETS.values(), resolve(_tenant())):
        assert not hasattr(choice, "api_key")
        assert set(vars(choice)) == {"provider", "model", "est_latency_ms", "notes"}


def test_api_key_for_reads_the_configured_key(monkeypatch):
    _set_key(monkeypatch, "openai_api_key")
    assert llm_factory.api_key_for("openai") == SENTINEL_KEY


def test_api_key_for_knows_nothing_about_unknown_providers(monkeypatch):
    _set_key(monkeypatch, "openai_api_key")
    assert llm_factory.api_key_for("banana") == ""
    assert llm_factory.api_key_for("") == ""


def test_a_whitespace_key_counts_as_unset(monkeypatch):
    """A stray space in a deployment's .env is a missing key, not a credential."""
    monkeypatch.setattr(llm_factory.settings, "anthropic_api_key", "   ")
    assert llm_factory.api_key_for("anthropic") == ""
    assert llm_factory._has_key("anthropic") is False


def test_validate_llm_config_raises_a_typed_configuration_error(monkeypatch):
    _clear_keys(monkeypatch)
    choice = resolve(_tenant(llm_preset="natural"))

    with pytest.raises(ProviderConfigurationError) as caught:
        llm_factory.validate_llm_config(choice)

    error = caught.value
    assert error.provider == "anthropic"
    assert error.category == "configuration_error"
    assert error.retryable is False
    assert "API key" in error.safe_message


def test_the_configuration_error_never_carries_another_providers_key(monkeypatch):
    _clear_keys(monkeypatch)
    _set_key(monkeypatch, "openai_api_key")

    with pytest.raises(ProviderConfigurationError) as caught:
        llm_factory.validate_llm_config(resolve(_tenant(llm_preset="natural")))

    assert SENTINEL_KEY not in str(caught.value)
    assert SENTINEL_KEY not in caught.value.safe_message


def test_validate_llm_config_returns_the_choice_to_use(monkeypatch):
    _set_key(monkeypatch, "google_api_key")
    resolved = llm_factory.validate_llm_config(
        resolve(_tenant(llm_provider="google", llm_model="gemini-2.0-flash"))
    )
    assert (resolved.provider, resolved.model) == ("google", "gemini-2.0-flash")
    assert not hasattr(resolved, "api_key")          # still selection-only


def test_validate_llm_config_refuses_an_unknown_provider(monkeypatch):
    _set_key(monkeypatch, "openai_api_key")
    unknown = llm_factory.LLMChoice("banana", "model", 1, "")

    with pytest.raises(UnsupportedProviderFeatureError) as caught:
        llm_factory.validate_llm_config(unknown)

    assert caught.value.provider == "banana"
    assert caught.value.category == "unsupported_feature"


def test_select_for_use_keeps_the_requested_provider_when_it_has_a_key(monkeypatch):
    _clear_keys(monkeypatch)
    _set_key(monkeypatch, "openai_api_key")

    choice = llm_factory.select_for_use(resolve(_tenant(llm_preset="fast")))

    assert (choice.provider, choice.model) == ("openai", "gpt-4o-mini")
    assert not hasattr(choice, "api_key")


def test_select_for_use_falls_back_to_a_configured_provider(monkeypatch):
    _clear_keys(monkeypatch)
    _set_key(monkeypatch, "google_api_key")

    choice = llm_factory.select_for_use(resolve(_tenant(llm_preset="fast")))   # wants openai

    assert (choice.provider, choice.model) == ("google", "gemini-2.0-flash")


def test_the_text_and_voice_fallbacks_agree(monkeypatch):
    """The same inputs must not produce different models on the two paths."""
    _clear_keys(monkeypatch)
    _set_key(monkeypatch, "anthropic_api_key")

    text_choice = llm_factory.select_for_use(resolve(_tenant(llm_preset="cheap")))
    voice_service = llm_factory.build_llm("google", "gemini-2.0-flash")

    assert text_choice.provider == "anthropic"
    assert text_choice.model == PRESETS[llm_factory.FALLBACK_PRESETS["anthropic"]].model
    assert voice_service.model_name == text_choice.model


def test_select_for_use_needs_at_least_one_key(monkeypatch):
    _clear_keys(monkeypatch)

    with pytest.raises(ProviderConfigurationError) as caught:
        llm_factory.select_for_use(resolve(_tenant()))

    assert caught.value.provider == "llm"


def test_select_for_use_refuses_an_unknown_provider(monkeypatch):
    _set_key(monkeypatch, "openai_api_key")
    unknown = llm_factory.LLMChoice("banana", "model", 1, "")

    with pytest.raises(UnsupportedProviderFeatureError) as caught:
        llm_factory.select_for_use(unknown)

    assert caught.value.provider == "banana"
