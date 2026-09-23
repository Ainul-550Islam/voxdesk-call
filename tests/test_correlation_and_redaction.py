"""Step 7 — correlation scoping and secret redaction in the log path."""
from __future__ import annotations

import structlog

from app.core.correlation import correlation_scope, current_correlation
from app.core.logging import _is_secret_key, _redact, redact_secrets


# ------------------------------------------------------------------ redaction ---

def test_secret_key_names_are_redacted():
    assert _is_secret_key("token")
    assert _is_secret_key("access_token")
    assert _is_secret_key("authorization")
    assert _is_secret_key("authorization_header")
    assert _is_secret_key("cookie")
    assert _is_secret_key("api_key")
    assert not _is_secret_key("llm_tokens")     # a count, not a secret
    assert not _is_secret_key("event")
    assert not _is_secret_key("request_id")


def test_redact_masks_key_values_recursively():
    record = {
        "event": "login",
        "token": "abc123",
        "password": "hunter2",
        "llm_tokens": 5,
        "nested": {"api_key": "sk-123", "ok": "fine"},
    }
    out = redact_secrets(None, "info", record)
    assert out["token"] == "***"
    assert out["password"] == "***"
    assert out["llm_tokens"] == 5            # counts survive
    assert out["nested"]["api_key"] == "***"
    assert out["nested"]["ok"] == "fine"


def test_redact_masks_configured_secret_values():
    # The default SECRET_KEY ("change-me") is a configured secret; a value
    # that contains it must be masked even under a non-obvious key name.
    assert _redact("configured with change-me inside") == "configured with *** inside"
    assert _redact("plain sentence") == "plain sentence"


def test_redact_passes_through_scalars():
    assert _redact(5) == 5
    assert _redact(True) is True
    assert _redact(None) is None
    assert _redact(3.14) == 3.14


def test_redact_handles_lists():
    assert _redact(["a", "change-me", 1]) == ["a", "***", 1]


# ------------------------------------------------------------------ correlation ---

def test_correlation_scope_binds_and_restores():
    ctx_before = dict(structlog.contextvars.get_contextvars())
    with correlation_scope(call_sid="CA-1", call_id="c-1", tenant_id="t-1"):
        bound = current_correlation()
        assert bound["call_sid"] == "CA-1"
        assert bound["call_id"] == "c-1"
        assert bound["tenant_id"] == "t-1"
    after = dict(structlog.contextvars.get_contextvars())
    assert after == ctx_before


def test_correlation_scope_ignores_none_values():
    with correlation_scope(call_sid=None, call_id="c-1"):
        bound = current_correlation()
        assert "call_sid" not in bound
        assert bound["call_id"] == "c-1"


def test_correlation_scope_truncates_long_values():
    with correlation_scope(call_sid="x" * 500):
        bound = current_correlation()
        assert len(bound["call_sid"]) == 128


def test_correlation_scope_with_no_fields_is_a_noop():
    ctx_before = dict(structlog.contextvars.get_contextvars())
    with correlation_scope():
        pass
    assert dict(structlog.contextvars.get_contextvars()) == ctx_before


# --------------------------------------------- the agent-failure log path ---
#
# Step 17 turned the SMS/WhatsApp agent's failure path into a structured one:
# `app.channels.messaging._log_agent_failure` splits "the provider is down"
# (typed, categorised, counted) from "our code is broken" (the real exception
# type, never re-labelled). These two tests pin the part that concerns this
# file: what is written, and that a secret reaching a value still gets masked.

def test_a_provider_failure_logs_the_safe_message_and_nothing_else():
    from structlog.testing import capture_logs

    from app.agent.errors import ProviderTimeoutError
    from app.channels import messaging

    error = ProviderTimeoutError(
        "openai timed out", provider="openai",
        detail={"exception_type": "APITimeoutError"},
    )

    with capture_logs() as captured:
        messaging._log_agent_failure(
            error, channel="sms", tenant_id="tenant-1", thread_id="thread-1"
        )

    record, = [r for r in captured if r["event"] == "channel.agent_provider_error"]
    assert record["error"] == error.safe_message      # never str(exc), never a payload
    assert record["provider"] == "openai"
    assert record["category"] == "timeout"
    assert record["retryable"] is True
    assert record["tenant_id"] == "tenant-1"
    assert record["thread_id"] == "thread-1"


def test_a_contract_failure_logs_its_real_type_and_is_not_a_provider_error():
    from structlog.testing import capture_logs

    from app.channels import messaging

    with capture_logs() as captured:
        messaging._log_agent_failure(
            TypeError("TextAgent() got an unexpected keyword argument 'api_key'"),
            channel="sms", tenant_id="tenant-1", thread_id="thread-1",
        )

    record, = [r for r in captured if r["event"] == "channel.agent_contract_error"]
    assert record["error_type"] == "TypeError"
    assert not any(r["event"] == "channel.agent_provider_error" for r in captured)


def test_a_secret_that_reaches_a_log_value_is_masked_by_the_pipeline():
    """Defence in depth: the handler logs safe text, and the pipeline masks the rest.

    ``change-me`` is the default ``SECRET_KEY`` this test process runs with, so it
    stands in for any configured secret value (see the redaction tests above).
    """
    import json

    record = redact_secrets(None, "error", {
        "event": "channel.agent_provider_error",
        "error": "provider said the key change-me is invalid",
        "detail": {"header": "Bearer change-me"},
    })

    assert "change-me" not in json.dumps(record)
