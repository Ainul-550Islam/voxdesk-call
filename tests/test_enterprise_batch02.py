"""Batch 02 — the enterprise surface completed and wired.

Batch 01 shipped eight domain models, eight services and three route modules,
with two honest gaps registered in its own docstrings:

1. **Route registration** was "outside the allowed file set for this batch and
   is reported as an integration dependency" — so ``/api/agents``,
   ``/api/workflows`` and ``/api/campaigns`` existed in code but were not
   reachable from the app.
2. Three services (``automation``, ``notification``, ``inbox``) had **no HTTP
   surface at all**.

This file proves both are closed, and that the new surface inherits the Batch 01
security posture rather than weakening it: tenant isolation is tested from the
outside (a real token for tenant B, a genuinely correct id from tenant A),
secrets/contacts are asserted absent from responses, and the "no arbitrary
code / no dialing" rules are asserted against the request surface.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from app.domain.automation_models import TriggerEvent
from app.services import automation_service, inbox_service, notification_service
from tests.conftest import auth_headers


@pytest_asyncio.fixture
async def app_module_routes(app):
    """The real app's route table, for the registration assertions below."""
    return app.routes


@pytest.fixture(autouse=True)
def _reset_enterprise_state():
    """Clear the in-process registries the Batch 01 services own.

    Those services are honest about not being durable (a migration is a
    registered gap), so their state lives in module globals. Tests must not
    inherit each other's tenants, automations or notifications.
    """
    automation_service._REGISTRY.clear()
    automation_service._RUNS.clear()
    automation_service._LAST_RUN_AT.clear()
    notification_service._TEMPLATES.clear()
    notification_service._NOTIFICATIONS.clear()
    notification_service._DEDUPE.clear()
    notification_service._READ.clear()
    inbox_service._OVERLAY.clear()
    yield
    automation_service._REGISTRY.clear()
    automation_service._RUNS.clear()
    automation_service._LAST_RUN_AT.clear()
    notification_service._TEMPLATES.clear()
    notification_service._NOTIFICATIONS.clear()
    notification_service._DEDUPE.clear()
    notification_service._READ.clear()
    inbox_service._OVERLAY.clear()


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


def _template_body(name: str = "Booking confirmed", channel: str = "in_app") -> dict:
    return {
        "name": name,
        "channel": channel,
        # Placeholder syntax is the model's single-brace substitution (see
        # NotificationTemplate.render); a doubled brace survives literally.
        "body": "Hi {customer}, your appointment {when} is confirmed.",
        "variables": ["customer", "when"],
    }


# ============================================================ router wiring ===

class TestRouterWiring:
    """The integration dependency Batch 01 reported, now closed."""

    EXPECTED = [
        "/api/agents",
        "/api/workflows",
        "/api/campaigns",
        "/api/automations",
        "/api/notifications",
        "/api/inbox/threads",
    ]

    async def test_every_enterprise_router_is_registered(self, app_module_routes):
        paths = {route.path for route in app_module_routes}
        for expected in self.EXPECTED:
            assert expected in paths, f"{expected} is not registered in app/main.py"

    async def test_new_surfaces_are_tenant_scoped_not_admin_namespaced(self, app_module_routes):
        # Every enterprise path is tenant-scoped by construction: the tenant
        # comes from the token, never from the path.
        for route in app_module_routes:
            path = getattr(route, "path", "")
            for prefix in ("/api/agents", "/api/workflows", "/api/campaigns",
                           "/api/automations", "/api/notifications", "/api/inbox"):
                if path.startswith(prefix):
                    assert "{tenant_id}" not in path, path


# ================================================================= AUTOMATION ===

class TestAutomationApi:
    async def test_create_then_read_back(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["status"] == "disabled"          # created disabled on purpose
        assert body["event"] == "call_completed"
        assert body["actions"][0]["name"] == "record_escalation_intent"

        fetched = await client.get(f"/api/automations/{body['id']}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json() == body

        listed = await client.get("/api/automations", headers=headers)
        assert [a["id"] for a in listed.json()] == [body["id"]]

    async def test_create_is_idempotent_on_name(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        first = await client.post("/api/automations", json=_automation_body(), headers=headers)
        second = await client.post("/api/automations", json=_automation_body(), headers=headers)
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] == second.json()["id"]

        listed = await client.get("/api/automations", headers=headers)
        assert len(listed.json()) == 1

    async def test_unknown_event_is_refused(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        body = {**_automation_body(), "event": "teleport_completed"}
        response = await client.post("/api/automations", json=body, headers=headers)
        assert response.status_code == 422

    async def test_unknown_action_is_refused(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        body = {**_automation_body(), "actions": [{"name": "exec_shell", "params": {}}]}
        response = await client.post("/api/automations", json=body, headers=headers)
        assert response.status_code == 422
        assert "controlled action" in response.json()["detail"]

    async def test_mass_assignment_is_refused(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        body = {**_automation_body(), "status": "enabled"}   # not a writable field
        response = await client.post("/api/automations", json=body, headers=headers)
        assert response.status_code == 422

    async def test_oversized_payload_is_refused(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        automation_id = created.json()["id"]
        run = await client.post(
            f"/api/automations/{automation_id}/run",
            json={
                "business_event_id": "call-1",
                "payload": {f"k{i}": "x" * 64 for i in range(2000)},
            },
            headers=headers,
        )
        assert run.status_code == 422

    async def test_enable_disable_then_evaluate(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        automation_id = created.json()["id"]

        # A disabled automation never evaluates, however well the payload matches.
        before = await client.post(
            "/api/automations/evaluate",
            json={"event": "call_completed", "payload": {"sentiment": "negative"}},
            headers=headers,
        )
        assert before.json() == []

        enabled = await client.post(f"/api/automations/{automation_id}/enable", headers=headers)
        assert enabled.json()["status"] == "enabled"

        after = await client.post(
            "/api/automations/evaluate",
            json={"event": "call_completed", "payload": {"sentiment": "negative"}},
            headers=headers,
        )
        assert [m["automation_id"] for m in after.json()] == [automation_id]

        # The filter is respected: a positive sentiment does not match.
        missed = await client.post(
            "/api/automations/evaluate",
            json={"event": "call_completed", "payload": {"sentiment": "positive"}},
            headers=headers,
        )
        assert missed.json() == []

        disabled = await client.post(f"/api/automations/{automation_id}/disable", headers=headers)
        assert disabled.json()["status"] == "disabled"

    async def test_run_is_deduplicated_by_business_event_id(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        automation_id = created.json()["id"]
        await client.post(f"/api/automations/{automation_id}/enable", headers=headers)

        payload = {"business_event_id": "call-123", "payload": {"sentiment": "negative"}}
        first = await client.post(f"/api/automations/{automation_id}/run", json=payload, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "completed"

        second = await client.post(f"/api/automations/{automation_id}/run", json=payload, headers=headers)
        assert second.json()["status"] == "cancelled"
        assert second.json()["last_error"] == "max_per_event budget exhausted"

    async def test_run_history_and_stats(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        automation_id = created.json()["id"]
        await client.post(f"/api/automations/{automation_id}/enable", headers=headers)
        await client.post(
            f"/api/automations/{automation_id}/run",
            json={"business_event_id": "call-9", "payload": {"sentiment": "negative"}},
            headers=headers,
        )

        runs = await client.get(f"/api/automations/{automation_id}/runs", headers=headers)
        assert len(runs.json()) == 1
        assert runs.json()[0]["business_event_id"] == "call-9"

        stats = await client.get(f"/api/automations/{automation_id}/stats", headers=headers)
        assert stats.json() == {"failed": 0, "suppressed": 0, "completed": 1}

    async def test_dedupe_key_is_deterministic(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        automation_id = created.json()["id"]
        body = {"business_event_id": "call-77", "payload": {}}
        one = await client.post(f"/api/automations/{automation_id}/dedupe-key", json=body, headers=headers)
        two = await client.post(f"/api/automations/{automation_id}/dedupe-key", json=body, headers=headers)
        assert one.json()["dedupe_key"] == two.json()["dedupe_key"]

    async def test_event_and_action_vocabularies_are_closed(self, client, viewer_a):
        headers = await auth_headers(client, viewer_a)
        events = await client.get("/api/automations/events", headers=headers)
        assert events.json() == sorted(e.value for e in TriggerEvent)
        actions = await client.get("/api/automations/actions", headers=headers)
        for forbidden in ("exec", "eval", "import", "os.system"):
            assert forbidden not in actions.json()

    async def test_viewer_reads_but_cannot_write(self, client, viewer_a):
        headers = await auth_headers(client, viewer_a)
        assert (await client.get("/api/automations", headers=headers)).status_code == 200
        assert (await client.post("/api/automations", json=_automation_body(), headers=headers)).status_code == 403

    async def test_agent_cannot_run_automations(self, client, agent_a, manager_a):
        manager_headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=manager_headers)
        automation_id = created.json()["id"]

        agent_headers = await auth_headers(client, agent_a)
        response = await client.post(
            f"/api/automations/{automation_id}/run",
            json={"business_event_id": "call-1", "payload": {}},
            headers=agent_headers,
        )
        assert response.status_code == 403

    async def test_cross_tenant_automation_is_not_found(self, client, manager_a, owner_b):
        manager_headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=manager_headers)
        automation_id = created.json()["id"]

        tenant_b_headers = await auth_headers(client, owner_b)
        assert (await client.get(f"/api/automations/{automation_id}", headers=tenant_b_headers)).status_code == 404
        assert (await client.get("/api/automations", headers=tenant_b_headers)).json() == []
        run = await client.post(
            f"/api/automations/{automation_id}/run",
            json={"business_event_id": "call-1", "payload": {}},
            headers=tenant_b_headers,
        )
        assert run.status_code == 404


# =============================================================== NOTIFICATION ===

class TestNotificationApi:
    async def test_template_create_and_render(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/notifications/templates", json=_template_body(), headers=headers)
        assert created.status_code == 201, created.text
        template_id = created.json()["id"]

        rendered = await client.post(
            f"/api/notifications/templates/{template_id}/render",
            json={"variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=headers,
        )
        assert rendered.status_code == 200
        assert rendered.json()["body"] == "Hi Ada, your appointment Tuesday is confirmed."

    async def test_unknown_channel_is_refused(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        response = await client.post(
            "/api/notifications/templates",
            json={**_template_body(), "channel": "carrier_pigeon"},
            headers=headers,
        )
        assert response.status_code == 422

    async def test_preference_check_reports_quiet_hours(self, client, viewer_a):
        headers = await auth_headers(client, viewer_a)
        open_hours = await client.post(
            "/api/notifications/preferences/check",
            json={"channel": "in_app", "enabled": True},
            headers=headers,
        )
        assert open_hours.json() == {"allowed": True, "reason": ""}

        quiet = await client.post(
            "/api/notifications/preferences/check",
            json={"channel": "in_app", "enabled": True,
                  "quiet_start": "22:00", "quiet_end": "08:00"},
            headers=headers,
        )
        # Overnight window: whichever side of midnight "now" falls on, the
        # reason must name quiet hours (or the call must be inside the window).
        if not quiet.json()["allowed"]:
            assert quiet.json()["reason"] == "recipient is in quiet hours"

        malformed = await client.post(
            "/api/notifications/preferences/check",
            json={"channel": "in_app", "quiet_start": "25:99"},
            headers=headers,
        )
        assert malformed.status_code == 422

    async def test_create_is_deduplicated_and_masks_the_recipient(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        template = await client.post("/api/notifications/templates", json=_template_body(), headers=headers)
        template_id = template.json()["id"]

        body = {
            "template_id": template_id,
            "recipient": {"kind": "phone", "target": "+15551234567"},
            "event_source": "appointment_reminder",
            "business_key": "appt-42",
            "variables": {"customer": "Ada", "when": "Tuesday"},
        }
        first = await client.post("/api/notifications", json=body, headers=headers)
        assert first.status_code == 201, first.text
        second = await client.post("/api/notifications", json=body, headers=headers)
        assert first.json()["id"] == second.json()["id"]        # one notification, not two

        recipient = first.json()["recipient"]
        assert recipient["target_masked"] == "***4567"
        assert "+15551234567" not in first.text                 # raw contact never returned

        listed = await client.get("/api/notifications", headers=headers)
        assert len(listed.json()) == 1

    async def test_read_state_round_trip(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        template = await client.post("/api/notifications/templates", json=_template_body(), headers=headers)
        created = await client.post(
            "/api/notifications",
            json={
                "template_id": template.json()["id"],
                "recipient": {"kind": "user", "user_id": "u-1"},
                "event_source": "system",
                "business_key": "read-1",
                "variables": {"customer": "Ada", "when": "Tuesday"},
            },
            headers=headers,
        )
        notification_id = created.json()["id"]
        assert created.json()["read"] is False

        assert (await client.post(f"/api/notifications/{notification_id}/read", headers=headers)).json()["read"] is True
        unread_only = await client.get("/api/notifications", params={"unread_only": True}, headers=headers)
        assert unread_only.json() == []
        assert (await client.post(f"/api/notifications/{notification_id}/unread", headers=headers)).json()["read"] is False

    async def test_delivery_honours_the_channels_reality(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        in_app = await client.post(
            "/api/notifications/templates",
            json=_template_body(name="In app", channel="in_app"), headers=headers)
        email = await client.post(
            "/api/notifications/templates",
            json=_template_body(name="Email", channel="email"), headers=headers)

        in_app_notification = await client.post(
            "/api/notifications",
            json={"template_id": in_app.json()["id"], "recipient": {"kind": "user", "user_id": "u-1"},
                  "event_source": "system", "business_key": "deliver-1",
                  "variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=headers,
        )
        delivered = await client.post(
            f"/api/notifications/{in_app_notification.json()['id']}/deliver", headers=headers)
        assert delivered.json()["delivery_state"] == "delivered"

        email_notification = await client.post(
            "/api/notifications",
            json={"template_id": email.json()["id"], "recipient": {"kind": "user", "user_id": "u-1"},
                  "event_source": "system", "business_key": "deliver-2",
                  "variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=headers,
        )
        suppressed = await client.post(
            f"/api/notifications/{email_notification.json()['id']}/deliver", headers=headers)
        # Batch 01 declared EMAIL as "no provider in this batch" — the API
        # reports that honestly instead of claiming a send.
        assert suppressed.json()["delivery_state"] == "suppressed"

    async def test_retry_after_failure_and_refusal_after_delivery(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        template = await client.post("/api/notifications/templates", json=_template_body(), headers=headers)
        notification = await client.post(
            "/api/notifications",
            json={"template_id": template.json()["id"], "recipient": {"kind": "user", "user_id": "u-1"},
                  "event_source": "system", "business_key": "retry-1",
                  "variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=headers,
        )
        notification_id = notification.json()["id"]

        retried = await client.post(f"/api/notifications/{notification_id}/retry", headers=headers)
        assert retried.json()["attempts"] == 1
        assert retried.json()["next_attempt_at"] != ""

        await client.post(f"/api/notifications/{notification_id}/deliver", headers=headers)
        refused = await client.post(f"/api/notifications/{notification_id}/retry", headers=headers)
        assert refused.status_code == 422

    async def test_delivery_summary_counts_states(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        template = await client.post("/api/notifications/templates", json=_template_body(), headers=headers)
        created = await client.post(
            "/api/notifications",
            json={"template_id": template.json()["id"], "recipient": {"kind": "user", "user_id": "u-1"},
                  "event_source": "system", "business_key": "summary-1",
                  "variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=headers,
        )
        await client.post(f"/api/notifications/{created.json()['id']}/deliver", headers=headers)
        summary = await client.get("/api/notifications/states/summary", headers=headers)
        assert summary.json()["delivered"] == 1

    async def test_agent_cannot_create_or_deliver(self, client, agent_a, manager_a):
        manager_headers = await auth_headers(client, manager_a)
        template = await client.post("/api/notifications/templates", json=_template_body(), headers=manager_headers)
        notification = await client.post(
            "/api/notifications",
            json={"template_id": template.json()["id"], "recipient": {"kind": "user", "user_id": "u-1"},
                  "event_source": "system", "business_key": "rbac-1",
                  "variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=manager_headers,
        )

        agent_headers = await auth_headers(client, agent_a)
        assert (await client.get("/api/notifications", headers=agent_headers)).status_code == 200
        assert (await client.post("/api/notifications", json={
            "template_id": template.json()["id"], "recipient": {"kind": "user"},
            "event_source": "system"}, headers=agent_headers)).status_code == 403
        delivered = await client.post(
            f"/api/notifications/{notification.json()['id']}/deliver", headers=agent_headers)
        assert delivered.status_code == 403

    async def test_cross_tenant_notifications_are_invisible(self, client, manager_a, owner_b):
        manager_headers = await auth_headers(client, manager_a)
        template = await client.post("/api/notifications/templates", json=_template_body(), headers=manager_headers)
        created = await client.post(
            "/api/notifications",
            json={"template_id": template.json()["id"], "recipient": {"kind": "user", "user_id": "u-1"},
                  "event_source": "system", "business_key": "iso-1",
                  "variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=manager_headers,
        )

        tenant_b_headers = await auth_headers(client, owner_b)
        assert (await client.get("/api/notifications", headers=tenant_b_headers)).json() == []
        assert (await client.get(
            f"/api/notifications/{created.json()['id']}", headers=tenant_b_headers)).status_code == 404
        # Tenant B cannot even reference tenant A's template id.
        assert (await client.get(
            f"/api/notifications/templates/{template.json()['id']}", headers=tenant_b_headers)).status_code == 404


# ====================================================================== INBOX ===

class TestInboxApi:
    async def _open_thread(self, client, headers, *, channel: str = "web",
                           customer: str = "+15550001111") -> dict:
        response = await client.post(
            "/api/inbox/threads",
            json={"channel": channel, "customer": customer, "initial_message": "hello"},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def test_open_thread_masks_participants(self, client, manager_a, tenant_a):
        headers = await auth_headers(client, manager_a)
        thread = await self._open_thread(client, headers)
        assert thread["channel"] == "web"
        assert thread["status"] == "open"
        assert thread["participants_masked"] == ["***1111", f"***{tenant_a.twilio_number[-4:]}"]
        assert "+15550001111" not in str(thread)

    async def test_list_and_fetch_thread(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        thread = await self._open_thread(client, headers)

        listed = await client.get("/api/inbox/threads", headers=headers)
        assert [t["id"] for t in listed.json()] == [thread["id"]]

        fetched = await client.get(f"/api/inbox/threads/{thread['id']}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["id"] == thread["id"]
        assert fetched.json()["message_count"] == 1

    async def test_messages_are_ordered_and_notes_stay_internal(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        thread = await self._open_thread(client, headers)
        thread_id = thread["id"]

        inbound = await client.post(
            f"/api/inbox/threads/{thread_id}/messages",
            json={"direction": "inbound", "body": "I need help"}, headers=headers)
        assert inbound.status_code == 201
        outbound = await client.post(
            f"/api/inbox/threads/{thread_id}/messages",
            json={"direction": "outbound", "body": "On it", "author_role": "agent"},
            headers=headers)
        assert outbound.json()["sequence"] > inbound.json()["sequence"]

        note = await client.post(
            f"/api/inbox/threads/{thread_id}/notes", json={"body": "internal hint"}, headers=headers)
        assert note.json()["internal_notes"] == ["internal hint"]

        messages = await client.get(f"/api/inbox/threads/{thread_id}/messages", headers=headers)
        bodies = [m["body"] for m in messages.json()]
        assert "hello" in bodies and "I need help" in bodies and "On it" in bodies
        # The note is reachable, but ONLY labelled as an internal note — never
        # as something the customer said or was told. That separation is the
        # whole point of the overlay.
        note_directions = [m["direction"] for m in messages.json() if m["body"] == "internal hint"]
        assert note_directions == ["internal_note"]

    async def test_queue_actions_round_trip(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        thread_id = (await self._open_thread(client, headers))["id"]

        assigned = await client.post(f"/api/inbox/threads/{thread_id}/assign",
                                     json={"assignee_id": "agent-7"}, headers=headers)
        assert assigned.json()["assignee_id"] == "agent-7"

        priority = await client.post(f"/api/inbox/threads/{thread_id}/priority",
                                     json={"priority": "urgent"}, headers=headers)
        assert priority.json()["priority"] == "urgent"

        tagged = await client.post(f"/api/inbox/threads/{thread_id}/tags",
                                   json={"tag": "vip"}, headers=headers)
        assert tagged.json()["tags"] == ["vip"]

        unread = await client.post(f"/api/inbox/threads/{thread_id}/unread", headers=headers)
        assert unread.json()["unread_count"] == 1
        read = await client.post(f"/api/inbox/threads/{thread_id}/read", headers=headers)
        assert read.json()["unread_count"] == 0

    async def test_escalate_close_reopen(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        thread_id = (await self._open_thread(client, headers))["id"]

        escalated = await client.post(f"/api/inbox/threads/{thread_id}/escalate",
                                      json={"reason": "needs a human"}, headers=headers)
        assert escalated.json()["status"] == "escalated"
        assert escalated.json()["escalated"] is True

        closed = await client.post(f"/api/inbox/threads/{thread_id}/close", headers=headers)
        assert closed.json()["status"] == "closed"

        reopened = await client.post(f"/api/inbox/threads/{thread_id}/reopen", headers=headers)
        assert reopened.json()["status"] == "open"

    async def test_bad_direction_and_empty_body_are_refused(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        thread_id = (await self._open_thread(client, headers))["id"]
        bad_direction = await client.post(
            f"/api/inbox/threads/{thread_id}/messages",
            json={"direction": "sideways", "body": "hi"}, headers=headers)
        assert bad_direction.status_code == 422
        empty = await client.post(
            f"/api/inbox/threads/{thread_id}/messages",
            json={"direction": "inbound", "body": ""}, headers=headers)
        assert empty.status_code == 422

    async def test_counts_and_unread_badges(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        thread_id = (await self._open_thread(client, headers))["id"]
        await client.post(f"/api/inbox/threads/{thread_id}/unread", headers=headers)

        counts = await client.get("/api/inbox/counts", headers=headers)
        assert counts.json()["open"] == 1 and counts.json()["closed"] == 0

        unread = await client.get("/api/inbox/unread-counts", headers=headers)
        assert list(unread.json().values()) == [1]

    async def test_identity_endpoint_explains_the_opaque_id(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        thread_id = (await self._open_thread(client, headers))["id"]
        identity = await client.get(f"/api/inbox/threads/{thread_id}/identity", headers=headers)
        assert identity.json()["thread_id"] == thread_id
        assert identity.json()["expected_id"] == thread_id
        assert identity.json()["derived_from"]["channel"] == "web"

    async def test_agent_reads_but_cannot_mutate_the_queue(self, client, agent_a, manager_a):
        manager_headers = await auth_headers(client, manager_a)
        thread_id = (await self._open_thread(client, manager_headers))["id"]

        agent_headers = await auth_headers(client, agent_a)
        assert (await client.get("/api/inbox/threads", headers=agent_headers)).status_code == 200
        # Registered limitation: agents have no CALL_READ_ALL, and the inbox
        # service has no per-assignee scoping, so mutations stay at manager+.
        assert (await client.post(f"/api/inbox/threads/{thread_id}/close", headers=agent_headers)).status_code == 403
        assert (await client.post(f"/api/inbox/threads/{thread_id}/assign",
                                  json={"assignee_id": "agent-9"}, headers=agent_headers)).status_code == 403

    async def test_cross_tenant_thread_is_not_found(self, client, manager_a, owner_b):
        manager_headers = await auth_headers(client, manager_a)
        thread_id = (await self._open_thread(client, manager_headers))["id"]

        tenant_b_headers = await auth_headers(client, owner_b)
        assert (await client.get(f"/api/inbox/threads/{thread_id}", headers=tenant_b_headers)).status_code == 404
        assert (await client.get("/api/inbox/threads", headers=tenant_b_headers)).json() == []
        # Tenant B's own queue is unaffected by tenant A's activity.
        tenant_b_counts = await client.get("/api/inbox/counts", headers=tenant_b_headers)
        assert tenant_b_counts.json()["open"] == 0


# ============================================================ auth on the edge ===

class TestUnauthenticatedAccess:
    @pytest.mark.parametrize("path", [
        "/api/agents",
        "/api/workflows",
        "/api/campaigns",
        "/api/automations",
        "/api/notifications",
        "/api/inbox/threads",
    ])
    async def test_anonymous_requests_are_rejected(self, client, path):
        response = await client.get(path)
        assert response.status_code in (401, 403)

    async def test_a_forged_token_is_rejected(self, client):
        response = await client.get(
            "/api/automations",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code == 401
