# Issue #167: frontend foundation browser qualification

## Scope

The qualification uses the built SPA in a real Chromium browser. Playwright
intercepts the generated client's readiness request with a typed synthetic
boundary and the chat route uses the existing Spine-owned `ChatTransport` with a
controlled API/SSE adapter.

The fixture is deliberately not an implementation of Issue #26. It does not
contact FastAPI, Cognee, Temporal, a model provider, a database or an identity
provider. The production chat adapter remains a later consuming step.

The controlled command always builds the current frontend sources before
Playwright starts a run. Playwright owns a strict Vite preview server at
`http://127.0.0.1:4175` by default; `SPINE_CONTROLLED_PREVIEW_PORT` can select a
different valid TCP port. Existing listeners are never reused, so a collision
fails before browser tests execute. The deployed-infrastructure qualification
remains isolated in `playwright.production.config.ts` and does not use this
preview server or the controlled fixtures.

## Qualified journeys

- administrator shell navigation preserves Workspace/Environment in the URL;
  reload and direct chat links restore the same scope;
- the generated client receives the injected bearer credential and sends both
  scope headers; a denied cross-scope response is rendered as a safe outcome;
- the injected administrator boundary denies unauthenticated browser sessions
  before the shell or API is reached;
- the controlled chat journey restores a session, renders the canonical stored
  message, submits through the transport, reconnects after a stream break, and
  does not render the streamed delta as the final answer;
- cancellation and replay use the transport's idempotent command cache and do
  not create a duplicate canonical message.

Unit/component tests continue to cover the complete shared state vocabulary:
`loading`, `empty`, `partial`, `stale`, `degraded`, `permission_denied`,
`validation_failed`, `failed` and `ready`. The browser qualification covers the
states exposed by this foundation slice: loading, ready, reconnecting,
permission denied, cancellation and terminal canonical rehydration.

## Verification

From the repository root:

```bash
pnpm web:api:check
pnpm web:typecheck
pnpm web:build
pnpm web:test
pnpm web:test:e2e
```

CI installs Chromium and its system dependencies before the last command. No
external service availability is required by the controlled fixtures.
