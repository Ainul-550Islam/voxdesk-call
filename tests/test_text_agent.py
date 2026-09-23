"""TextAgent — construction, provider selection, and the failure contract.

The agent is the one part of the LLM path no test touched before this file
existed: ``tests/test_channels.py`` exercises the SMS/WhatsApp helpers and
``tests/test_side_effect_exactly_once.py`` replaces the whole agent with a stub,
so a constructor that unpacked the wrong object survived a fully green suite and
would have crashed on the first real inbound text message.

The tests below start from the production contract — a real ``Tenant`` row, real
``settings``, the real ``resolve()``/``validate_llm_config()`` pair — and then
drive each failure the P0 report listed: missing credentials, an unknown
provider, a provider timeout, a provider API error, a malformed provider
response, and a bug in our own code (which must never be dressed up as a
provider outage).

Only the SDK boundary is ever faked (``openai.AsyncOpenAI`` /
``anthropic.AsyncAnthropic``, or the client factory that wraps them). The agent's
loop, tool plumbing, channel shaping and — importantly — its error
classification all run for real, so these tests fail if the classification moves
or disappears.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.agent import text_agent as text_agent_module
from app.agent.errors import (
    CATEGORIES,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderRuntimeError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    UnsupportedProviderFeatureError,
)
from app.agent.text_agent import MAX_TOOL_ROUNDS, SMS_SAFE_LENGTH, TextAgent
from app.core.config import settings
from tests.conftest import make_tenant

SENTINEL_KEY = "sk-test-sentinel-key-never-a-real-credential"
PROVIDER_KEYS = ("openai_api_key", "anthropic_api_key", "google_api_key")


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch):
    """Every test starts from "nothing is configured" and opts in explicitly.

    A developer's ``.env`` must not decide whether these tests pass, and the
    missing-credential cases need a genuinely empty configuration.
    """
    for name in PROVIDER_KEYS:
        monkeypatch.setattr(settings, name, "")
    yield


class FakeHandlers:
    """Stands in for ``FunctionHandlers``; records what the agent asked of it."""

    def __init__(self, results=None):
        self.calls: list[tuple[str, dict]] = []
        self._results = results or {}

    async def dispatch(self, name, args):
        self.calls.append((name, args))
        return self._results.get(name, {"ok": True})


def _tenant(**overrides):
    base = dict(
        id=uuid.uuid4(), name="Acme Dental", temperature=0.6,
        llm_preset=None, llm_provider=None, llm_model=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _SdkError(Exception):
    """A stand-in for a provider SDK exception (name and status are what matter)."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


# ------------------------------------------------------- SDK-shaped replies ---

def _openai_response(text: str, tool_calls: list[dict], *, arguments: str | None = None):
    """An object shaped like ``openai``'s chat completion response."""
    calls = [
        SimpleNamespace(
            id=call["id"],
            function=SimpleNamespace(
                name=call["name"],
                arguments=arguments if arguments is not None
                else json.dumps(call.get("args", {})),
            ),
        )
        for call in tool_calls
    ]
    message = SimpleNamespace(
        content=text,
        tool_calls=calls or None,
        model_dump=lambda exclude_none=False: {"role": "assistant", "content": text},
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _anthropic_response(text: str, tool_calls: list[dict]):
    """An object shaped like ``anthropic``'s messages response."""
    blocks = []
    if text:
        blocks.append(SimpleNamespace(type="text", text=text))
    for call in tool_calls:
        blocks.append(SimpleNamespace(type="tool_use", id=call["id"], name=call["name"],
                                      input=call.get("args", {})))
    return SimpleNamespace(content=blocks)


def _patch_openai_transport(monkeypatch, script, *, raw_arguments: str | None = None):
    """Fake only the OpenAI-compatible SDK; the agent's code runs for real.

    ``script`` is a list of either ``(text, tool_calls)`` replies or exceptions
    to raise — so a test can inject an SDK failure and still exercise the real
    classification inside ``_complete_openai``.
    """
    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        item = script.pop(0)
        if isinstance(item, BaseException):
            raise item
        text, tool_calls = item
        return _openai_response("" if text is None else text, tool_calls,
                                arguments=raw_arguments)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(text_agent_module, "_openai_style_client",
                        lambda provider, api_key: client)
    monkeypatch.setattr(text_agent_module, "build_system_prompt",
                        lambda tenant, provider=None: "SYSTEM")
    return calls


def _patch_anthropic_transport(monkeypatch, script):
    """Fake only ``anthropic.AsyncAnthropic``; everything else is the real code."""
    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        item = script.pop(0)
        if isinstance(item, BaseException):
            raise item
        text, tool_calls = item
        return _anthropic_response("" if text is None else text, tool_calls)

    factory = lambda *, api_key: SimpleNamespace(  # noqa: E731 - a one-line test double
        messages=SimpleNamespace(create=create)
    )
    monkeypatch.setattr("anthropic.AsyncAnthropic", factory)
    monkeypatch.setattr(text_agent_module, "build_system_prompt",
                        lambda tenant, provider=None: "SYSTEM")
    return calls


# ============================================================ construction ===

def test_construction_uses_the_selection_and_the_configured_key(monkeypatch):
    """The P0: this used to unpack a selection object that carries no key."""
    monkeypatch.setattr(settings, "anthropic_api_key", SENTINEL_KEY)
    agent = TextAgent(_tenant(), FakeHandlers(), channel="sms")

    assert agent.provider == "anthropic"           # the tenant's default preset
    assert agent.model == "claude-haiku-4-5"
    assert agent.api_key == SENTINEL_KEY           # from settings, never the tenant row
    assert agent.selection.provider == "anthropic"  # the selection is kept, unmodified


def test_construction_is_total_for_every_supported_provider(monkeypatch):
    for name in PROVIDER_KEYS:
        monkeypatch.setattr(settings, name, SENTINEL_KEY)
    for provider, model in (("openai", "gpt-4o"), ("anthropic", "claude-sonnet-4-5"),
                            ("google", "gemini-2.0-flash")):
        tenant = _tenant(llm_provider=provider, llm_model=model)
        agent = TextAgent(tenant, FakeHandlers())
        assert (agent.provider, agent.model) == (provider, model)


async def test_construction_against_the_real_tenant_model(db, monkeypatch):
    """The production contract end to end: a real Tenant row decides the model."""
    monkeypatch.setattr(settings, "openai_api_key", SENTINEL_KEY)
    tenant = await make_tenant(db, "Text Clinic")
    tenant.llm_preset = "fast"
    await db.commit()
    await db.refresh(tenant)

    agent = TextAgent(tenant, FakeHandlers(), channel="whatsapp")

    assert (agent.provider, agent.model) == ("openai", "gpt-4o-mini")
    prompt = agent.system_prompt()                 # the real prompt builder, not a stub
    assert "CHANNEL: WhatsApp" in prompt
    assert "Maximum 45 words" in prompt            # the text-channel cap is applied


def test_missing_credential_is_a_typed_configuration_error(monkeypatch):
    """A misconfigured tenant is reported as configuration, never as a TypeError."""
    with pytest.raises(ProviderConfigurationError) as caught:
        TextAgent(_tenant(llm_preset="fast"), FakeHandlers())

    error = caught.value
    assert error.category == "configuration_error"
    assert error.provider == "openai"
    assert error.retryable is False
    assert "API key" in error.safe_message
    assert not isinstance(error, TypeError)


def test_no_provider_configured_at_all_is_reported_per_provider(monkeypatch):
    with pytest.raises(ProviderConfigurationError) as caught:
        TextAgent(_tenant(llm_provider="google", llm_model="gemini-2.0-flash"),
                  FakeHandlers())
    assert caught.value.provider == "google"


def test_unknown_provider_is_refused_rather_than_silently_switched(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", SENTINEL_KEY)
    tenant = _tenant(llm_provider="banana", llm_model="m-1")
    with pytest.raises(UnsupportedProviderFeatureError) as caught:
        TextAgent(tenant, FakeHandlers())
    assert caught.value.provider == "banana"
    assert caught.value.category == "unsupported_feature"


def test_text_agent_refuses_where_the_voice_path_falls_back(monkeypatch):
    """The one deliberate difference, pinned.

    ``select_for_use`` (the voice path's rule) falls back to whoever has a key;
    ``TextAgent`` refuses, because a text agent answers a paying customer
    directly and a silent switch would answer on a vendor the tenant did not
    choose. Each rule matches the pre-existing behaviour of its own path.
    """
    from app.agent.llm_factory import resolve, select_for_use

    monkeypatch.setattr(settings, "openai_api_key", SENTINEL_KEY)
    tenant = _tenant(llm_preset="cheap")           # wants google, has no google key

    assert select_for_use(resolve(tenant)).provider == "openai"      # voice: falls back
    with pytest.raises(ProviderConfigurationError):                  # text: refuses
        TextAgent(tenant, FakeHandlers())


# ================================================================ reply() ===

async def test_successful_reply_keeps_the_documented_shape(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", SENTINEL_KEY)
    _patch_anthropic_transport(monkeypatch, [("Your appointment is at 3pm.", [])])
    agent = TextAgent(_tenant(), FakeHandlers())

    result = await agent.reply([], "hi")

    assert result["reply"] == "Your appointment is at 3pm."
    assert result["provider"] == "anthropic"
    assert result["model"] == "claude-haiku-4-5"
    assert result["tools_used"] == []


async def test_openai_path_is_used_for_openai_and_google(monkeypatch):
    monkeypatch.setattr(settings, "google_api_key", SENTINEL_KEY)
    calls = _patch_openai_transport(monkeypatch, [("Sure.", [])])
    agent = TextAgent(_tenant(llm_provider="google", llm_model="gemini-2.0-flash"),
                      FakeHandlers())

    await agent.reply([{"role": "user", "content": "earlier"}], "now")

    assert calls[0]["model"] == "gemini-2.0-flash"
    # The system prompt goes first, and the history is preserved in order.
    assert calls[0]["messages"][0]["role"] == "system"
    assert calls[0]["messages"][0]["content"].startswith("SYSTEM")
    assert calls[0]["messages"][1] == {"role": "user", "content": "earlier"}
    assert calls[0]["messages"][2] == {"role": "user", "content": "now"}


async def test_tool_calls_are_dispatched_and_reported(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", SENTINEL_KEY)
    handlers = FakeHandlers({"check_availability": {"slots": ["15:00"]}})
    calls = _patch_anthropic_transport(monkeypatch, [
        ("", [{"id": "tu-1", "name": "check_availability", "args": {"day": "Tue"}}]),
        ("Tuesday at 3pm works.", []),
    ])
    agent = TextAgent(_tenant(), handlers)

    result = await agent.reply([], "is Tuesday free?")

    assert handlers.calls == [("check_availability", {"day": "Tue"})]
    assert result["tools_used"] == ["check_availability"]
    assert result["reply"] == "Tuesday at 3pm works."
    # The tool result was fed back to the model before the second call.
    second_messages = calls[1]["messages"]
    assert any(block.get("type") == "tool_result" for block in second_messages[-1]["content"])


async def test_openai_tool_round_trip(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", SENTINEL_KEY)
    handlers = FakeHandlers({"check_availability": {"slots": ["15:00"]}})
    _patch_openai_transport(monkeypatch, [
        ("", [{"id": "call-1", "name": "check_availability", "args": {"day": "Tue"}}]),
        ("Tuesday at 3pm works.", []),
    ])
    agent = TextAgent(_tenant(llm_preset="fast"), handlers)

    result = await agent.reply([], "is Tuesday free?")

    assert handlers.calls == [("check_availability", {"day": "Tue"})]
    assert result["reply"] == "Tuesday at 3pm works."


async def test_a_runaway_tool_loop_is_capped(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", SENTINEL_KEY)
    _patch_anthropic_transport(monkeypatch, [
        ("", [{"id": f"tu-{i}", "name": "check_availability", "args": {}}])
        for i in range(MAX_TOOL_ROUNDS + 3)
    ])
    agent = TextAgent(_tenant(), FakeHandlers())

    result = await agent.reply([], "loop please")

    # The loop stops at the cap and hands off rather than calling the provider forever.
    assert result["reply"] == "Let me have someone get back to you on that."
    assert len(result["tools_used"]) == MAX_TOOL_ROUNDS


async def test_sms_reply_is_shaped_for_the_channel(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", SENTINEL_KEY)
    _patch_anthropic_transport(monkeypatch, [("First sentence. " * 40, [])])
    agent = TextAgent(_tenant(), FakeHandlers(), channel="sms")

    result = await agent.reply([], "hi")

    assert len(result["reply"]) <= SMS_SAFE_LENGTH


# ======================================================= provider failures ===

@pytest.mark.parametrize("raised,expected", [
    (_SdkError("boom"), ProviderRuntimeError),
    (TimeoutError("deadline exceeded"), ProviderTimeoutError),
    (_SdkError("slow", status_code=408), ProviderTimeoutError),
    (_SdkError("slow", status_code=429), ProviderRateLimitedError),
    (_SdkError("bad key", status_code=401), ProviderAuthenticationError),
    (_SdkError("nope", status_code=500), ProviderUnavailableError),
    (_SdkError("nope", status_code=503), ProviderUnavailableError),
])
async def test_provider_failures_are_typed(monkeypatch, raised, expected):
    monkeypatch.setattr(settings, "anthropic_api_key", SENTINEL_KEY)
    _patch_anthropic_transport(monkeypatch, [raised])
    agent = TextAgent(_tenant(), FakeHandlers())

    with pytest.raises(expected) as caught:
        await agent.reply([], "hi")

    assert caught.value.provider == "anthropic"
    assert caught.value.category in CATEGORIES


async def test_provider_failures_are_typed_on_the_openai_path_too(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", SENTINEL_KEY)
    _patch_openai_transport(monkeypatch, [_SdkError("slow", status_code=429)])
    agent = TextAgent(_tenant(llm_preset="fast"), FakeHandlers())

    with pytest.raises(ProviderRateLimitedError) as caught:
        await agent.reply([], "hi")
    assert caught.value.provider == "openai"
    assert caught.value.retryable is True


async def test_timeout_is_retryable_and_auth_failure_is_not(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", SENTINEL_KEY)
    _patch_anthropic_transport(monkeypatch, [TimeoutError("slow")])
    with pytest.raises(ProviderTimeoutError) as timed_out:
        await TextAgent(_tenant(), FakeHandlers()).reply([], "hi")
    assert timed_out.value.retryable is True

    _patch_anthropic_transport(monkeypatch, [_SdkError("bad key", status_code=401)])
    with pytest.raises(ProviderAuthenticationError) as rejected:
        await TextAgent(_tenant(), FakeHandlers()).reply([], "hi")
    assert rejected.value.retryable is False


async def test_the_original_exception_stays_in_the_chain(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", SENTINEL_KEY)
    original = _SdkError("upstream said no", status_code=429)
    _patch_anthropic_transport(monkeypatch, [original])

    with pytest.raises(ProviderError) as caught:
        await TextAgent(_tenant(), FakeHandlers()).reply([], "hi")

    assert caught.value.__cause__ is original


async def test_a_provider_error_never_carries_the_sdk_message_or_a_key(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", SENTINEL_KEY)
    leaky = _SdkError(
        f"401 invalid api key {SENTINEL_KEY} for request from +15551234567",
        status_code=401,
    )
    _patch_anthropic_transport(monkeypatch, [leaky])

    with pytest.raises(ProviderError) as caught:
        await TextAgent(_tenant(), FakeHandlers()).reply([], "hi")

    rendered = f"{caught.value} {caught.value.safe_message} {caught.value.detail}"
    assert SENTINEL_KEY not in rendered
    assert "+15551234567" not in rendered
    assert "invalid api key" not in rendered        # the SDK's own wording is dropped


# ------------------------------------------- our bugs are not provider bugs ---

async def test_a_malformed_provider_response_is_not_converted_into_a_provider_error(monkeypatch):
    """A response shape we cannot parse must surface as the bug it is, never as a
    retryable "provider error"."""
    monkeypatch.setattr(settings, "anthropic_api_key", SENTINEL_KEY)
    monkeypatch.setattr(text_agent_module, "build_system_prompt",
                        lambda tenant, provider=None: "SYSTEM")

    async def create(**kwargs):
        # No ``.type`` on the content block: not a shape the parser understands.
        return SimpleNamespace(content=[SimpleNamespace(nope=True)])

    factory = lambda *, api_key: SimpleNamespace(  # noqa: E731 - one-line test double
        messages=SimpleNamespace(create=create)
    )
    monkeypatch.setattr("anthropic.AsyncAnthropic", factory)
    agent = TextAgent(_tenant(), FakeHandlers())

    with pytest.raises(AttributeError) as caught:
        await agent.reply([], "hi")
    assert not isinstance(caught.value, ProviderError)


async def test_an_empty_choices_list_is_our_index_error(monkeypatch):
    """``choices[0]`` on an empty response is our IndexError, not a provider error."""
    monkeypatch.setattr(settings, "openai_api_key", SENTINEL_KEY)
    _patch_openai_transport(monkeypatch, [("unused", [])])
    real_complete = text_agent_module._complete_openai

    class _EmptyResponse:
        choices: list = []

    class _Client:
        class chat:                                    # noqa: N801 - SDK shape
            class completions:                         # noqa: N801 - SDK shape
                @staticmethod
                async def create(**kwargs):
                    return _EmptyResponse()

    with pytest.raises(IndexError) as caught:
        await real_complete(_Client(), "gpt-4o-mini", [], [], 0.6, provider="openai")
    assert not isinstance(caught.value, ProviderError)


async def test_invalid_tool_arguments_json_is_our_json_error(monkeypatch):
    """Unparsable tool arguments surface as a JSONDecodeError, not a provider error."""
    monkeypatch.setattr(settings, "openai_api_key", SENTINEL_KEY)
    _patch_openai_transport(
        monkeypatch,
        [("", [{"id": "call-1", "name": "check_availability", "args": {}}])],
        raw_arguments="{not json",
    )
    agent = TextAgent(_tenant(llm_preset="fast"), FakeHandlers())

    with pytest.raises(json.JSONDecodeError) as caught:
        await agent.reply([], "hi")
    assert not isinstance(caught.value, ProviderError)


def test_an_unrecognised_exception_keeps_its_identity_when_it_is_typed():
    """A non-provider exception is typed, but never promoted to an outage.

    A ``TypeError`` raised inside the transport call (a bad argument we passed,
    say) cannot be told apart from an SDK-side surprise. What matters is that the
    result is **not** one of the categories operators alert and retry on: it is
    ``provider_error``, it is not retryable, and ``detail`` still names the real
    exception type so the log cannot hide a contract break.
    """
    from app.agent.errors import classify_provider_exception

    classified = classify_provider_exception(
        TypeError("TextAgent() got an unexpected keyword argument"), provider="openai"
    )

    assert isinstance(classified, ProviderError)
    assert classified.category == "provider_error"
    assert classified.retryable is False
    assert classified.detail["exception_type"] == "TypeError"
    assert "TypeError" in str(classified)          # the real culprit is named…
    assert "unexpected keyword argument" not in str(classified)   # …not the raw text
    assert not isinstance(classified, (
        ProviderTimeoutError, ProviderRateLimitedError, ProviderAuthenticationError,
        ProviderUnavailableError,
    ))
