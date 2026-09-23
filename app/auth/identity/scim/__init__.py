"""SCIM 2.0 provisioning (RFC 7643 / RFC 7644).

``schemas`` — resource representations, built by hand and readable against the RFC
``filter``  — a parsed, allow-listed filter compiler (never string-built SQL)
``service`` — credentials, users, groups, and the deprovisioning rules

A SCIM client is a machine principal authenticated by its own tenant-scoped
credential; it is never a user session and never an API key.
"""
