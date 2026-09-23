"""Batch 02 persistence — the registries' state survives a restart.

Batch 01 was explicit that the automation, notification and inbox services kept
their state in process-local dictionaries and that "a migration is reported at
the end of the batch". Batch 02 supplies both halves: the schema
(``alembic/versions/0012_enterprise_persistence.py``) and the scope that
hydrates/flushes those dictionaries around each request
(``app/services/enterprise_store.py``).

The tests below simulate a restart the honest way: they clear the *entire*
in-process state between requests (exactly what a new worker process starts
with) and then re-read through the API. Anything the second read returns must
have come back out of the database, because there is nowhere else for it to
come from.

They also pin the two properties that make the swap safe:

* the database is authoritative — a failed request writes nothing, and
* hydration is tenant-scoped — one tenant's rows never appear in another's view.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from app.domain.automation_models import TriggerEvent
from app.services import automation_service, inbox_service, notification_service
from app.services.enterprise_store import _ROWS
from tests.conftest import auth_headers


@pytest_asyncio.fixture
async def app_module_routes(app):
    """The real app's route table (mirrors the Batch 02 test module)."""
    return app.routes


def _forget_everything() -> None:
    """Wipe all in-process state: the post-restart starting position.

    This is deliberately harsher than a real restart (it drops every tenant, not
    just one) so a passing assertion cannot be explained by leftover globals.
    """
    automation_service._REGISTRY.clear()
    automation_service._RUNS.clear()
    automation_service._LAST_RUN_AT.clear()
    notification_service._TEMPLATES.clear()
    notification_service._NOTIFICATIONS.clear()
    notification_service._DEDUPE.clear()
    notification_service._READ.clear()
    inbox_service._OVERLAY.clear()
    _ROWS.clear()


@pytest.fixture(autouse=True)
def _isolated_registries():
    """Same isolation discipline as the Batch 02 suite: no cross-test tenants."""
    _forget_everything()
    yield
    _forget_everything()


def _automation_body(name: str = "Negative sentiment alert") -> dict:
    return {
        "name": name,
        "event": "call_completed",
        "description": "Escalate when a call ends badly",
        "filters": [{"field": "sentiment", "operator": "eq", "value": "negative"}],
        "actions": [
            {"name": "record_escalation_intent", "params": {"destination": "+15550009999"}}
        ],
        "max_per_event": 1,
        "cooldown_seconds": 0,
    }


def _template_body(channel: str = "in_app") -> dict:
    return {
        "name": "Booking confirmed",
        "channel": channel,
        "body": "Hi {customer}, your appointment {when} is confirmed.",
        "variables": ["customer", "when"],
    }


def _notification_body(template_id: str, business_key: str = "appt-42") -> dict:
    return {
        "template_id": template_id,
        "recipient": {"kind": "phone", "target": "+15551234567"},
        "event_source": "appointment_reminder",
        "business_key": business_key,
        "variables": {"customer": "Ada", "when": "Tuesday"},
    }


async def _open_thread(client, headers, *, channel: str = "web",
                       customer: str = "+15550001111") -> dict:
    response = await client.post(
        "/api/inbox/threads",
        json={"channel": channel, "customer": customer, "initial_message": "hello"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


# =================================================== the migration is present ===

class TestSchema:
    async def test_the_five_batch02_tables_exist_in_the_metadata(self):
        from app.db.models import Base

        for table in ("automations", "automation_runs", "notification_templates",
                      "notifications", "inbox_thread_states"):
            assert table in Base.metadata.tables, f"{table} has no model"

    async def test_the_revision_chains_off_the_previous_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(Config("alembic.ini"))
        # The chain is linear by design: one head, and each revision names its
        # predecessor. 0015 (refused machine credentials) is the current head;
        # the assertion is about the *shape* of the chain, so the names here
        # move together whenever a migration is added.
        heads = script.get_heads()
        assert heads == ["0015_credential_auth_rejected"]
        revision = script.get_revision("0015_credential_auth_rejected")
        assert revision.down_revision == "0014_identity_audit_actions"
        identity = script.get_revision("0013_enterprise_identity")
        assert identity.down_revision == "0012_enterprise_persistence"
        predecessor = script.get_revision("0012_enterprise_persistence")
        assert predecessor.down_revision == "0011_side_effect_exactly_once"


# ======================================================== automations, durable ===

class TestAutomationsSurviveRestart:
    async def test_definition_and_run_history_outlive_the_process(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        assert created.status_code == 201, created.text
        automation_id = created.json()["id"]
        assert (await client.post(
            f"/api/automations/{automation_id}/enable", headers=headers
        )).json()["status"] == "enabled"
        run = await client.post(
            f"/api/automations/{automation_id}/run",
            json={"business_event_id": "call-1", "payload": {"sentiment": "negative"}},
            headers=headers,
        )
        assert run.status_code in (200, 201), run.text

        # ---- restart ----
        _forget_everything()

        fetched = await client.get(f"/api/automations/{automation_id}", headers=headers)
        assert fetched.status_code == 200, fetched.text
        body = fetched.json()
        assert body["name"] == "Negative sentiment alert"
        assert body["status"] == "enabled"                    # not silently disabled
        assert body["filters"] == [
            {"field": "sentiment", "operator": "eq", "value": "negative"}
        ]
        assert body["actions"][0]["name"] == "record_escalation_intent"

        runs = await client.get(f"/api/automations/{automation_id}/runs", headers=headers)
        assert [r["business_event_id"] for r in runs.json()] == ["call-1"]

    async def test_evaluation_still_works_after_a_restart(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        automation_id = created.json()["id"]
        await client.post(f"/api/automations/{automation_id}/enable", headers=headers)

        _forget_everything()

        matched = await client.post(
            "/api/automations/evaluate",
            json={"event": "call_completed", "payload": {"sentiment": "negative"}},
            headers=headers,
        )
        assert [m["automation_id"] for m in matched.json()] == [automation_id]
        # And the filter is still the filter: a positive call does not match.
        missed = await client.post(
            "/api/automations/evaluate",
            json={"event": "call_completed", "payload": {"sentiment": "positive"}},
            headers=headers,
        )
        assert missed.json() == []

    async def test_create_is_still_idempotent_on_name_across_restarts(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        first = await client.post("/api/automations", json=_automation_body(), headers=headers)
        _forget_everything()
        second = await client.post("/api/automations", json=_automation_body(), headers=headers)
        assert second.status_code == 201, second.text
        assert second.json()["id"] == first.json()["id"]

        listed = await client.get("/api/automations", headers=headers)
        assert [a["id"] for a in listed.json()] == [first.json()["id"]]

    async def test_a_refused_create_writes_nothing(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        bad = {**_automation_body(), "event": "teleport_completed"}
        assert (await client.post("/api/automations", json=bad, headers=headers)).status_code == 422

        _forget_everything()                                    # nothing to recover from...
        assert (await client.get("/api/automations", headers=headers)).json() == []


# ====================================================== notifications, durable ===

class TestNotificationsSurviveRestart:
    async def test_template_and_notification_outlive_the_process(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        template = await client.post(
            "/api/notifications/templates", json=_template_body(), headers=headers
        )
        template_id = template.json()["id"]
        created = await client.post(
            "/api/notifications", json=_notification_body(template_id), headers=headers
        )
        assert created.status_code == 201, created.text
        notification_id = created.json()["id"]

        _forget_everything()

        listed = await client.get("/api/notifications", headers=headers)
        assert [n["id"] for n in listed.json()] == [notification_id]
        reopened = await client.get(f"/api/notifications/{notification_id}", headers=headers)
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["template_id"] == template_id
        assert reopened.json()["recipient"]["target_masked"] == "***4567"
        assert "+15551234567" not in reopened.text        # PII stays masked after a reload

    async def test_dedupe_and_read_state_survive_a_restart(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        template_id = (await client.post(
            "/api/notifications/templates", json=_template_body(), headers=headers
        )).json()["id"]
        body = _notification_body(template_id)
        first = await client.post("/api/notifications", json=body, headers=headers)
        notification_id = first.json()["id"]
        await client.post(f"/api/notifications/{notification_id}/read", headers=headers)

        _forget_everything()

        # The dedupe index is a database unique constraint, so a replayed create
        # after a restart still returns the same notification instead of a twin.
        second = await client.post("/api/notifications", json=body, headers=headers)
        assert second.status_code == 201, second.text
        assert second.json()["id"] == notification_id
        assert len((await client.get("/api/notifications", headers=headers)).json()) == 1

        unread = await client.get(
            "/api/notifications", params={"unread_only": True}, headers=headers
        )
        assert unread.json() == []                        # the read flag was persisted

        summary = await client.get("/api/notifications/states/summary", headers=headers)
        assert summary.status_code == 200
        assert summary.json()["pending"] == 1      # still awaiting delivery, counted once

    async def test_render_still_uses_the_single_brace_syntax_after_a_restart(
        self, client, manager_a
    ):
        headers = await auth_headers(client, manager_a)
        template_id = (await client.post(
            "/api/notifications/templates", json=_template_body(), headers=headers
        )).json()["id"]

        _forget_everything()

        rendered = await client.post(
            f"/api/notifications/templates/{template_id}/render",
            json={"variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=headers,
        )
        assert rendered.status_code == 200, rendered.text
        assert rendered.json()["body"] == "Hi Ada, your appointment Tuesday is confirmed."


# ============================================================= inbox, durable ===

class TestInboxSurvivesRestart:
    async def test_overlay_state_outlives_the_process(self, client, manager_a, agent_a):
        headers = await auth_headers(client, manager_a)
        thread = await _open_thread(client, headers)
        thread_id = thread["id"]
        await client.post(
            f"/api/inbox/threads/{thread_id}/assign",
            json={"assignee_id": "agent-7"}, headers=headers)
        await client.post(
            f"/api/inbox/threads/{thread_id}/priority",
            json={"priority": "high"}, headers=headers)
        await client.post(
            f"/api/inbox/threads/{thread_id}/tags",
            json={"tag": "billing"}, headers=headers)
        await client.post(
            f"/api/inbox/threads/{thread_id}/tags",
            json={"tag": "urgent"}, headers=headers)
        await client.post(
            f"/api/inbox/threads/{thread_id}/notes",
            json={"body": "internal hint"}, headers=headers)
        await client.post(f"/api/inbox/threads/{thread_id}/unread", headers=headers)

        _forget_everything()

        fetched = await client.get(f"/api/inbox/threads/{thread_id}", headers=headers)
        assert fetched.status_code == 200, fetched.text
        body = fetched.json()
        assert body["status"] == "assigned"               # derived from the assignee
        assert body["assignee_id"] == "agent-7"
        assert body["priority"] == "high"
        assert set(body["tags"]) == {"billing", "urgent"}
        assert body["internal_notes"] == ["internal hint"]
        assert body["unread_count"] == 1

        counts = await client.get("/api/inbox/unread-counts", headers=headers)
        assert counts.status_code == 200
        # One badge, still set: the endpoint keys by the internal call id (the
        # opaque thread id is a hash of it, deliberately not reversible), so the
        # assertion is on the badge set rather than on a guessable key.
        assert list(counts.json().values()) == [1]

    async def test_messages_are_still_ordered_and_notes_still_internal(
        self, client, manager_a
    ):
        headers = await auth_headers(client, manager_a)
        thread_id = (await _open_thread(client, headers))["id"]
        await client.post(
            f"/api/inbox/threads/{thread_id}/notes",
            json={"body": "internal hint"}, headers=headers)

        _forget_everything()

        messages = await client.get(
            f"/api/inbox/threads/{thread_id}/messages", headers=headers
        )
        assert messages.status_code == 200, messages.text
        assert [m["direction"] for m in messages.json()] == ["inbound", "internal_note"]

    async def test_escalation_and_reopen_still_follow_the_transition_table(
        self, client, manager_a
    ):
        headers = await auth_headers(client, manager_a)
        thread_id = (await _open_thread(client, headers))["id"]
        assert (await client.post(
            f"/api/inbox/threads/{thread_id}/escalate",
            json={"reason": "customer asked for a supervisor"}, headers=headers,
        )).json()["status"] == "escalated"

        _forget_everything()

        # ESCALATED -> CLOSED -> OPEN is legal; ESCALATED is still not a status
        # you can jump straight out of an arbitrary way.
        closed = await client.post(f"/api/inbox/threads/{thread_id}/close", headers=headers)
        assert closed.json()["status"] == "closed"
        reopened = await client.post(f"/api/inbox/threads/{thread_id}/reopen", headers=headers)
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["status"] == "open"


# ============================================================ tenant isolation ===

class TestHydrationIsTenantScoped:
    async def test_one_tenants_rows_never_appear_in_anothers_view(
        self, client, manager_a, owner_b
    ):
        headers_a = await auth_headers(client, manager_a)
        headers_b = await auth_headers(client, owner_b)

        created = await client.post("/api/automations", json=_automation_body(), headers=headers_a)
        automation_id = created.json()["id"]
        template_id = (await client.post(
            "/api/notifications/templates", json=_template_body(), headers=headers_a
        )).json()["id"]
        await client.post(
            "/api/notifications", json=_notification_body(template_id), headers=headers_a
        )
        thread_id = (await _open_thread(client, headers_a))["id"]

        _forget_everything()

        # Tenant B, hydrated from the same tables, sees its own (empty) rows.
        assert (await client.get("/api/automations", headers=headers_b)).json() == []
        assert (await client.get("/api/notifications", headers=headers_b)).json() == []
        assert (await client.get("/api/inbox/threads", headers=headers_b)).json() == []

        # A genuinely correct id from tenant A is still not found by tenant B.
        for path in (f"/api/automations/{automation_id}",
                     f"/api/notifications/{template_id}",
                     f"/api/inbox/threads/{thread_id}"):
            response = await client.get(path, headers=headers_b)
            assert response.status_code in (403, 404), (path, response.status_code)

        # Tenant A's own view is intact after B's reads.
        assert [a["id"] for a in (
            await client.get("/api/automations", headers=headers_a)
        ).json()] == [automation_id]


# ======================================================= the scope's own rules ===

class TestTenantScope:
    async def test_the_scope_clears_the_registry_when_the_handler_raises(self, db):
        """A failing operation must leave the process as it found it."""
        from app.services.enterprise_store import tenant_scope

        tenant_id = "00000000-0000-0000-0000-00000000c0de"
        automation_service._REGISTRY.setdefault(tenant_id, {})["leak"] = object()
        try:
            async with tenant_scope(db, tenant_id):
                automation_service._REGISTRY.setdefault(tenant_id, {})["leak"] = object()
                raise RuntimeError("handler failed")
        except RuntimeError:
            pass
        assert automation_service._REGISTRY.get(tenant_id, {}).get("leak") is None

    async def test_unknown_events_are_still_refused_by_the_domain(self):
        with pytest.raises(ValueError):
            TriggerEvent("teleport_completed")
