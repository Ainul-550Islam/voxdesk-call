"""The one thing SQLite cannot check: PostgreSQL accepts the labels we write.

``audit_logs.action`` is ``Enum(AuditAction)``. On SQLite that renders as
``VARCHAR`` plus a ``CHECK`` built from the model, so a member that exists in
Python is always writable. On PostgreSQL it is a real ``auditaction`` type, and a
member added to the model but never added to the type fails at INSERT time with::

    invalid input value for enum auditaction: "CREDENTIAL_AUTH_REJECTED"

That is exactly the bug migration ``0014`` was written to repair, and the reason
``0013`` shipped 53 labels nothing could ever write: the whole test suite ran on
SQLite and was green while a real deployment could not record a single identity
event.

So this suite asks the live database the question the rest of the suite cannot:
*is every member of ``AuditAction`` writable here?* It is opt-in — it skips when
``DATABASE_URL`` is not PostgreSQL — and it writes and removes its own probe rows
under a unique marker, so it leaves the database exactly as it found it.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.identity.events import event_names
from app.core.config import settings
from app.db.models import AuditAction, AuditLog

pytestmark = pytest.mark.asyncio


def _database_url() -> str:
    return os.environ.get("DATABASE_URL") or str(settings.database_url)


@pytest.fixture
async def postgres_session():
    url = _database_url()
    if not url.startswith("postgresql"):
        pytest.skip(
            "This check needs the real database: set DATABASE_URL to a PostgreSQL "
            "instance that has been migrated to head."
        )
    engine = create_async_engine(url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


async def test_every_audit_action_is_writable_in_the_live_database(postgres_session):
    session = postgres_session
    marker = f"enum-probe-{uuid.uuid4()}"
    members = list(AuditAction)

    rows = [
        AuditLog(id=uuid.uuid4(), action=action, actor_email=marker, detail={})
        for action in members
    ]
    session.add_all(rows)
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - the message is the assertion
        await session.rollback()
        missing = [
            action.name
            for action in members
            if f'"{action.name}"' in str(exc) or f"'{action.name}'" in str(exc)
        ]
        pytest.fail(
            f"the live database refused {len(missing) or 'some'} audit labels "
            f"({', '.join(missing) or 'unparsed error'}): {exc}"
        )

    try:
        read_back = (
            await session.execute(
                select(AuditLog.action).where(AuditLog.actor_email == marker)
            )
        ).scalars().all()
        assert set(read_back) == set(members), (
            "every member must round-trip through the database type"
        )
        assert len(read_back) == len(members)
    finally:
        await session.execute(delete(AuditLog).where(AuditLog.actor_email == marker))
        await session.commit()

    remaining = (
        await session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.actor_email == marker)
        )
    ).scalar_one()
    assert remaining == 0, "the probe rows were removed again"


async def test_the_identity_event_names_are_writable_too(postgres_session):
    """The names the events endpoint filters on, written and removed the same
    way: a vocabulary the feed advertises must be a vocabulary the database
    can store."""
    session = postgres_session
    marker = f"event-probe-{uuid.uuid4()}"
    names = event_names()
    assert names, "the identity event vocabulary is empty"

    session.add_all(
        [
            AuditLog(id=uuid.uuid4(), action=AuditAction(name), actor_email=marker, detail={})
            for name in names
        ]
    )
    await session.commit()
    try:
        stored = (
            await session.execute(
                select(AuditLog.action).where(AuditLog.actor_email == marker)
            )
        ).scalars().all()
        assert {row.value for row in stored} == set(names)
        assert AuditAction.CREDENTIAL_AUTH_REJECTED.value in names, (
            "a refused machine credential is an identity event"
        )
    finally:
        await session.execute(delete(AuditLog).where(AuditLog.actor_email == marker))
        await session.commit()
