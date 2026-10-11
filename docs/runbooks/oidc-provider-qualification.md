# Live OIDC provider qualification

This runbook validates one deployed provider configuration against Spine's
provider-neutral OAuth resource-server contract. It is intentionally not part of the default offline suite and must be run for the exact configuration version
used in staging and again before production enablement.

## Prerequisites

Prepare an operator-controlled client registration and retain the following as
secrets or deployment evidence, never in Git or shell history:

- `SPINE_LIVE_OIDC_CONFIGURATION_VERSION`: immutable Spine configuration version;
- `SPINE_LIVE_OIDC_ISSUER`: exact HTTPS issuer URL, without query or fragment;
- `SPINE_LIVE_OIDC_AUDIENCE`: exact Spine API audience;
- `SPINE_LIVE_OIDC_CLIENT_ID`: qualified user-facing interactive client ID;
- `SPINE_LIVE_OIDC_ACCESS_TOKEN`: a short-lived signed RFC 9068 access token for
  that issuer, audience, and client, with `typ=at+jwt` or
  `typ=application/at+jwt`;
- `SPINE_LIVE_OIDC_CLIENT_CREDENTIALS_DISABLED_EVIDENCE`: a safe change-ticket or
  provider-configuration reference proving the client cannot use the
  client-credentials grant for the Spine audience.

The provider must publish exact issuer discovery and a same-origin HTTPS JWKS
endpoint. The registration must issue RS256 JWT access tokens containing `iss`,
`aud`, `sub`, `client_id`, `exp`, `iat`, and `jti`. Do not use an ID token.

## Smoke command

Load the values through the deployment secret mechanism, then opt in explicitly:

```bash
export SPINE_RUN_LIVE_OIDC=1
uv run pytest -q -m live_oidc tests/integration/oidc/test_live_provider.py
```

The command verifies discovery, JWKS compatibility, readiness, signature and
claim validation, and configuration-version provenance. It never prints the
access token or provider payload.

## Provider controls that require operator evidence

The smoke test cannot create provider grants or force administrative settings.
Before approval, attach evidence that:

1. the listed client is interactive and cannot obtain a client-credentials token
   for the exact Spine audience;
2. a token for another audience or client is rejected;
3. expiry and configured clock-skew boundaries match the deployment policy;
4. provider timeouts and outages produce unavailable readiness without raw
   provider detail;
5. real key rotation was exercised.

For key rotation, keep one Spine process running, authenticate a token signed by
the current key, rotate the provider key, then authenticate a new token with the
new `kid`. Record that the unknown key caused one refresh, the new signature was
accepted, and an unknown or over-stale key was not accepted. The deterministic
offline matrix in `tests/infrastructure/auth/test_key_cache.py` remains the
regression gate for concurrency, bounded stale fallback, and refresh failure.

## Record the result

Record the date, environment, configuration version, exact issuer, audience,
client ID, safe evidence references, key-rotation result, and command result.
Never record the access token, raw claims, subject, provider response, or signing
key. If any operator control was not exercised, mark it unverified and keep the
protected routes disabled for that environment.
