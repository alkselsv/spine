---
status: accepted
---

# Provider-neutral OIDC and canonical request authorization

Spine's HTTP API acts as an OAuth 2.0 resource server and accepts only signed
JWT access tokens issued for the configured API audience. It discovers issuer
metadata and signing keys through provider-neutral OpenID Connect metadata, uses
the provider-scoped `(issuer, subject)` only as an Authentication Alias, and
resolves that alias through PostgreSQL to the platform-level Canonical Human
Identity representing that person. Workspace membership, environment membership
and the R1 `administrator` role come only from current Spine-owned records;
token roles, groups and arbitrary claims never grant authority.

The alternative of trusting identity-provider groups was rejected because it
would make provider configuration a second authorization store and would couple
Spine to provider-specific claim shapes. Accepting ID tokens directly at the API
or using an identity-provider SDK was rejected because it risks cross-JWT
confusion and makes the API boundary depend on a browser client or vendor. The
frontend may later acquire access tokens through Authorization Code + PKCE, but
that login flow remains outside the API authentication seam.

Every protected R1 route declares its required role, purpose and operation and
requires canonical Workspace and Environment scope. A trusted server
composition root combines the validated Authentication Alias, current
PostgreSQL bindings, server-validated Workspace and Environment, and the
existing Diagnostic Context into one immutable request context before
application execution. The development local adapter implements the same
authentication port with one configured bearer credential and one separately
configured identity, and is rejected at startup outside an explicit development
deployment mode.

Provider-neutrality here means standards-based seams without a vendor SDK or
vendor claims, not compatibility with every provider token profile. R1
intentionally requires an issuer capable of producing RFC 9068-compatible JWT
access tokens for the Spine API audience; opaque tokens and non-conforming JWTs
need a later adapter decision. Allowed client registrations are
operator-qualified as user-facing clients that cannot use client credentials
for this audience; the adapter does not infer grant type from vendor-specific
claims.
Authentication configuration is immutable, explicitly versioned and retained
in safe decision provenance. OIDC discovery/JWKS availability is part of
readiness with bounded key caching. Provisioning, browser login UX, document
Access Policy evaluation, delegation and impersonation remain with their owning
specifications.
