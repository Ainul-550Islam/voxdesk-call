"""Error paths that must stay VISIBLE (P0 hardening).

Every test here drives a real failure through a production helper and asserts
two things at once:

* the caller-visible contract is unchanged (False / None / fail-closed 429),
* the failure is observable — logged with a bounded, non-secret payload.

These are the handlers that used to swallow an exception with a bare ``pass``
or a bare ``return None``: the behaviour was defensible, the silence was not.
Each one now logs the exception TYPE only, never the driver's message, because
provider/driver text can carry credentials (a DSN with a password, a Redis URL,
an S3 response with a signed request).
"""
from __future__ import annotations

import contextlib
import logging

import structlog
from structlog.testing import capture_logs

from app.core import health
from app.core.cache import _RedisCache
from app.core.rate_limit import _RedisLimiter
from app.knowledge.storage.s3 import S3Storage
from app.release import ops


@contextlib.contextmanager
def _debug_log_level():
    """Reconfigure structlog at DEBUG for the duration of a block.

    The app's default level is ``settings.log_level`` (INFO), and operators
    raise it to DEBUG when they are diagnosing degradation. The per-operation
    cache handlers below log at that level on purpose, so the assertion has to
    run at the level the trace is meant to be read at.
    """
    previous = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        yield
    finally:
        structlog.configure(wrapper_class=previous)


# ------------------------------------------------------------------ readiness ---

async def test_a_database_probe_failure_is_logged_and_still_answers_false(monkeypatch):
    def boom():
        raise RuntimeError("could not connect to host=db password=hunter2")

    monkeypatch.setattr(health, "get_engine", boom)

    with capture_logs() as captured:
        ok = await health.check_database()

    assert ok is False                                    # contract unchanged
    record, = [r for r in captured if r["event"] == "health.database_check_failed"]
    assert record["error_type"] == "RuntimeError"
    assert "hunter2" not in str(record)                   # never the driver text


# ------------------------------------------------------------------ rate limit ---

async def test_a_broken_rate_limiter_fails_closed_and_names_the_reason(monkeypatch):
    limiter = _RedisLimiter("redis://localhost:6379/0")

    def boom():
        raise ConnectionError("redis://user:a-secret@localhost:6379/0 refused")

    monkeypatch.setattr(limiter, "_get", boom)

    with capture_logs() as captured:
        allowed = await limiter.allow("tenant:1", limit=10, window=60)

    assert allowed is False                               # fail CLOSED, unchanged
    record, = [r for r in captured if r["event"] == "rate_limit.backend_unavailable"]
    assert record["error_type"] == "ConnectionError"
    assert record["backend"] == "redis"
    assert "a-secret" not in str(record)


# ----------------------------------------------------------------------- cache ---

async def test_redis_cache_degradation_is_logged_as_a_degradation(monkeypatch):
    cache = _RedisCache("redis://localhost:6379/0")

    def boom():
        raise ConnectionError("no route to host")

    monkeypatch.setattr(cache, "_get", boom)

    # The probe is visible at the default level; the per-operation traces are
    # DEBUG (they would otherwise fire on every request of an outage).
    with capture_logs() as captured:
        assert await cache.ping() is False

    probed, = [r for r in captured if r["event"] == "cache.ping_failed"]
    assert probed["error_type"] == "ConnectionError"
    assert probed["log_level"] == "warning"

    with _debug_log_level(), capture_logs() as captured:
        assert await cache.get("tenant:1:key") is None
        assert await cache.set("tenant:1:key", {"v": 1}, 60) is None
        assert await cache.delete("tenant:1:key") is None

    events = {r["event"] for r in captured}
    assert {"cache.get_failed", "cache.set_failed", "cache.delete_failed"} <= events
    for record in captured:
        assert record["error_type"] == "ConnectionError"


# -------------------------------------------------------------- live-call usage ---

async def test_a_usage_counting_failure_still_lets_the_frame_through(monkeypatch):
    """The pass-through contract holds AND the dropped sample is traceable."""
    from pipecat.frames.frames import TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection

    from app.agent.usage_tracker import UsageTracker

    tracker = UsageTracker(track_stt=True)

    def boom(frame):
        raise ValueError("unexpected frame shape")

    monkeypatch.setattr(tracker, "_count_transcription", boom)

    pushed: list = []

    async def _push(frame, direction=None):
        pushed.append(frame)

    monkeypatch.setattr(tracker, "push_frame", _push)

    frame = TranscriptionFrame(
        text="my order never arrived", user_id="u1", timestamp="2026-01-01T00:00:00Z"
    )

    with capture_logs() as captured:
        await tracker.process_frame(frame, FrameDirection.DOWNSTREAM)

    assert pushed == [frame]                              # the call keeps flowing
    record, = [r for r in captured if r["event"] == "usage.frame_count_failed"]
    assert record["frame_type"] == "TranscriptionFrame"
    assert record["error_type"] == "ValueError"
    assert "order never arrived" not in str(record)       # no customer words


# -------------------------------------------------------------- object storage ---

async def test_a_denied_object_lookup_is_not_reported_as_a_missing_object(
    monkeypatch,
):
    storage = S3Storage(bucket="voxdesk-knowledge")

    class Denied(Exception):
        pass

    def boom():
        raise Denied("AccessDenied")

    monkeypatch.setattr(storage, "_get_client", boom)

    with capture_logs() as captured:
        assert await storage.exists("tenant-1/knowledge/doc.pdf") is False

    record, = [r for r in captured if r["event"] == "knowledge.s3_exists_failed"]
    assert record["error_type"] == "Denied"
    assert record["bucket"] == "voxdesk-knowledge"


# ------------------------------------------------------------ release evidence ---

def test_unreadable_certificate_sans_are_not_reported_as_a_hostname_mismatch():
    class NotACertificate:
        pass

    with capture_logs() as captured:
        matches = ops._hostname_matches("api.example.com", NotACertificate())

    assert matches is False                               # no evidence of a match
    record, = [r for r in captured if r["event"] == "release.tls_san_unreadable"]
    assert record["host"] == "api.example.com"
    assert record["error_type"] == "AttributeError"
