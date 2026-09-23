# VoxDesk — SAML 2.0 service provider

AuthnRequest generation, assertion parsing, a hand-written XML-DSig verifier,
and every content rule that turns a signed XML document into a login.
Implementation: `app/auth/identity/sso/saml.py` (protocol),
`app/auth/identity/sso/service.py` (connections, attempts, provisioning),
routes in `app/api/sso_routes.py`.

OIDC is the subject of [`SSO-OIDC.md`](SSO-OIDC.md). Connection admin,
provisioning, claim mapping and audit are shared and documented there; this file
covers what is specific to SAML.

---

## 1. Endpoints

| Method | Path | Who | Purpose |
| --- | --- | --- | --- |
| `GET` | `/auth/sso/{slug}/metadata` | public | SP metadata for an active SAML connection |
| `GET` | `/api/sso/connections/{id}/metadata` | `identity:read` | the same document, admin-scoped |
| `POST` | `/auth/sso/{slug}/acs` | public | assertion consumer service |
| `POST` | `/auth/sso/{slug}/slo` | public | single logout, IdP-initiated |
| `POST` | `/api/sso/connections/{id}/certificates` | `identity:write` + fresh proof | register the IdP signing certificate |
| `GET` | `/api/sso/connections/{id}/certificates` | `identity:read` | list, with status and expiry |
| `DELETE` | `/api/sso/connections/{id}/certificates/{cid}` | `identity:write` + fresh proof | retire (24 h overlap, never the last one) |
| `POST` | `/api/sso/connections/{id}/test` | `identity:write` | configuration self-test |

Metadata is public because the IdP must be able to fetch it before anyone has
logged in, and it contains nothing secret: an entity id and two URLs. It is
served **only** for a connection that speaks SAML: an OpenID Connect connection
has no ACS URL an IdP could post to, so asking for its metadata answers 404
(publicly) or 400/404 (administratively) rather than handing an administrator a
document full of URLs that would never work. It
advertises **no** `KeyDescriptor`, because we do not sign AuthnRequests and
advertising a key we do not use would be worse than omitting one.

---

## 2. The login exchange

```
GET  /auth/sso/{slug}/start
      ├── creates an attempt row (state hash, nonce hash, expiry)
      ├── builds a deflated AuthnRequest whose ID is derived from the attempt
      └── returns { authorization_url, state, connection_id, protocol }

browser → IdP → POST /auth/sso/{slug}/acs   (SAMLResponse, RelayState)
      ├── spend RelayState as the single-use state (atomic UPDATE)
      ├── parse once; verify the signature on the same tree
      ├── validate content (issuer, audience, recipient, destination,
      │   times, InResponseTo, name-id format)
      ├── claim the assertion id (unique constraint = replay protection)
      ├── resolve/provision the user, map the role
      └── mint the session, set the refresh cookie
```

The `InResponseTo` value is **derived** from the attempt
(`request_id_for(attempt) == f"id-{attempt.id}"`) rather than stored in a second
column, so the value checked cannot drift from the value actually sent — it is
a function of the row that produced the AuthnRequest.

`RelayState` carries the state value and nothing else. It is not signed because
it is only ever compared to a row: an attacker who changes it invalidates it.

The ACS is bound to one connection by slug, and `SAMLResponse` is bounded
before parsing: > 512 KiB (or > 1 MiB encoded) is answered with **413**
`assertion_too_large` rather than being decoded. A real IdP assertion is a few
kilobytes; a megabyte of XML is not a login attempt.

---

## 3. Parsing (`safe_parse`, `parse_response`)

`safe_parse` refuses any document containing a **document type declaration**
before the parser sees it, then parses with `resolve_entities=False,
no_network=True, load_dtd=False, dtd_validation=False, huge_tree=False,
recover=False`. Refusing a DTD up front removes the entire XXE and
entity-expansion class of attack, and no legitimate SAML response has one.

`parse_response` performs the checks that do not depend on configuration, so no
caller can forget them:

- the root element is a `Response`;
- the status is exactly `urn:oasis:names:tc:SAML:2.0:status:Success` (anything
  else is reported with the status code, so an operator sees *why* the IdP
  refused);
- there is **exactly one** assertion — two is the substrate of signature
  wrapping, and the parser refuses it here rather than hoping a later check
  catches it;
- the assertion has an `ID`, without which replay protection is impossible.

It then returns a `ParsedAssertion` with `assertion_id`, `name_id`,
`name_id_format`, `issuer`, `audience`, `recipient`, `destination`,
`in_response_to`, `not_before`, `not_on_or_after`,
`subject_not_on_or_after`, `attributes`, `response_id` and `session_index`.

Attribute values are normalised: **one value is a `str`, several are a `list`**,
so a claim mapper never has to guess the shape it was handed.

The response bytes are parsed **once** and the same tree is used for both
signature verification and the structural checks. Parsing untrusted XML twice
is not merely wasted work — it is two chances for the two parses to disagree.

---

## 4. Signature verification (`signature_of`, `verify_signature`)

`signature_of(root, parsed, connection, certificates)`:

1. Builds `{fingerprint: pem}` from the connection's certificates, decrypting
   each with the identity key ring (a row that will not decrypt is logged and
   skipped, never trusted).
2. Verifies the assertion's signature **if present**, and the response's
   signature if present.
3. Refuses an **unsigned assertion** when
   `require_signed_assertions` is set (the default) — a signed envelope must not
   be able to protect a body somebody swapped.
4. Requires at least one signature.

`verify_signature` is deliberately narrow, because XML-DSig is a large
specification and the parts an attacker can abuse are exactly the parts a
general library offers:

| Check | Rule |
| --- | --- |
| signature present | a `<ds:Signature>` must exist on the element being verified |
| canonicalization | only exclusive and inclusive C14N 1.0 are accepted |
| references | **exactly one** `ds:Reference`; more than one is refused outright |
| reference target | the URI must be `#<id>` and must resolve to the element the signature sits in — a signature covering a different element is refused |
| transforms | enveloped-signature, exclusive C14N, inclusive C14N; anything else is refused |
| digest | SHA-1 / SHA-256 / SHA-384 / SHA-512 only, and the computed digest must equal `ds:DigestValue` (constant-time compare) on a copy of the tree with the signature removed |
| signature method | RSA-PKCS1v15 or ECDSA with SHA-1/256/384/512; anything else is refused |
| certificate | the presented `ds:KeyInfo/ds:X509Data/ds:X509Certificate` **must match one on file** (byte-for-byte DER comparison). The certificate in the response is used *only* to name the fingerprint in the error; it is never trusted because it arrived with the assertion |
| SHA-1 | still accepted for compatibility with older IdPs, but every acceptance is logged as `sso.sha1_signature_accepted` so a tenant can see it is in use |

The verifier is hand-written and is tested against `signxml`, an independent
XML-DSig implementation — the tests sign with a second library and verify with
this one, so a bug would have to appear in both to pass.

A refusal names the certificate **by fingerprint**, never by echoing it, so an
operator can compare with their IdP's current certificate without the
certificate travelling through logs.

---

## 5. Content validation (`validate_assertion`)

Every check is configuration-dependent, and each refusal names the check that
failed — an operator chasing a broken integration needs to know it was the
audience, not "invalid SAML".

| Check | Rule |
| --- | --- |
| `InResponseTo` | must equal the request id derived from this attempt |
| issuer | must equal the connection's `idp_entity_id` |
| audience | the SP entity id must appear in an `AudienceRestriction`; a response with **no** audience restriction is refused |
| recipient | `SubjectConfirmationData/@Recipient` must equal the ACS URL when present |
| destination | `Response/@Destination` must equal the ACS URL when present |
| `NotBefore` | may not be in the future beyond `SSO_CLOCK_SKEW_SECONDS` |
| `NotOnOrAfter` | must exist (an assertion with no expiry is refused) and must not have passed, skew included |
| subject confirmation | `SubjectConfirmationData/@NotOnOrAfter` is checked separately; an expired subject confirmation is refused even if the conditions are valid |
| name-id format | if the connection pins one, the assertion must use it |

The AssertionConsumerService URL checked is the connection's configured
`acs_url` when set, otherwise the derived one — the same value the metadata
document publishes, so an IdP cannot be pointed at one URL and validated
against another.

---

## 6. Single logout

`POST /auth/sso/{slug}/slo` acknowledges an IdP-initiated sign-out and returns
`{"logged_out": true, "detail": "Sign-out acknowledged. Sessions in this
workspace were not affected."}`.

**Our sessions are ours.** An IdP saying "this user signed out elsewhere" is
treated as a request to end sessions, never as proof that a session already
ended, and the endpoint is idempotent. It performs no destructive action on its
own: a logout that a third party can trigger for any user is a denial of
service, not a security feature.

---

## 7. Certificates and rotation

- Certificates are stored **sealed** (`pem_encrypted`, `pem_key_id`) with the
  tenant in the AAD, plus the SHA-256 fingerprint, subject, issuer and validity
  window for display. The seal is keyed by *purpose*
  (`identity.secrets.PURPOSE_SAML_CERTIFICATE`), and the reader opens with the
  same constant: a purpose that drifts between the writer and the reader makes
  every certificate unreadable at once, which presents to an operator as "no
  signing certificate is configured" while the row sits there marked active.
- `add_certificate` is **idempotent by fingerprint**: uploading last year's and
  this year's certificate during a rotation does not create duplicate rows.
- `read_certificate` accepts a PEM block, a bare base64 DER body (what IdPs
  paste), or a whole metadata/`X509Certificate` document with the base64
  extracted. Truncated PEM material is refused with a validation message, not a
  crash.
- `retire_certificate` refuses to retire **the last usable certificate of an
  active connection** — one click must not lock out every user of that
  connection — and keeps the retired row for an overlap window so a provider
  mid-rollover is not rejected instantly.
- Both operations emit `SSO_CERTIFICATE_ROTATED` with the operation
  (`registered` / `retired`), the fingerprint and the expiry, and both require a
  fresh proof of presence.

The rotation procedure: register the new certificate (still active alongside the
old one) → roll the IdP over → confirm logins succeed → retire the old one.

---

## 8. Replay protection

Two mechanisms, both required:

- the **state** (RelayState) is single-use and spent atomically, so a captured
  ACS POST cannot be replayed through the same attempt;
- the **assertion id** is claimed by writing it onto the attempt row, where a
  `UNIQUE` constraint (`uq_sso_attempt_assertion`) makes a second use a
  database error rather than a second session. Replay protection that the
  database enforces cannot be forgotten by a future code path.

An assertion that arrives after its attempt expired is refused by
`consume_state` before any XML is parsed.

---

## 9. Configuration

Per connection: `slug`, `idp_entity_id`, `idp_sso_url`, `idp_slo_url`,
`sp_entity_id`, `acs_url`, `name_id_format`, `require_signed_assertions`,
`signature_algorithm`, `require_verified_email`, `allow_account_linking`,
`jit_enabled`, `deny_unmapped_roles`, `default_role`, claim mappings and claim
names.

Deployment-wide: `SSO_ENABLED`, `SSO_STATE_TTL_SECONDS`,
`SSO_CLOCK_SKEW_SECONDS`, `PUBLIC_BASE_URL`,
`IDENTITY_ENCRYPTION_KEYS` (certificates cannot be sealed without it, and
production refuses SSO with no ring).

---

## 10. Provisioning and linking

SAML and OIDC share one provisioning engine — `claims.decide_provisioning` in
`app/auth/identity/sso/claims.py` — so the rules, including the four refusals
that protect the account boundary, are documented once in
[`SSO-OIDC.md`](SSO-OIDC.md#7-provisioning-and-account-linking). Two of them are
worth repeating here because SAML is where operators meet them:

- **Account linking is off by default** (`connections.allow_account_linking`).
  An assertion for an address that already exists is refused until an
  administrator turns linking on for that connection; the default is "refuse",
  because an IdP that can attach its own subject to an existing account can
  attach itself to an administrator's.
- **Provisioning has two switches** and both must be on:
  `connections.jit_enabled` and the workspace's
  `identity_policies.jit_provisioning_allowed`. They are checked separately so
  that the refusal says which one is off.

The HTTP response is the flat `{"code": "sso_failed"}` for every SSO failure —
the login endpoint must not become a way to discover which addresses exist. The
reason is on the exception's `code` and in the audit trail: one of
`sso_account_linking_disabled`, `sso_link_requires_verified_email`,
`sso_provisioning_disabled_on_connection` or
`sso_provisioning_disabled_for_workspace`, recorded as `IDENTITY_LINK_REJECTED`
with the connection id.

A **disabled** user is refused even though the IdP keeps asserting them
(`SSO_LOGIN_FAILED`, reason `user_disabled`): an identity provider must not be
able to undo a deprovisioning. A user that was *deleted* is a different case, and
deliberately so: with JIT on, the next assertion creates a fresh account, exactly
as a directory-driven login is supposed to. Deprovisioning in the directory means
deactivating the assignment there, not only deleting the record here.

---

## 11. Tests

`tests/security/test_sso.py` generates a real RSA key pair and a self-signed
certificate, mints signed assertions with `signxml`, and drives the verifier
directly — there is no fake provider in the application and no skipped
"real provider" test.

| Test | Proves |
| --- | --- |
| correctly signed assertion accepted | happy path, including the matched fingerprint in the result |
| tampered attribute | the signature covers the claims |
| unsigned assertion inside a signed response | `require_signed_assertions` is honoured; the same response is acceptable with it off |
| assertion signed by an untrusted certificate | refusal names the fingerprint, never echoes the certificate |
| two assertions in one response | signature wrapping is refused at parse time |
| reference pointing at a different element | a signature that covers something else is refused |
| two `ds:Reference`s, unsupported digest, non-success status | the narrow acceptance set |
| wrong `InResponseTo` / audience / recipient / destination / issuer | each content rule, individually |
| no audience restriction, no expiry, expired, expired subject confirmation, not-yet-valid | the time and audience rules, with skew |
| connection with no certificate / undecryptable certificate | fail closed |
| certificate parsing (PEM, bare DER, pasted XML, junk, truncated) | what an operator actually pastes |
| SP metadata, AuthnRequest uniqueness and NSName-ness, DTD refusal | the SP side of the protocol and the XXE door being shut |
