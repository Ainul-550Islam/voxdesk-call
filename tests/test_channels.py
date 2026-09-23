"""SMS / WhatsApp channel logic: prefixes, opt-out keywords, reply shaping —
plus the webhook's agent-failure contract (Step 17, P0).

The delivery rules at the bottom of this file are the ones a customer feels:
whatever breaks behind the webhook, the person who texted the business still
gets an answer, and nothing about *why* it broke leaks into that answer.
"""
import json

import pytest
import structlog
from sqlalchemy import select

from app.agent.errors import ProviderConfigurationError, ProviderTimeoutError
from app.agent.text_agent import SMS_SAFE_LENGTH, channel_rules, shape_for_channel
from app.channels import messaging
from app.channels.messaging import (
    HELP_WORDS,
    START_WORDS,
    STOP_WORDS,
    detect_channel,
    help_text,
    normalize_keyword,
    opt_out_confirmation,
    strip_channel_prefix,
)
from app.core import metrics
from app.db.models import Call, Speaker, Turn
from tests.conftest import make_tenant

# ------------------------------------------------------------- addressing ---

def test_strip_whatsapp_prefix():
    assert strip_channel_prefix("whatsapp:+15551234567") == "+15551234567"


def test_strip_leaves_plain_sms_alone():
    assert strip_channel_prefix("+15551234567") == "+15551234567"


@pytest.mark.parametrize("addr,expected", [
    ("whatsapp:+15551234567", "whatsapp"),
    ("+15551234567", "sms"),
])
def test_detect_channel(addr, expected):
    assert detect_channel(addr) == expected


# ---------------------------------------------------------------- keywords ---

@pytest.mark.parametrize("raw", ["STOP", " stop ", "Stop!", "STOPALL", "unsubscribe"])
def test_stop_variants_are_recognised(raw):
    assert normalize_keyword(raw) in STOP_WORDS


def test_start_and_help_are_recognised():
    assert normalize_keyword("START") in START_WORDS
    assert normalize_keyword("Help") in HELP_WORDS


def test_normal_sentence_is_not_a_keyword():
    kw = normalize_keyword("Can I book for tomorrow at 3?")
    assert kw not in STOP_WORDS and kw not in HELP_WORDS


def test_stop_inside_a_sentence_is_not_an_opt_out():
    """'please stop by at 3' must NOT unsubscribe the customer."""
    assert normalize_keyword("please stop by at 3") not in STOP_WORDS


def test_opt_out_confirmation_tells_them_how_to_return():
    text = opt_out_confirmation("Bright Smile Dental")
    assert "Bright Smile Dental" in text and "START" in text


def test_help_text_includes_stop_instruction():
    assert "STOP" in help_text("Acme")


# ----------------------------------------------------------- reply shaping ---

def test_long_sms_is_trimmed_at_a_sentence_boundary():
    long_text = ("We are open nine to five. " * 30).strip()
    out = shape_for_channel(long_text, "sms")
    assert len(out) <= SMS_SAFE_LENGTH
    assert out.endswith(".")


def test_short_sms_is_untouched():
    assert shape_for_channel("Booked for 2 PM.", "sms") == "Booked for 2 PM."


def test_whatsapp_is_not_trimmed():
    long_text = "a" * 1000
    assert len(shape_for_channel(long_text, "whatsapp")) == 1000


def test_sms_rules_forbid_markdown_and_cap_length():
    rules = channel_rules("sms", "Acme")
    assert "300 characters" in rules and "No markdown" in rules


def test_whatsapp_rules_allow_bold_but_not_tables():
    rules = channel_rules("whatsapp", "Acme")
    assert "*bold*" in rules and "No headers, no tables" in rules

# ------------------------------------------- the agent-failure contract ---
#
# `inbound_message` is the one place where a customer is waiting at the other
# end, so two rules hold there:
#
#   1. the customer always gets a safe answer — never a 500, never a stack trace,
#      never the words "TypeError" or "rate limit";
#   2. the operator always gets enough truth to know what actually happened —
#      which means a *provider* failure (typed, counted, categorised) and a
#      *contract* failure (our bug: logged with its real exception type) must
#      not be flattened into the same log line.
#
# Before Step 17 the TextAgent constructor sat outside the try block, so a
# contract failure there escaped the handler entirely: Twilio saw a 500, the
# customer saw nothing, and the operator saw nothing either.

SAFE_APOLOGY = "Sorry, I'm having trouble right now."


class _LogRecorder:
    """A structlog stand-in that keeps whole records, contextvars included.

    The real pipeline merges the bound contextvars (``request_id``,
    ``tenant_id``) into every record, so the recorder does the same — otherwise
    a test could not tell whether correlation actually reached the log line.
    """

    def __init__(self):
        self.records: list[dict] = []

    def _capture(self, event, **kwargs):
        self.records.append({**structlog.contextvars.get_contextvars(), "event": event, **kwargs})

    def info(self, event, **kwargs):
        self._capture(event, **kwargs)

    def warning(self, event, **kwargs):
        self._capture(event, **kwargs)

    def error(self, event, **kwargs):
        self._capture(event, **kwargs)

    def debug(self, event, **kwargs):
        self._capture(event, **kwargs)

    def events(self) -> list[str]:
        return [record["event"] for record in self.records]

    def find(self, event) -> dict:
        for record in self.records:
            if record["event"] == event:
                return record
        raise AssertionError(f"{event} was never logged; got {self.events()}")

    def rendered(self) -> str:
        return json.dumps(self.records, default=str)


async def _inbound(client, db, *, body: str, number: str, sid: str = "SM-agent-1"):
    """Create the tenant for `number` and post one inbound text to the webhook."""
    tenant = await make_tenant(db, "Text Clinic")
    tenant.twilio_number = number
    await db.commit()
    response = await client.post(
        "/channels/message",
        data={"From": "+15559990000", "To": number, "Body": body, "MessageSid": sid},
    )
    return tenant, response


def _agent_that(fails_with: BaseException, monkeypatch, *, at_construction=False):
    """Point the webhook at an agent that fails the way a test needs it to."""
    if at_construction:
        def broken(tenant, handlers, channel):        # noqa: ARG001 - signature parity
            raise fails_with
        monkeypatch.setattr(messaging, "TextAgent", broken)
        return

    class BrokenAgent:
        def __init__(self, tenant, handlers, channel):   # noqa: ARG002 - parity
            pass

        async def reply(self, history, user_text):       # noqa: ARG002 - parity
            raise fails_with

    monkeypatch.setattr(messaging, "TextAgent", BrokenAgent)


async def test_a_construction_failure_answers_the_customer_instead_of_500(
    client, db, monkeypatch
):
    """The P0's second half: the constructor used to sit outside the guard."""
    _agent_that(ProviderConfigurationError("no key configured", provider="openai"),
                monkeypatch, at_construction=True)

    _tenant, response = await _inbound(client, db, body="hello",
                                       number="+15551234001")

    assert response.status_code == 200                      # not 500
    assert SAFE_APOLOGY in response.text
    assert "no key configured" not in response.text         # internals stay internal


async def test_a_contract_error_is_logged_as_our_bug_not_as_a_provider_error(
    client, db, monkeypatch
):
    recorder = _LogRecorder()
    monkeypatch.setattr(messaging, "log", recorder)
    _agent_that(TypeError("TextAgent() got an unexpected keyword argument"), monkeypatch)

    _tenant, response = await _inbound(client, db, body="hello",
                                       number="+15551234002")

    assert response.status_code == 200
    record = recorder.find("channel.agent_contract_error")
    assert record["error_type"] == "TypeError"              # the real culprit is named
    assert "unexpected keyword argument" in record["error"]
    assert "channel.agent_provider_error" not in recorder.events()


async def test_a_provider_error_is_categorised_logged_and_counted(
    client, db, monkeypatch
):
    recorder = _LogRecorder()
    monkeypatch.setattr(messaging, "log", recorder)
    counter = metrics.PROVIDER_ERRORS.labels(provider="openai", category="timeout")
    before = counter._value.get()
    _agent_that(ProviderTimeoutError("openai timed out", provider="openai"), monkeypatch)

    tenant, response = await _inbound(client, db, body="hello",
                                      number="+15551234003")

    assert response.status_code == 200
    assert SAFE_APOLOGY in response.text
    record = recorder.find("channel.agent_provider_error")
    assert record["provider"] == "openai"
    assert record["category"] == "timeout"
    assert record["retryable"] is True
    assert record["tenant_id"] == str(tenant.id)            # correlation reached the log
    assert record.get("request_id")                         # …and so did the request id
    assert counter._value.get() - before >= 1.0             # counted for the dashboard
    assert "channel.agent_contract_error" not in recorder.events()


async def test_neither_secrets_nor_the_customers_words_reach_the_logs(
    client, db, monkeypatch
):
    """The failure path is where logging is most tempting and most dangerous.

    The SDK's own message is the thing that can carry a key, so the provider
    error here is built the way production builds it — by classifying an SDK
    exception whose text contains a key *and* the customer's words. What reaches
    the log must be the fixed, useful part and nothing else.
    """
    from app.agent.errors import classify_provider_exception

    recorder = _LogRecorder()
    monkeypatch.setattr(messaging, "log", recorder)
    secret = "sk-live-sentinel-should-never-be-logged"
    customer_words = "my card number is 4111 1111 1111 1111"

    class _SdkError(Exception):
        status_code = 408

    sdk_error = _SdkError(
        f"openai said: invalid api key {secret} while handling {customer_words}"
    )
    _agent_that(classify_provider_exception(sdk_error, provider="openai"), monkeypatch)

    _tenant, response = await _inbound(client, db, body=customer_words,
                                       number="+15551234004")

    assert response.status_code == 200
    rendered = recorder.rendered()
    assert secret not in rendered
    assert "4111 1111 1111 1111" not in rendered            # the customer's words stay out
    assert "invalid api key" not in rendered                # the SDK's wording is dropped
    assert "openai timed out" in rendered                   # the useful part survives


async def test_the_failed_turn_is_still_saved_so_the_conversation_survives(
    client, db, monkeypatch
):
    _agent_that(ProviderTimeoutError("openai timed out", provider="openai"), monkeypatch)

    _tenant, response = await _inbound(client, db, body="are you open tomorrow?",
                                       number="+15551234005")
    assert response.status_code == 200

    thread = (await db.execute(
        select(Call).where(Call.intent == "chat:sms")
    )).scalars().first()
    assert thread is not None
    turns = (await db.execute(
        select(Turn).where(Turn.call_id == thread.id)
    )).scalars().all()
    assert [t.speaker for t in turns] == [Speaker.USER, Speaker.ASSISTANT]
    assert turns[0].text == "are you open tomorrow?"
    assert SAFE_APOLOGY in turns[1].text
    assert thread.llm_used is None                          # nothing invented about the model


async def test_production_construction_is_unaffected_by_the_logging_change(
    client, db, monkeypatch
):
    """One end-to-end pass with the real agent and a faked SDK boundary."""
    from app.agent import text_agent as text_agent_module
    from app.core.config import settings

    tenant = await make_tenant(db, "Text Clinic")
    tenant.twilio_number = "+15551234006"
    tenant.llm_preset = "fast"
    await db.commit()
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-sentinel")

    async def create(**kwargs):
        message = type("M", (), {
            "content": "We are open tomorrow from 9am.",
            "tool_calls": None,
            "model_dump": lambda self, exclude_none=False: {"role": "assistant",
                                                            "content": self.content},
        })()
        return type("R", (), {"choices": [type("C", (), {"message": message})()]})()

    client_double = type("Client", (), {
        "chat": type("Chat", (), {
            "completions": type("Completions", (), {
                "create": staticmethod(create),
            })(),
        })(),
    })()
    monkeypatch.setattr(text_agent_module, "_openai_style_client",
                        lambda provider, api_key: client_double)

    response = await client.post(
        "/channels/message",
        data={"From": "+15559990000", "To": "+15551234006", "Body": "open tomorrow?",
              "MessageSid": "SM-agent-happy"},
    )

    assert response.status_code == 200
    assert "We are open tomorrow from 9am." in response.text
    thread = (await db.execute(
        select(Call).where(Call.intent == "chat:sms")
    )).scalars().first()
    assert thread.llm_used == "openai/gpt-4o-mini"
