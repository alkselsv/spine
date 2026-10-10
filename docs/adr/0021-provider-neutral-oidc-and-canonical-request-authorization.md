---
status: accepted
---

# Provider-neutral OIDC and canonical request authorization

Spine's HTTP API acts as an OAuth 2.0 resource server and accepts only signed
JWT access tokens issued for the configured API audience. It discovers issuer
metadata and signing keys through provider-neutral OpenID Connect metadata, uses
the provider-scoped `(issuer, subject)` only as an Authentication Alias, and
resolves that alias through PostgreSQL to a workspace-scoped Canonical Human
Identity. Workspace membership, environment membership and the R1
`administrator` role come only from current Spine-owned records; token roles,
groups and arbitrary claims never grant authority.

The alternative of trusting identity-provider groups was rejected because it
would make provider configuration a second authorization store and would couple
Spine to provider-specific claim shapes. Accepting ID tokens directly at the API
or using an identity-provider SDK was rejected because it risks cross-JWT
confusion and makes the API boundary depend on a browser client or vendor. The
frontend may later acquire access tokens through Authorization Code + PKCE, but
that login flow remains outside the API authentication seam.

Every protected route declares its scope kind, required role, purpose and
operation. A trusted server composition root combines the validated
Authentication Alias, current PostgreSQL bindings, server-validated Workspace
and Environment, and the existing Diagnostic Context into one immutable request
context before application execution. The development local adapter implements
the same authentication port with one configured identity and is rejected at
startup outside an explicit development deployment mode.

This decision requires an issuer capable of producing RFC 9068-compatible JWT
access tokens for the Spine API audience, makes OIDC discovery/JWKS availability
part of readiness with bounded key caching, and leaves provisioning, browser
login UX, document Access Policy evaluation, delegation and impersonation to
their owning specifications.
