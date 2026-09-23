# VoxDesk — SCIM 2.0 provisioning

Tenant-scoped SCIM 2.0 (RFC 7643 / RFC 7644): Users, Groups, the discovery
endpoints, and the bearer credential an identity provider authenticates with.
Implementation: `app/auth/identity/scim/service.py`, `.../scim/schemas.py`,
`.../scim/filter.py`, routes in `app/api/scim_routes.py`.

---

## 1. Two surfaces, two audiences

| Surface | Prefix | Authenticated by |
| --- | --- | --- |
| credential administration | `/api/scim/credentials` | a **human** session with `identity:write` + fresh proof of presence |
| the protocol | `/scim/v2/{connection_id}/…` | a **SCIM bearer token** and nothing else |

The separation is the point:

- A SCIM credential **cannot** call the product API. Its scopes are
  `scim:users` and `scim:groups` — deliberately not the RBAC vocabulary — so a
  leaked provisioning token can enumerate nothing but users and groups in its
  own tenant.
- A user JWT **cannot** call `/scim/v2`. An unattended integration must not need
  a human's token, and a human's token must never act as a provisioning
  credential. A SCIM credential is never a super-admin JWT: it is tenant-bound
  by its row and never reaches identity administration.

The protocol surface answers with SCIM error documents (RFC 7644 §3.12), not
this product's error shape, because the caller is an identity provider rather
than our dashboard:

| Condition | Status | `scimType` |
| --- | --- | --- |
| missing / malformed / revoked / expired token, inactive tenant | **401** | — |
| credential lacks the required scope | **403** | — |
| resource in another tenant | **404** | `notFound` (identical to "does not exist") |
| duplicate `userName` | **409** | `uniqueness` |
| bad filter, bad payload, unsupported PATCH path | **400** | `invalidValue` |

---

## 2. Credentials

```
vdscim_<32 hex chars of the row id>_<32-byte secret>
   │                  │                    └── hashed at rest, never stored
   │                  └── the row is looked up by it, so no scan is needed
   └── identification prefix (SCIM_TOKEN_PREFIX); not a secret
```

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/scim/credentials` | list, with prefix, label, scopes, last used |
| `POST` | `/api/scim/credentials` | 201, returns the token **once** |
| `POST` | `/api/scim/credentials/{id}/rotate` | replacement issued, old revoked in the same transaction |
| `DELETE` | `/api/scim/credentials/{id}` | 204 |

- A credential carries at least one of `scim:users` / `scim:groups`; a
  credential with neither is refused at creation.
- It may be bound to a **connection** (`connection_id`), which is what the
  `/scim/v2/{connection_id}/…` path segment names, or unbound (the path then
  uses `default`).
- Rotation follows the same rule as API keys: no window in which two tokens are
  valid. The revocation of the predecessor is a **conditional update**
  (`WHERE revoked_at IS NULL`), so two administrators rotating the same
  credential at the same moment cannot both succeed and leave two live tokens
  behind: the winner issues the replacement, the loser gets 404.
  `SCIM_CREDENTIAL_CREATED`, `_ROTATED`, `_REVOKED` are audited, one entry per
  event even under a retry.
- `DELETE` is idempotent for the same reason: revoking an already-revoked
  credential is a no-op (204) and does not write a second audit entry.

`authenticate` refuses with the **same** 401 and the same body for a malformed
token, an unknown row, a wrong secret, a revoked token, an expired token and an
inactive tenant, so the endpoint cannot be used to test whether a token exists.
Every 401 also carries `WWW-Authenticate: Bearer realm="scim"`, which is what an
IdP's configuration screen reads to confirm it is talking to a bearer-token
endpoint. `last_used_at` is written at most once per interval, not on every
request.

---

## 3. Discovery endpoints

| Method | Path | Returns |
| --- | --- | --- |
| `GET` | `/{connection_id}/ServiceProviderConfig` | RFC 7643 §5: `patch` supported, `filter` supported with `maxResults`, `etag` / `sort` / `changePassword` / `bulk` not supported — the truth, not an optimistic advertisement |
| `GET` | `/{connection_id}/ResourceTypes` | `User` and `Group` with their endpoints |
| `GET` | `/{connection_id}/Schemas` | the attribute schemas for both resources |

These are what an IdP reads before it starts provisioning; they are matched to
the behaviour in §4–§6 so an integration that reads them is not surprised.

---

## 4. Users

| Method | Path | Behaviour |
| --- | --- | --- |
| `GET` | `/{c}/Users` | `ListResponse` with `totalResults`, `startIndex`, `itemsPerPage`, `Resources` |
| `POST` | `/{c}/Users` | **201** with a `Location` header; 409 `uniqueness` on a duplicate |
| `GET` | `/{c}/Users/{id}` | one resource, or 404 |
| `PUT` | `/{c}/Users/{id}` | replace |
| `PATCH` | `/{c}/Users/{id}` | RFC 7644 §3.5.2 |
| `DELETE` | `/{c}/Users/{id}` | 204; deprovisions first, then deletes |

Fields honoured: `userName` (required on create; must be an address, because an
account that cannot authenticate is not an account), `externalId`,
`displayName` / `name.formatted`, `active`, `emails` (`primary`, `verified`),
and `groups` on read. Anything else in a payload is **ignored rather than
rejected** — IdPs send a lot of attributes, and refusing a payload because it
mentions `nickName` would make the integration unusable.

Resource shape (RFC 7643 §4.1) includes `id`, `externalId`, `userName`, `name`,
`displayName`, `emails`, `active`, `groups`, and a `meta` block with
`resourceType`, `created`, `lastModified` and `location`. Timestamps are
RFC 3339 UTC with a `Z`.

### Deactivation is immediate

`active: false` (through PUT, PATCH or the dedicated path) is not a flag an
integration can set and forget:

- `is_active` goes false, `token_version` is bumped, **every refresh token is
  revoked**, and **every `user_sessions` row is closed** with
  `revoked_reason = "scim_deprovisioned"`. The user is out at the next request,
  not at the next token expiry.
- The **last active owner cannot be deprovisioned** — 403 with an explanation,
  because a provisioning system that empties the owner seat would lock a tenant
  out of its own settings.
- `SCIM_USER_DEPROVISIONED` is emitted.
- Reactivation is allowed and emits `SCIM_USER_UPDATED`.

`DELETE` deprovisions first and then removes the row, so no orphaned access
survives a deletion mid-flight.

### PATCH semantics

`{"op": "add"|"replace"|"remove", "path": …, "value": …}` with RFC 7644 path
handling, including the no-path form where `value` is an object of
attribute → value. Supported paths: `active`, `displayName`,
`name.formatted`, `userName`, `emails` / `emails.value`, `externalId`.

- Unknown paths are **refused** (`400 invalidValue`) rather than silently
  ignored: an integration that believes it disabled something must not be wrong.
- `remove` on a user attribute is refused with a message pointing at
  deactivation, which is the operation the IdP actually wants.
- A PATCH that leaves the user inactive runs the same immediate deprovisioning
  path as an explicit `active: false`.

---

## 5. Groups

Groups are stored as `scim_group_mappings` rows and synchronise **roles**:

| Method | Path | Behaviour |
| --- | --- | --- |
| `GET` | `/{c}/Groups` | list, with `members` |
| `POST` | `/{c}/Groups` | 201; `displayName` required |
| `GET`/`PUT`/`PATCH` | `/{c}/Groups/{id}` | read / replace / patch |
| `DELETE` | `/{c}/Groups/{id}` | 204 |

`_role_for_group` maps a group name through the connection's `group_mapping`
(see `SSO-OIDC.md` §6); a group with no mapping changes nothing. Every membership
change is audited (`SCIM_GROUP_CREATED`, `_UPDATED`, `_DELETED`, plus
`SCIM_USER_UPDATED` for the affected users).

`_sync_group_roles` grants each member the **highest** role any of their groups
maps to, and never lowers a role:

- an IdP syncs groups one at a time, so a member of both `Managers` and `Agents`
  would otherwise end up with whatever the last-synced group maps to;
- a role that came from a claims mapping, from SCIM provisioning or from an
  administrator is not revoked by a directory sync — deprovisioning is what
  revokes access;
- a role outside `ASSIGNABLE_VIA_SSO` is ignored, and an owner is never touched,
  so a group cannot grant `owner` even if a mapping somehow named it (the
  mapping endpoint refuses that outright).

There is no path by which a provisioning system invents authority: the mapping
is administrator-configured and validated at write time, and a group only ever
*raises* a member to a role the mapping already names.

---

## 6. Filtering and pagination

`GET …/Users?filter=…&startIndex=…&count=…`

- The filter grammar (RFC 7644 §3.4.2.2) is **parsed, not evaluated**: a real
  tokenizer, an allow-list of attributes per resource type, and bound
  parameters. `eval`-style translation or string interpolation would be a SQL
  injection with extra steps, and a filter arrives from an external system and
  runs against a tenant's data.
- Supported: `eq ne co sw ew pr gt ge lt le`, `and` / `or` / `not`, parentheses,
  and the sub-attributes we expose (`emails.value`, `emails.primary`,
  `name.formatted`, `meta.lastModified`, `meta.created`). Anything else is
  refused with the offending token and `scimType: invalidFilter` (RFC 7644
  §3.12), so an integrator gets a useful error instead of an empty result.
- Pagination: `startIndex` (1-based, default 1), `count` clamped to
  `SCIM_MAX_PAGE_SIZE` (default 500, default page `SCIM_DEFAULT_PAGE_SIZE` =
  100). `count=0` returns metadata with no resources, which is what an IdP
  asking "how many?" expects.
- A filter that matches nothing is **200 with an empty `ListResponse`**, not
  404.

---

## 7. Isolation, idempotency and what is never logged

- Every query is scoped by the **credential's** `tenant_id`; a resource id from
  another tenant answers 404 with the same body as an id that does not exist,
  so the surface cannot be used to probe for other tenants' users.
- `externalId` is recorded per user and returned on every read, so a client that
  lost its state resolves the person by the IdP's own handle rather than
  creating a second account under a new address; a genuine duplicate `userName`
  is 409 `uniqueness`, which idempotent clients handle by fetching.
- Two syncs racing for the same address produce **one** account and one 409: the
  unique constraint on `users.email` is the arbiter, and the loser is told it
  conflicted rather than being handed a 500.
- A malformed id is 400 `invalidValue`; a well-formed id that this workspace does
  not hold is 404 `notFound`. The two are distinguishable on purpose: an IdP
  needs to know whether to fix its request or drop a stale record.
- Audited: `SCIM_USER_PROVISIONED`, `SCIM_USER_UPDATED`,
  `SCIM_USER_DEPROVISIONED`, `SCIM_GROUP_*`, `SCIM_CREDENTIAL_*`.
- Never logged or returned: the credential secret (shown once, hashed at rest),
  and nothing in an audit detail carries a payload from the IdP.

---

## 8. Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `SCIM_ENABLED` | `true` | master switch; off means every SCIM route answers 404 |
| `SCIM_DEFAULT_PAGE_SIZE` | `100` | page size when `count` is omitted |
| `SCIM_MAX_PAGE_SIZE` | `500` | hard ceiling for `count` |
| `SCIM_TOKEN_PREFIX` | `vdscim` | identification prefix |
| `PUBLIC_BASE_URL` | — | used to build the `location` and `meta.location` URLs |

Per tenant, `identity_policies.scim_enabled` can switch provisioning off for one
tenant without touching the deployment. It is evaluated **per request**, not at
issuance: turning it off makes an existing credential stop working on its next
call (403, `SCIM provisioning is switched off for this workspace.`) rather than
at its next expiry, and the credential and its audit history are left untouched
for when it is turned back on.

---

## 9. Tests

SCIM behaviour is covered by the identity suites; a provisioning integration
should be validated end-to-end against a real IdP before enabling it in
production, with a credential scoped to one tenant and rotated after the test.
The mechanical contract of the routes (auth dependency, response shape, tenant
scoping) is asserted by `scripts/check_identity_route_contracts.py`, and the
authorization surface (human credential administration only) by
`tests/security/test_authorization_matrix.py`.
