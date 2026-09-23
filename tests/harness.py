"""Helpers that build requests for the suites that drive the real HTTP surface.

Two of them graduate here because more than one test package needs them: the
federated-login tests need an IdP, the SCIM tests need a caller that speaks
``application/scim+json`` with a provisioning credential, and the integration
tests need both. Fixtures stay in ``conftest.py``; the mechanics of making a
request live next to the data they carry.
"""
from __future__ import annotations

import json

SCIM_MEDIA = "application/scim+json"


class ScimClient:
    """A SCIM caller bound to one connection."""

    def __init__(self, client, connection_id: str, token: str) -> None:
        self._client = client
        self.connection_id = connection_id
        self.token = token

    def url(self, path: str = "") -> str:
        suffix = f"/{path.lstrip('/')}" if path else ""
        return f"/scim/v2/{self.connection_id}{suffix}"

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "content-type": SCIM_MEDIA}

    async def get(self, path: str = "", **params):
        return await self._client.get(self.url(path), headers=self.headers, params=params or None)

    async def post(self, path: str = "", body: dict | None = None):
        return await self._client.post(
            self.url(path), content=json.dumps(body or {}), headers=self.headers
        )

    async def put(self, path: str, body: dict):
        return await self._client.put(self.url(path), content=json.dumps(body), headers=self.headers)

    async def patch(self, path: str, body: dict):
        return await self._client.patch(self.url(path), content=json.dumps(body), headers=self.headers)

    async def delete(self, path: str):
        return await self._client.delete(self.url(path), headers=self.headers)

    def as_token(self, token: str) -> "ScimClient":
        """The same caller, with a different token — used to prove a token died."""
        return ScimClient(self._client, self.connection_id, token)


def user_payload(user_name: str, **overrides) -> dict:
    """A minimal RFC 7643 user, with the fields an IdP always sends."""
    payload = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": user_name,
        "displayName": user_name.split("@")[0].title(),
        "emails": [{"value": user_name, "primary": True}],
        "active": True,
    }
    payload.update(overrides)
    return payload


__all__ = ["SCIM_MEDIA", "ScimClient", "user_payload"]
