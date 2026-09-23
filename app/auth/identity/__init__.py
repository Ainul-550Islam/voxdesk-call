"""Enterprise identity (STEP 18).

The package holds the identity layer as one readable unit: the schema
(``models``), the policy evaluator (``policies``), the session and MFA
machinery, the machine-credential implementations, the federated-login
protocols (``sso``) and the provisioning API (``scim``).

Nothing here replaces what the product already had. ``app.auth.service`` still
authenticates passwords, ``app.auth.jwt`` still mints the same tokens, and
``app.auth.rbac`` is still the only source of truth for what a role may do. This
package adds the enterprise surface *on top*: a second factor, a federated
login, session visibility, machine identities, and the audit events that make
all of it reviewable.
"""
