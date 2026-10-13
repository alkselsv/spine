# OpenAPI client handoff

`web/api/openapi.json` and `web/api/generated/` are generated artifacts and must
not be edited by hand. `web/api/codegen/export_openapi.py` exports the canonical
document from `spine.api.main.create_app`; `openapi-typescript` 7.13.0 generates
the request, response and operation types consumed by `openapi-fetch` 0.17.0.
The generator runs in an isolated workspace on TypeScript 5.9.3, while the web
application keeps its TypeScript 7 compiler. The exporter runs through `uv run`
so CI uses the repository's locked Python environment.

Every protected R1 operation has a stable server-owned `operationId`, HTTP
Bearer security, and required UUID headers named `X-Workspace-ID` and
`X-Environment-ID`. A generated client must expose both scope headers as
required inputs and apply the bearer credential through its authentication
configuration; identity, roles and route policy are never request fields.

The backend compatibility test in `tests/api/test_authorization.py` verifies
these generator-facing OpenAPI fields. `web/src/api/client.ts` creates the typed
client with an injected credential and immutable Workspace/Environment scope;
`web/src/api/query.ts` is the TanStack Query boundary for cancellation,
freshness and retry policy. Its generic path transform removes only the two
injected scope headers from operation inputs, so later generated protected
resources do not make authorization context caller-controlled.

Run generation and the same no-diff check used by CI from the repository root:

```bash
pnpm web:api:generate
pnpm web:api:check
```

CI exports the backend schema again, regenerates TypeScript and fails if either
committed artifact differs. A backend contract change therefore updates both
files through the command above, never by editing generated output.

The client exposes a frozen request-only surface, rejects per-request transport,
header, middleware and cross-origin overrides before requesting a credential,
forces redirects to fail, and sends credentials only in the `Authorization`
header. Structured failures are reduced to stable code, safe message,
retryability, trace ID and a frontend category; unknown or transport failures
use safe frontend-owned fallbacks.
