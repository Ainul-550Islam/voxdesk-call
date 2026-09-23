"""The SCIM package's fixtures now live where every package can reach them.

The caller and its payload builder are in :mod:`tests.harness` and the fixtures
(``scim_connection``, ``scim_token``, ``scim``, ``in_another_tenant``) are in the
root ``tests/conftest.py``, because the integration tests drive the same
endpoints. This module stays so the existing ``from tests.auth.scim.conftest
import ScimClient, user_payload`` keeps working.
"""
from __future__ import annotations

from tests.harness import SCIM_MEDIA, ScimClient, user_payload

__all__ = ["SCIM_MEDIA", "ScimClient", "user_payload"]
