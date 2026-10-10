# Issue #5 — OIDC identity and request authorization

Status: implementation-ready; decomposed into GitHub Issues #69–#74.

Originating issue: GitHub Issue #5, "[R0 blocker] Implement OIDC identity and
workspace/environment authorization".

## Summary

R1 needs a trustworthy answer to four questions before any API command or query
runs: who authenticated, which Workspace and Environment are in scope, which
Spine roles are currently effective there, and which trace owns the operation.
The current prototype has no authentication or request authorization and accepts
caller-selected Cognee datasets directly. The persistence kernel already
requires a cryptographically bound trusted context, but deliberately cannot
construct one from an HTTP request.

This specification defines the smallest identity and request-authorization
foundation required by the governed document Q&A slice:

- a provider-neutral OIDC/OAuth resource-server adapter for signed JWT access
  tokens;
- Workspace-scoped Canonical Human Identity and Authentication Alias mapping;
- current Workspace membership and Environment-local R1 role bindings in
  PostgreSQL;
- immutable authorization and request contexts assembled only by a trusted
  composition root;
- early, disclosure-safe rejection of authentication, role and scope failures;
- an explicit development-only local authentication adapter;
- mapping into the existing trusted persistence and diagnostics contracts.

OIDC proves control of one external account. It does not grant a Spine role,
select a tenant, confer document access, or make token claims canonical. R1
authorization remains server-owned and deny-by-default.

## Goals

1. Authenticate interactive R1 API requests through one provider-neutral port
   without importing an identity-provider SDK into domain or application code.
2. Map a validated `(issuer, subject)` Authentication Alias to exactly one
   current Canonical Human Identity inside the requested Workspace.
3. Resolve current Workspace membership, Environment ownership/membership and
   the `administrator` role from PostgreSQL before application execution.
4. Construct one immutable request context carrying acting subject, Workspace,
   Environment, roles, server-declared purpose/operation and Diagnostic Context.
5. Produce an existing `TrustedPersistenceContext` without allowing request
   bodies, token groups, route dependencies or tests to mint authority.
6. Fail closed with stable, non-leaking errors and auditable decisions wherever a
   trusted tenant and actor can be established.
7. Keep the default test suite deterministic and offline while separately
   testing discovery, JWKS rotation and real PostgreSQL isolation.

## Non-goals

- Browser login pages, Authorization Code + PKCE UI, refresh-token storage,
  logout UX or frontend token management; Issue #24 consumes this API contract.
- User lifecycle, SCIM, just-in-time provisioning, invitation flows, password
  authentication or user/RBAC administration UI.
- Trusting provider `roles`, `groups`, `entitlements`, email, username or domain
  claims as Spine authority.
- Opaque-token introspection, multi-issuer federation, social login or
  identity-provider-specific claim adapters in R1.
- Service-to-service client-credentials authentication, connector identity or
  Tool Gateway credentials; those require their consuming slice.
- `read_content`, `process_content`, Access Policy, team grants, policy
  proposals, approvals or protected-content disclosure; Issue #7 and ADR 0011
  own those rules.
- Administrator impersonation, human delegation, break-glass access or
  administrator-loss recovery.
- A general policy engine or a role vocabulary beyond the R1 `administrator`.
- Protecting or changing the response bodies of legacy `/ask` and `/ingest`
  compatibility endpoints.

## Sources and accepted constraints

- `AGENTS.md` requires Workspace and Environment context, server-side
  authorization, two-workspace isolation and non-leaking denials.
- `docs/ARCHITECTURE.md` makes PostgreSQL authoritative for effective
  memberships and role bindings, separates Actor Reference from authority, and
  forbids client-controlled acting identity.
- `docs/INTERFACE.md` makes every R1 page/API operation administrator-only while
  preserving separate protected-content authorization.
- `CONTEXT.md` defines Acting Subject, Administrator, Canonical Human Identity
  and Trusted Authorization Context.
- ADR 0011 makes administrators non-content-superusers and current PostgreSQL
  policy the only authority for protected document content.
- The Issue #8 persistence kernel supplies canonical Workspace/Environment
  records, RLS, trusted context proofs and the Unit of Work boundary.
- The Issue #4 diagnostics specification supplies Diagnostic Context,
  Structured Error, safe Diagnostic Events and tenant-scoped Audit Events.
- [OpenID Connect Discovery 1.0](https://openid.net/specs/openid-connect-discovery-1_0.html)
  requires exact issuer metadata validation and publishes `jwks_uri`.
- [RFC 9068](https://www.rfc-editor.org/rfc/rfc9068.html) defines explicitly
  typed signed JWT access tokens and resource-server validation.
- [RFC 8725](https://www.rfc-editor.org/rfc/rfc8725.html) requires explicit
  algorithm verification and defenses against cross-JWT confusion.
- [PyJWT documentation](https://pyjwt.readthedocs.io/en/stable/usage.html)
  documents explicit issuer, audience, time and JWKS-backed signature checks.

## Domain language

This specification uses the existing terms literally:

- **Authentication Alias** is one provider-scoped `(issuer, subject)` identifier
  proven by a valid access token. It is not a User, role or authority.
- **Canonical Human Identity** is the Workspace-scoped representation of one
  verified human. Multiple approved Authentication Aliases may map to it.
- **Acting Subject** is the authenticated Canonical Human Identity whose current
  authority is carried by an interactive request.
- **Administrator** is the R1 Control Plane role. It permits operational and
  policy-management surfaces but does not grant protected-content access.
- **Trusted Authorization Context** contains server-derived identity, scope,
  service principal, purpose and operation for a later access decision.

`User`, `account`, `OIDC subject`, `email` and `current user` must not be used as
synonyms for Canonical Human Identity in contracts or audit records.

## Responsibility and module seams

### Authentication port

The application-facing authentication port exposes one operation conceptually:

```text
authenticate(credentials, diagnostic_context) -> AuthenticatedAlias
```

`AuthenticatedAlias` is frozen, provider-neutral and contains only the exact
issuer, subject and bounded authentication metadata needed by downstream
mapping. It contains no FastAPI request, raw token, JWT header, arbitrary claims,
roles or groups. Provider errors are translated before crossing this seam.

Production uses an OIDC JWT adapter. Development may use a local adapter that
returns one configured alias through the same port. Tests normally use a fake.

### Authorization directory port

The authorization directory resolves one request in one transactionally
consistent snapshot:

```text
resolve_request_authority(
  authenticated_alias,
  requested_scope,
  route_policy,
) -> AuthorizationResolution
```

The result either contains the current Canonical Human Identity, membership,
roles, exact Workspace/Environment and authorization generation, or a bounded
denial category. Callers cannot enumerate records or compose authority from
generic CRUD methods.

An in-memory adapter supports application tests. PostgreSQL is canonical and
uses the existing Unit of Work, RLS and context-bound repository conventions.

### Trusted request-context factory

Only the API composition root owns the request-context factory and the existing
`TrustedContextBoundary`. Route parsing, request bodies and ordinary dependency
injection never receive either capability. The factory combines:

- a validated `AuthenticatedAlias`;
- the exact current authorization resolution;
- one server-validated Workspace and Environment selection;
- one immutable server-declared route policy;
- the existing `DiagnosticContext`.

It emits the same detached immutable snapshot to the application handler and to
the persistence-context issuer. The request-owned source is not reread after
construction.

### Route authorization policy

Every `/api/v1` command/query declares a static policy at registration time:

```text
RouteAuthorizationPolicy:
  scope_kind: workspace | environment
  required_roles: {administrator}
  purpose
  operation
  service_principal_id: optional, server configured
```

Duplicate route registrations, missing policies, empty role requirements,
unknown purpose/operation identifiers or client-selected policies fail startup.
Business modules may only narrow this context with their own current policy;
they cannot widen it.

## Request flow

```text
Bearer access token
  -> OIDC metadata/JWKS adapter
  -> validated Authentication Alias
  -> explicit route Workspace/Environment selection
  -> PostgreSQL identity + membership + role resolution
  -> role/scope decision + safe audit/diagnostic outcome
  -> trusted request context
  -> trusted persistence context
  -> application command/query
  -> later object-level Access Policy checks
```

Authentication, scope selection, role authorization and document authorization
are separate decisions. Passing an earlier decision never implies a later one.

## OIDC JWT access-token contract

### Deployment configuration

OIDC mode requires frozen `extra="forbid"` settings for:

- exact HTTPS issuer URL without query or fragment;
- exact Spine API audience;
- non-empty allowed client ID set;
- explicit asymmetric signing-algorithm allowlist, initially including RS256;
- bounded discovery/JWKS connect/read timeouts;
- bounded fresh-cache TTL and maximum stale-key interval;
- bounded clock skew;
- startup readiness policy.

Production dependencies are declared directly before import. The intended
implementation uses `PyJWT[crypto]>=2.14,<3` for JOSE/JWT validation and an async
`httpx>=0.27,<1` metadata client behind a port. Transitive Cognee dependencies do
not satisfy this requirement.

Issuer URLs are operator configuration, never derived from tokens, request
parameters, email domains or WebFinger input. Production issuer, discovery and
JWKS transport require HTTPS. Tests use an injected transport and clock.

### Discovery and key handling

- Fetch `/.well-known/openid-configuration` for the configured issuer.
- Require the returned `issuer` to equal the configured issuer exactly.
- Require one HTTPS `jwks_uri`; do not follow a redirect to a different origin.
- Reject duplicate/ambiguous keys, missing `kid`, disallowed key use or a key
  incompatible with the configured algorithm.
- Cache validated metadata and public keys only for bounded intervals.
- An unknown `kid` triggers at most one forced refresh for that authentication
  attempt. Refresh failure never falls back to an arbitrary key.
- A still-valid cached matching key may serve during a bounded provider outage;
  once maximum staleness is exceeded, authentication fails unavailable.
- Raw metadata/JWKS provider failures are not returned or logged.

OIDC readiness verifies configuration, exact discovery issuer, compatible
signing algorithms and at least one usable signing key. Startup never mutates
identity or authorization records.

### Token validation

The adapter accepts one `Authorization: Bearer` credential and enforces all of
the following before returning an Authentication Alias:

- compact signed JWT shape and bounded token/header/claim sizes;
- `typ` exactly `at+jwt` or `application/at+jwt`;
- `alg` from the configured asymmetric allowlist; never `none` or an HMAC
  algorithm mixed with public-key material;
- matching `kid` and valid signature from the configured issuer's JWKS;
- exact `iss`, expected `aud`, required non-empty `sub`, allowed `client_id`,
  required `exp`, `iat` and `jti`, and valid optional `nbf`;
- injected current time plus only the configured bounded clock skew;
- human interactive token profile rather than client-credentials/service use.

The access token is treated as opaque by the web client and secret by Spine. It
is never persisted, included in an exception, audit/diagnostic payload, metric,
trace attribute or log. ID tokens are never accepted as API bearer credentials.

Email, name and profile claims are optional display hints only and do not
participate in lookup. Token `roles`, `groups`, `scope` and `entitlements` are
ignored for Spine authorization in R1.

## Canonical identity and authorization records

### Logical records

PostgreSQL owns these logical records behind purpose-specific repositories:

- `CanonicalHumanIdentity`: stable non-zero ID, Workspace, lifecycle status and
  immutable creation provenance;
- `AuthenticationAliasBinding`: exact issuer/subject, Canonical Human Identity,
  version/status and trusted mapping provenance;
- `WorkspaceMembership`: Canonical Human Identity, Workspace, version/status;
- `EnvironmentRoleBinding`: Canonical Human Identity, Workspace, Environment,
  role, version/status;
- `AuthorizationGeneration`: monotonic current generation for a Workspace and
  Environment, advanced atomically with any effective binding change.

Physical tables may normalize immutable definitions and activation history, but
must preserve these observable invariants:

1. Every identity and alias is Workspace-scoped; the same external alias may map
   independently in two Workspaces without creating a cross-tenant identity.
2. One current alias maps to at most one Canonical Human Identity per Workspace.
3. An active Environment role requires an active Workspace membership and an
   Environment canonically owned by the same Workspace.
4. R1 supports only the exact `administrator` role value.
5. Revoked/disabled identity, alias, membership or role fails closed.
6. Changes retain immutable history and advance the authorization generation.
7. Current resolution is one consistent snapshot; it cannot combine identity,
   membership and role rows from different generations.
8. Raw tokens and arbitrary claim sets are never stored.

These records are authorization metadata, not document content grants. The
runtime receives only narrowly scoped reads needed for resolution. There is no
generic cross-tenant list or mutation repository.

### Provisioning boundary

Issue #5 defines storage and trusted read contracts but no runtime provisioning
API. Initial aliases, Canonical Human Identities, memberships and role bindings
are supplied by an explicitly trusted deployment/bootstrap procedure before the
R1 Control Plane is exposed. Tests use dedicated migration-owner seed helpers.

That prerequisite does not grant `read_content`, create Access Policies or
complete the broader ADR 0011 Authorization Bootstrap. A later owning
specification must compose initial identity bindings with policy templates,
service authorities and sealing without weakening these records. JIT creation
from token claims is prohibited.

## Scope and role resolution

- Workspace and Environment selection is explicit route context or immutable
  single-deployment configuration. It is never inferred from token groups,
  issuer hostnames, request bodies or arbitrary headers.
- A client may request a scope, but the server treats it only as a selector and
  proves membership, role and canonical Environment ownership before use.
- Environment identity is the canonical UUID, not the display name or
  `development`/`staging`/`production` kind alone.
- Workspace-only routes reject an unexpected Environment where ambiguity could
  widen scope. Environment routes require both identities.
- Any command object that also contains tenant identities must exactly match the
  trusted request context before handler execution.
- Missing alias, membership, role, Environment, ownership or current generation
  yields one disclosure-safe denial. The response does not reveal which check
  failed or whether the target scope exists.
- The `administrator` role authorizes the R1 Control Plane route only. Protected
  metadata, questions, answers, citations, evidence and content-bearing run
  history still require current Access Policy authorization.

## Trusted request and persistence contexts

The frozen request context contains:

```text
AuthorizedRequestContext:
  acting_subject_id
  workspace_id
  environment_id: optional only for declared workspace-scoped routes
  roles
  authorization_generation
  purpose
  operation
  service_principal_id: optional, server selected
  diagnostic_context
  provenance
```

It excludes raw credentials, issuer claims, request bodies and mutable database
entities. It is request-local and cannot be reused across requests.

The composition root maps it to `TrustedPersistenceContext` as follows:

- scope is the exact Workspace or Workspace/Environment selection;
- origin is `interactive`;
- acting subject is the Canonical Human Identity;
- optional service principal, purpose and operation come only from registered
  route/application policy;
- trace ID is copied from `DiagnosticContext`;
- the existing `TrustedContextBoundary` signs all fields together.

Changing any field, substituting a context, using a foreign boundary, crossing a
request/task or presenting an authorization generation other than the resolved
snapshot fails before repository access. RLS remains defense in depth and never
substitutes for this authorization decision.

Long-running or multi-disclosure business operations must re-resolve current
authorization at their own protected boundaries. A request context is evidence
of one decision, not a lease that survives revocation.

## Development-only local adapter

The local adapter exists only to keep offline local development usable before an
OIDC provider is configured. It has these invariants:

- `authentication_mode=local` is accepted only with an explicit development
  deployment mode; staging/production startup fails closed;
- it emits one configured Authentication Alias and then uses the same canonical
  PostgreSQL mapping, membership and role resolution as OIDC;
- it does not accept user, Workspace, Environment or roles from request headers,
  query parameters, cookies or bodies;
- it cannot mint Canonical Human Identities or bindings;
- its activation is visible in safe readiness/configuration summaries and
  diagnostics without exposing alias values;
- tests verify that changing only a deployment-mode label cannot bypass the
  startup guard through malformed or missing settings.

The local adapter is not a password system, mock production identity provider or
emergency access mechanism.

## FastAPI integration

- Trace middleware establishes `DiagnosticContext` before authentication.
- Public liveness/readiness endpoints declare an explicit authentication
  exemption; no other `/api/v1` route is exempt implicitly.
- A shared dependency extracts one Bearer credential, invokes authentication,
  resolves route scope/role and returns `AuthorizedRequestContext`.
- The dependency completes before any application command/query, Unit of Work,
  Cognee call, file read or model call.
- OpenAPI declares Bearer authentication and required scope parameters without
  exposing provider-specific claims or hand-written frontend types.
- `/api/v1` responses reuse Structured Error v1 and the same `X-Trace-ID`.
- Legacy `/ask` and `/ingest` receive no new R1 authority and retain response-body
  compatibility until their tested replacements exist.

The implementation includes a synthetic administrator-only `/api/v1` route or
equivalent test harness solely to prove composition. Business APIs remain owned
by #3, #22, #23, #25 and #26.

## Error and audit behavior

Stable external categories are:

| Condition | HTTP | Safe category | Retryability |
|---|---:|---|---|
| Missing/malformed bearer credential | 401 | `authentication.invalid` | `never` |
| Invalid signature/type/issuer/audience/client/time/subject | 401 | `authentication.invalid` | `never` |
| OIDC metadata/key service unavailable with no eligible cache | 503 | `authentication.unavailable` | `after_delay` |
| Authenticated but alias/membership/scope/role is not allowed | 403 | `authorization.denied` | `never` |
| Authorization state cannot be established consistently | 503 | `authorization.unavailable` | `after_delay` |
| Unexpected failure | 500 | generic registered internal code | `never` |

All use the Issue #4 Structured Error envelope and trace. No response distinguishes
unknown Workspace, wrong Environment, absent membership, disabled identity or
missing administrator role.

Authentication failures and denials without a trusted tenant/actor emit only a
registry-validated safe Diagnostic Event. Once a canonical actor and safe tenant
scope are established, allow/deny decisions append a registered tenant-scoped
Audit Event containing only canonical IDs, route policy identifiers, bounded
outcome/reason codes, authorization generation and trace. Raw issuer subject,
token data, provider response, route body and protected target are excluded.

If required audit for an allowed protected operation cannot commit, the
operation does not execute. A denied operation remains denied when audit fails.

## Configuration, readiness and lifecycle

- Settings are frozen and validated before network clients or adapters are
  created.
- Importing `spine.auth` performs no network, keychain, database or provider I/O.
- OIDC clients and caches are process-owned, created during startup and closed at
  shutdown.
- Readiness distinguishes invalid configuration, discovery/JWKS unavailability,
  incompatible provider metadata and authorization-store unavailability through
  safe operator codes.
- Liveness does not depend on provider availability.
- Configuration summaries expose mode, issuer origin and freshness state only in
  a sanitized form; no client secret or token is required by this JWT validation
  mode.
- Cache refresh is bounded and concurrency-safe so a missing key does not cause
  an unbounded request stampede.

## Security and disclosure invariants

- Authentication Alias is not authority; current PostgreSQL bindings are.
- Token roles/groups/scopes cannot create a Spine role or Access Principal.
- ID tokens, unsigned tokens, symmetric/public-key algorithm confusion and
  tokens for another API/client are rejected.
- Raw tokens, claims, provider errors and Authentication Alias values never
  enter logs, metrics, trace attributes, public errors or audit payloads.
- Workspace/Environment mismatch is rejected before application execution and
  before opening a context for the wrong tenant.
- No denial reveals existence, name, membership count, role state or Environment
  ownership of another Workspace.
- Request contexts and authorization snapshots do not outlive one request and
  do not make positive authorization caches survive a generation change.
- The local adapter cannot activate in production, accept caller identity or
  bypass canonical mapping.
- Tests, fakes and dependency overrides cannot access the production trusted
  context issuer through a public import or request-controlled dependency.

## User stories

1. As an R1 administrator, I can authenticate with my organization's OIDC
   provider and enter only a Workspace/Environment where Spine currently binds
   my canonical identity to the administrator role.
2. As a security engineer, I can change provider issuer, audience and client
   configuration without changing application/domain contracts.
3. As a security engineer, I know an identity-provider group cannot silently
   become a Spine role or document grant.
4. As an operator, I receive a safe readiness failure when discovery/JWKS cannot
   establish trustworthy verification keys.
5. As an operator, bounded cached keys tolerate a short provider outage without
   accepting an unknown or over-stale key.
6. As an application developer, I receive one immutable request context instead
   of parsing credentials or tenant headers in each use case.
7. As a persistence developer, I can map that same trusted context into the
   existing Unit of Work without weakening its provenance proof.
8. As a test author, I can exercise authentication and authorization offline with
   injected clocks, transports and repositories.
9. As a local developer, I can use one preconfigured local identity without
   installing a fake provider, while production refuses that mode.
10. As a privacy reviewer, I can prove raw tokens and provider identifiers do not
    leak through errors, diagnostics or audit.

## Executable acceptance criteria

AC1. Authentication Alias, authorization resolution, route policy and request
context contracts are frozen, typed and reject extra/malformed fields.

AC2. Domain/application contracts import no FastAPI, SQLAlchemy, PyJWT, httpx,
provider SDK, Cognee or Temporal types.

AC3. Exact issuer/subject identifies an alias only inside the requested
Workspace and cannot confer role or document authority by itself.

AC4. One current alias maps to at most one Canonical Human Identity per
Workspace; duplicate/current conflicts fail migration or command atomically.

AC5. Active role resolution requires active identity, alias, Workspace
membership, Environment ownership/membership and exact administrator binding in
one current authorization generation.

AC6. Revoked/disabled/stale records fail closed and retained history proves what
changed without becoming current authority.

AC7. In-memory and PostgreSQL directory adapters pass the same allow, deny,
cross-workspace and cross-environment contract suite.

AC8. PostgreSQL tables use named constraints, explicit tenant classification,
forced RLS and least-privilege grants; the runtime cannot enumerate another
Workspace's aliases or roles.

AC9. OIDC discovery accepts only exact configured issuer metadata and an
eligible HTTPS JWKS endpoint.

AC10. Token validation rejects wrong `typ`, `alg`, `kid`, signature, issuer,
audience, client, subject, expiration, issued-at and not-before values.

AC11. ID tokens, `none`, HMAC/public-key confusion, duplicate/ambiguous keys and
oversized credentials are rejected before claim mapping.

AC12. Unknown key ID causes at most one bounded refresh; matching fresh/bounded
stale cache and maximum-staleness behavior are deterministic.

AC13. OIDC adapter tests use an injected clock and HTTP transport; the default
suite requires no network or external provider.

AC14. Token role/group/scope/email/name claims never change the authorization
decision for the same canonical PostgreSQL state.

AC15. Scope selection proves canonical Workspace/Environment ownership and role
before application handler, Unit of Work or business adapter execution.

AC16. Missing/unknown/cross-tenant/disabled/unprivileged scope cases share the
same disclosure-safe denial schema and contain none of the protected fixture
corpus.

AC17. AuthorizedRequestContext maps exactly to TrustedPersistenceContext;
acting subject, scope, purpose, operation, service principal and trace mismatch
fails before repository access.

AC18. Context substitution, foreign issuer boundary, mutation attempt,
cross-task use and reuse after request completion fail deterministically.

AC19. Every protected `/api/v1` route has an explicit static authorization
policy; startup fails for an unclassified route.

AC20. Public health/readiness exemptions are explicit and cannot be inherited by
new routers.

AC21. Administrator access and denial through FastAPI are exercised in-process
with `httpx.ASGITransport`, including response schema and `X-Trace-ID`.

AC22. Authentication and authorization failures map to stable Structured Error
codes without raw token, claims, provider messages, database text or target
scope details.

AC23. Eligible allow/deny decisions create registry-valid Audit Events with the
same tenant, actor, generation and trace; untrusted-scope failures emit only safe
Diagnostic Events.

AC24. Required audit failure prevents allowed execution while never converting a
denied request into an allowed one.

AC25. Local mode resolves the same PostgreSQL authority as OIDC and startup
rejects it for staging, production, missing or malformed deployment mode.

AC26. Caller-controlled headers, query/body fields, dependency overrides and
token claims cannot select local identity, roles, route policy or trusted-context
issuer.

AC27. Legacy `/ask` and `/ingest` response bodies remain compatible and are not
declared R1 protected surfaces.

AC28. Direct runtime dependencies are declared in `pyproject.toml`, locked and
imported lazily enough that basic offline commands remain usable.

AC29. Migrations upgrade from every retained revision to one head and verify
identity/authorization constraints, indexes, RLS and grants in real PostgreSQL.

AC30. The full suite, compileall and domain-import boundary pass, and a
provider-neutral conformance harness documents the exact unverified production
issuer prerequisite.

## Test levels

### Domain/application unit tests

- Contract shape, immutability, role vocabulary and route-policy registration.
- Alias/membership/role resolution with in-memory repositories.
- Allowed, denied, disabled, stale-generation and tenant mismatch outcomes.
- Request-to-persistence context mapping and provenance substitution.
- Stable failure categorization and negative disclosure corpus.

### Adapter contract tests

- The same authorization-directory suite against in-memory and PostgreSQL
  adapters.
- OIDC discovery/JWKS/token fixtures through an injected async transport.
- Key rotation, duplicate keys, refresh failure, cache expiry and concurrency.
- Development local-adapter startup matrix.

### PostgreSQL integration tests

- Empty and retained-revision migrations to one head.
- Named constraints, indexes, foreign keys, RLS, grants and runtime immutability.
- Two Workspaces, two Environments and one reused external alias with no
  cross-tenant correlation/disclosure.
- Concurrent binding/current-generation changes and consistent snapshot reads.
- One-connection pool reuse with no Workspace/Environment setting leakage.

### API composition tests

- `httpx.ASGITransport` success, 401, 403, 503, validation and unexpected error
  paths.
- Explicit route-policy coverage and public endpoint exemptions.
- Same trace across middleware, structured response, audit/diagnostic event and
  trusted persistence context.
- Handler-spy proof that every rejected case stops before application execution.
- Legacy endpoint body compatibility.

### Live-provider qualification

A real OIDC provider test is opt-in and never part of the offline default suite.
Before production, the configured issuer must prove discovery, RFC 9068 access
token issuance for the exact audience/client, key rotation and expiry behavior.
The handoff must name the issuer configuration and any behavior not exercised.

## Ticket decomposition

1. **#69 — Establish identity, authorization and route-policy contracts.**
   Framework-independent models, ports, in-memory resolver, failure taxonomy and
   architecture tests.
2. **#70 — Persist canonical identity, membership, roles and generations.**
   Purpose-specific repositories, migration, PostgreSQL adapter, RLS/grants and
   cross-adapter contract suite.
3. **#71 — Validate provider-neutral OIDC JWT access tokens.**
   Direct dependencies, settings, discovery/JWKS client, cache/rotation,
   standards validation and offline provider harness.
4. **#72 — Build trusted request authorization and audit composition.**
   Consistent directory resolution, route policy decision, audit/diagnostic
   behavior and exact TrustedPersistenceContext mapping.
5. **#73 — Enforce authentication and scope in FastAPI with local development
   mode.** Shared dependencies, explicit route classification, OpenAPI security,
   structured failures, local guard and synthetic protected route.
6. **#74 — Qualify the complete Issue #5 foundation.** Migration matrix,
   two-tenant/API composition, leak corpus, compatibility, documentation and
   final AC1–AC30 traceability.

## Dependency plan

```text
#69 ─┬─> #70 ─┐
     └─> #71 ─┼─> #72 ─> #73 ─> #74
#61 ----------┤          ^
#63/#64 ------┘          |
#67 ---------------------┘
```

- #69 can start immediately and does not depend on concrete diagnostics types.
- #70 and #71 can run in parallel after #69.
- #72 requires the diagnostic/structured-error contracts from #61 and the audit
  contract/persistence from #63/#64.
- #73 requires #72 and the FastAPI diagnostic composition seam from #67.
- #74 requires the preceding Issue #5 implementation tickets.
- Issue #8 and the ADR 0011 decision are already complete prerequisites.
- Closing Issue #5 continues to unblock #3, #24, #26 and #27; it does not make
  their business or protected-content authorization complete.

## Compatibility and rollout

1. Land contracts and adapters without changing current legacy route bodies.
2. Apply the identity/authorization migration with the ordinary explicit
   migration procedure.
3. Provision approved initial mappings through the trusted deployment process;
   do not infer them from the first login.
4. Configure and qualify OIDC in development/staging with exact issuer,
   audience and client IDs.
5. Enable protected `/api/v1` routes only after readiness, identity records,
   diagnostics and audit dependencies are available.
6. Keep local mode development-only and visibly reported.
7. Run provider-neutral and real-provider qualification before production.

Rollback disables new `/api/v1` protected routes and returns to the previous
application version without deleting identity history. Database rollback uses a
truthful migration downgrade only when verified; otherwise restore backup or
apply a corrective forward migration. Legacy compatibility paths remain intact
through this rollout but are not accepted production R1 substitutes.

## Deferred decisions

- Exact browser Authorization Code + PKCE library, token storage and session UX.
- Multi-issuer/federation and account-linking workflow.
- SCIM/JIT provisioning and runtime membership/RBAC administration.
- Service/client-credentials authentication and workload identity.
- Team membership and all SourceObject Access Policy evaluation.
- Policy-proposal, independent approval and administrator recovery workflows.
- Authentication assurance (`acr`/`amr`) requirements for later high-risk
  actions.
- Concrete retention period or encryption-at-rest policy for external subject
  identifiers; the broader PII/retention ADR owns that choice.

## Completion criteria

Issue #5 is complete only when:

1. All six implementation tickets are merged and AC1–AC30 have executable
   evidence at their declared test levels.
2. Every R1 `/api/v1` route is either explicitly public or protected by one
   registered route authorization policy before handler execution.
3. OIDC authentication, local development mode and current PostgreSQL role
   resolution all pass the shared provider-neutral suite.
4. Two-Workspace/two-Environment PostgreSQL and ASGI tests prove isolation and
   non-leaking denials.
5. Trusted request, diagnostics, audit and persistence contexts agree exactly on
   actor, scope and trace.
6. Legacy compatibility tests, full pytest, compileall and domain-import gates
   pass.
7. Production handoff names the exact issuer/audience/client prerequisites and
   the result of the opt-in real-provider qualification.
