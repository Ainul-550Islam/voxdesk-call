"""Federated login: OIDC and SAML 2.0.

``claims``  — claim normalization, role mapping, provisioning decisions
``oidc``    — discovery, authorization-code flow, ID-token verification
``saml``    — SP metadata, AuthnRequest, response parsing and signature checks
``service`` — connections, certificates, login attempts, and completion

The split is deliberate: the protocol modules know nothing about the database
beyond the connection row they are handed, and the service module holds every
decision that touches a user. That is what lets the signature and token checks
be tested against crafted input rather than against a live identity provider.
"""
