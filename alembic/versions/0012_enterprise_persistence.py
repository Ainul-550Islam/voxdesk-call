"""enterprise surface persistence: automations, notifications, inbox state

Batch 02 closes the schema gap Batch 01 reported: the automation, notification
and inbox services kept their state in process-local dictionaries, so a restart
(or a second API worker) lost every automation, notification and unread badge.

Five tables, all tenant-scoped and all cascading from `tenants` (and, for the
inbox overlay, from `calls` so the retention purge takes it along):

* ``automations``            — definitions (filters/actions as JSON)
* ``automation_runs``       — one row per business event, PK = idempotency key
* ``notification_templates``— templates
* ``notifications``         — rendered notifications (holds recipient PII;
                              unique per (tenant, dedupe_key); purged by
                              ``app.core.retention.purge_expired_notifications``)
* ``inbox_thread_states``   — the inbox overlay (priority/assignee/tags/notes/
                              unread/SLA) over a real ``calls`` row

State columns are plain strings rather than PostgreSQL enum types: the closed
vocabularies live in the domain layer (and are pinned by
``tests/test_enum_consistency.py`` for the enums that already existed), and this
migration deliberately does not widen that contract.

Revision ID: 0012_enterprise_persistence
Revises: 0011_side_effect_exactly_once
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_enterprise_persistence"
down_revision = "0011_side_effect_exactly_once"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="disabled"),
        sa.Column("filters", sa.JSON(), nullable=True),
        sa.Column("actions", sa.JSON(), nullable=True),
        sa.Column("schedule_kind", sa.String(length=16), nullable=False, server_default="on_event"),
        sa.Column("delay_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("backoff_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_per_event", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_run_at", sa.String(length=40), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_automations_tenant_id", "automations", ["tenant_id"])
    op.create_index("ix_automations_tenant_event", "automations", ["tenant_id", "event"])

    op.create_table(
        "automation_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("automation_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("business_event_id", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("last_error", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("result_summary", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("finished_at", sa.String(length=40), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_automation_runs_tenant_id", "automation_runs", ["tenant_id"])
    op.create_index("ix_automation_runs_automation_id", "automation_runs", ["automation_id"])
    op.create_index("ix_automation_runs_idempotency_key", "automation_runs", ["idempotency_key"])
    op.create_index(
        "ix_automation_runs_tenant_automation", "automation_runs",
        ["tenant_id", "automation_id"],
    )
    op.create_index("ix_automation_runs_created", "automation_runs", ["created_at"])

    op.create_table(
        "notification_templates",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("variables", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_templates_tenant_id", "notification_templates", ["tenant_id"]
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("recipient", sa.JSON(), nullable=True),
        sa.Column("event_source", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("rendered_body", sa.Text(), nullable=False, server_default=""),
        sa.Column("delivery_state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("sent_at", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("error_summary", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_notification_dedupe"),
    )
    op.create_index("ix_notifications_tenant_id", "notifications", ["tenant_id"])
    op.create_index("ix_notifications_template_id", "notifications", ["template_id"])
    op.create_index("ix_notifications_tenant_state", "notifications", ["tenant_id", "delivery_state"])
    op.create_index("ix_notifications_created", "notifications", ["created_at"])

    op.create_table(
        "inbox_thread_states",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "call_id", sa.UUID(), sa.ForeignKey("calls.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("priority", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("assignee_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("notes", sa.JSON(), nullable=True),
        sa.Column("unread", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opened_at", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("sla_deadline_at", sa.String(length=40), nullable=False, server_default=""),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "call_id", name="uq_inbox_thread_state_call"),
    )
    op.create_index("ix_inbox_thread_states_tenant_id", "inbox_thread_states", ["tenant_id"])
    op.create_index("ix_inbox_thread_states_call_id", "inbox_thread_states", ["call_id"])


def downgrade() -> None:
    op.drop_index("ix_inbox_thread_states_call_id", table_name="inbox_thread_states")
    op.drop_index("ix_inbox_thread_states_tenant_id", table_name="inbox_thread_states")
    op.drop_table("inbox_thread_states")

    op.drop_index("ix_notifications_created", table_name="notifications")
    op.drop_index("ix_notifications_tenant_state", table_name="notifications")
    op.drop_index("ix_notifications_template_id", table_name="notifications")
    op.drop_index("ix_notifications_tenant_id", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index("ix_notification_templates_tenant_id", table_name="notification_templates")
    op.drop_table("notification_templates")

    op.drop_index("ix_automation_runs_created", table_name="automation_runs")
    op.drop_index("ix_automation_runs_tenant_automation", table_name="automation_runs")
    op.drop_index("ix_automation_runs_idempotency_key", table_name="automation_runs")
    op.drop_index("ix_automation_runs_automation_id", table_name="automation_runs")
    op.drop_index("ix_automation_runs_tenant_id", table_name="automation_runs")
    op.drop_table("automation_runs")

    op.drop_index("ix_automations_tenant_event", table_name="automations")
    op.drop_index("ix_automations_tenant_id", table_name="automations")
    op.drop_table("automations")
