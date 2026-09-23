"""Revoking sessions: one, the others, or all of them.

These are the four buttons the dashboard puts in front of a user, and each one
has a precise meaning that has to survive the code path it takes:

* revoke **one** — that device dies, the others keep working, and the refresh
  tokens minted from it die with it;
* revoke **the others** — the caller stays signed in (revoking yourself here is
  the classic bug: the user clicks "sign out everywhere else" and is thrown out
  of the page they clicked it on);
* revoke **all** — including the caller, and the access token stops working on
  the very next request rather than at its next expiry;
* afterwards the refresh token is useless too: a revoked session must not be
  able to mint a fresh access token, or "sign out" is a fifteen-minute promise.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth.identity.models import UserSession
from app.db.models import UserRole
from tests.conftest import auth_headers, make_tenant, make_user

pytestmark = pytest.mark.asyncio


def _second_device(app) -> AsyncClient:
    """A second browser: its own client, therefore its own cookie jar."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _session_id(headers: dict[str, str]) -> uuid.UUID:
    from app.auth.jwt import decode_access_token

    return decode_access_token(headers["Authorization"].split(" ", 1)[1]).session_id


async def _live_rows(db, user_id) -> list[UserSession]:
    rows = (
        await db.execute(
            select(UserSession)
            .where(UserSession.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return [row for row in rows if row.is_live(now)]


async def test_the_session_list_marks_the_caller_and_hides_nothing_else(client, app, db):
    tenant = await make_tenant(db, "Session list tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    first = await auth_headers(client, user)

    async with _second_device(app) as second:
        second_headers = await auth_headers(second, user)
        listed = await client.get("/api/sessions", headers=first)
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["current_session_id"] == str(_session_id(first))
        assert len(body["sessions"]) == 2
        current = [s for s in body["sessions"] if s["current"]]
        assert len(current) == 1 and current[0]["id"] == str(_session_id(first))
        assert body["limits"], "the list carries the tenant's session limits"
        assert second_headers  # the second device is signed in for real


async def test_revoking_one_session_leaves_the_others_alone(client, app, db):
    tenant = await make_tenant(db, "One session tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    first = await auth_headers(client, user)

    async with _second_device(app) as second:
        second_headers = await auth_headers(second, user)
        victim = str(_session_id(second_headers))

        revoked = await client.delete(f"/api/sessions/{victim}", headers=first)
        assert revoked.status_code in (200, 204), revoked.text

        assert (await second.get("/api/sessions", headers=second_headers)).status_code == 401
        assert (await client.get("/api/sessions", headers=first)).status_code == 200

    live = await _live_rows(db, user.id)
    assert len(live) == 1
    assert live[0].id == _session_id(first)


async def test_revoking_others_keeps_the_caller_signed_in(client, app, db):
    tenant = await make_tenant(db, "Revoke others tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    first = await auth_headers(client, user)

    async with _second_device(app) as second:
        second_headers = await auth_headers(second, user)
        response = await client.post("/api/sessions/revoke-others", headers=first)
        assert response.status_code == 200, response.text
        assert response.json()["revoked"] >= 1

        assert (await client.get("/api/sessions", headers=first)).status_code == 200
        assert (await second.get("/api/sessions", headers=second_headers)).status_code == 401

    live = await _live_rows(db, user.id)
    assert [row.id for row in live] == [_session_id(first)]


async def test_revoking_all_ends_the_callers_session_too(client, app, db):
    tenant = await make_tenant(db, "Revoke all tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    first = await auth_headers(client, user)

    async with _second_device(app) as second:
        second_headers = await auth_headers(second, user)
        response = await client.delete("/api/sessions", headers=first)
        assert response.status_code in (200, 204), response.text

        assert (await client.get("/api/sessions", headers=first)).status_code == 401
        assert (await second.get("/api/sessions", headers=second_headers)).status_code == 401

    assert await _live_rows(db, user.id) == []


async def test_a_revoked_refresh_token_cannot_mint_a_new_access_token(client, app, db):
    tenant = await make_tenant(db, "Refresh tenant")
    user = await make_user(db, tenant, UserRole.AGENT)

    async with _second_device(app) as second:
        second_headers = await auth_headers(second, user)
        victim = str(_session_id(second_headers))
        assert (await client.delete(f"/api/sessions/{victim}", headers=second_headers)).status_code in (200, 204)

        # The device still holds its refresh cookie; the session behind it is
        # gone, so it must not be exchangeable for a fresh access token.
        refreshed = await second.post("/auth/refresh", json={})
        assert refreshed.status_code == 401, refreshed.text


async def test_another_users_session_id_is_not_revocable(client, app, db, tenant_a, tenant_b):
    """A session id from someone else's account answers the same as a made-up one."""
    mine = await make_user(db, tenant_a, UserRole.AGENT)
    theirs = await make_user(db, tenant_b, UserRole.AGENT)
    my_headers = await auth_headers(client, mine)

    async with _second_device(app) as second:
        their_headers = await auth_headers(second, theirs)
        foreign = str(_session_id(their_headers))

        response = await client.delete(f"/api/sessions/{foreign}", headers=my_headers)
        assert response.status_code == 404, response.text
        assert (await second.get("/api/sessions", headers=their_headers)).status_code == 200


async def test_a_session_can_be_renamed_without_revoking_it(client, app, db):
    tenant = await make_tenant(db, "Rename tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    headers = await auth_headers(client, user)
    session_id = str(_session_id(headers))

    renamed = await client.patch(
        f"/api/sessions/{session_id}", json={"label": "Field laptop"}, headers=headers
    )
    assert renamed.status_code == 200, renamed.text
    assert (await client.get("/api/sessions", headers=headers)).status_code == 200
