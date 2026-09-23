"""Resource documents, list envelopes and error bodies.

Implementation: :mod:`app.auth.identity.scim.schemas`.

This module is the presentation half of the SCIM surface, and it is the half a
customer's IdP breaks on:

* ``user_resource`` / ``group_resource`` serialize a row into the RFC 7643
  shape, including ``externalId``, the ``primary`` email flag, ``active``, and
  the ``meta`` block with the resource type and a ``location`` that actually
  resolves;
* ``list_response`` builds the RFC 7644 §3.4.2 envelope with ``totalResults``,
  ``startIndex`` and ``itemsPerPage``, so a provider can page correctly;
* ``error_response`` builds the RFC 7644 §3.12 body: ``schemas``,
  ``detail``, and ``scimType`` such as ``uniqueness`` or ``invalidValue``.
  The HTTP status is decided next to it, in the routes, so a 409 and a
  ``scimType: uniqueness`` always travel together;
* ``ServiceProviderConfig``, ``ResourceTypes`` and ``Schemas`` describe what
  this deployment supports — including patch, filter and the ``authentication``
  scheme — without overstating anything.

Timestamps are always ISO-8601 with a ``Z``, never a local time.
"""
from __future__ import annotations

from app.auth.identity.scim.schemas import (
    error_response,
    group_resource,
    list_response,
    parse_uuid,
    resource_types,
    schemas,
    service_provider_config,
    user_resource,
)

__all__ = [
    "error_response",
    "group_resource",
    "list_response",
    "parse_uuid",
    "resource_types",
    "schemas",
    "service_provider_config",
    "user_resource",
]
