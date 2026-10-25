# R1 governed Q&A browser qualification

This runbook executes Issue #508 only against an operator-approved isolated
deployment whose Q16-01 readiness and Q16-02 projection qualification pass.
For the repository-owned disposable Compose deployment, an explicit one-shot
bootstrap creates only the configured synthetic principal and its two isolated
workspace/environment authority graphs. API startup never provisions authority.

## Inputs

Supply these values out of band. Do not paste their values into issues, logs or
committed files.

The Playwright production config loads the gitignored repository-root `.env`
and overlays `.env.e2e`; values already exported by the operator environment
take precedence. Runtime identity and scope remain single-sourced in `.env`;
approved browser inputs and evidence destinations live in `.env.e2e`.

- `SPINE_E2E_WEB_BASE_URL`: deployed Web origin serving the candidate build;
- `SPINE_LOCAL_AUTH_BEARER_CREDENTIAL`: short-lived access token for the existing
  administrator principal;
- `SPINE_LOCAL_AUTH_CONFIGURATION_VERSION`: safe non-secret principal/auth generation;
- `SPINE_WORKSPACE_A_ID`, `SPINE_ENVIRONMENT_A_ID` and
  `SPINE_WORKSPACE_B_ID`, `SPINE_ENVIRONMENT_B_ID`: two distinct,
  authorized test scopes;
- `SPINE_E2E_DOCUMENT_PATH`: permitted synthetic or approved anonymized `.txt`,
  `.md`, `.markdown`, `.pdf`, `.docx` or `.xlsx` file outside Git;
- optionally `SPINE_E2E_DOCX_DOCUMENT_PATH`: permitted synthetic `.docx` file
  outside Git for the focused UI → upload → parsing → projection regression;
  when absent, only that focused test is skipped;
- `SPINE_E2E_ANSWERABLE_QUESTION`, `SPINE_E2E_UNANSWERABLE_QUESTION` and
  `SPINE_E2E_CANCEL_QUESTION`: approved test inputs;
- `SPINE_E2E_QUALIFICATION_RUN_ID`, `SPINE_E2E_COMMIT_SHA`,
  `SPINE_E2E_DEPLOYMENT_FINGERPRINT`, `SPINE_E2E_WEB_RELEASE_IDENTITY`,
  `SPINE_RUNTIME_RELEASE_IDENTITY` and
  `SPINE_E2E_PROVIDER_PROFILE_IDENTITY`: candidate metadata;
- optionally `SPINE_E2E_REPORT_PATH`; by default the committed qualification
  report path is updated.

The token is passed to the same browser runtime identity interface used by the
SPA. It is retained behind an in-memory callback and is not placed in a URL,
storage or result artifact. Workspace/environment values remain selectors;
the server independently authenticates and authorizes every request.

## Execute

For the repository-owned disposable deployment, build and wait for the whole
Compose dependency graph first:

```bash
docker compose --env-file .env --env-file .env.qualification \
  --profile qualification-provisioning run --rm qualification-authority-provisioner
docker compose --env-file .env --env-file .env.qualification \
  up --build --detach --wait
docker compose --env-file .env --env-file .env.qualification ps --all
curl --fail http://127.0.0.1:4175/api/v1/health/live
```

The Compose profile applies the canonical PostgreSQL migrations, bootstraps the
explicit disposable qualification authority and starts the pinned PGVector,
Temporal and Neo4j/APOC services before API and Web. It binds published ports
to loopback and reads runtime secrets from `.env` plus explicitly bounded
qualification authority/provider authorization from `.env.qualification`. The approved
Issue #508 input directory is mounted read-only. The API composes the durable
ingestion adapters and the `outbox-dispatcher` consumes their transactional
outbox against the same PostgreSQL deployment. A healthy container graph is
necessary but not sufficient: the browser report remains BLOCKED until the
complete browser journey produces all required correlated terminal evidence.

From `web/` run:

```bash
corepack pnpm test:e2e:production
```

The production config has no `webServer`, route interception, retries, trace,
screenshots or video. A failure writes a bounded safe reason code; inspect
server-side safe trace references under the authorized operator procedure
rather than enabling content-bearing Playwright artifacts.

The duplicate/retry scenario injects one browser-side response loss only after
the real admission response has succeeded. It does not mock or fulfill an API
route; the retry reaches the same deployed admission endpoint and must return
the persisted replay for the original idempotency key.

## Review

Accept the record only when it reports `pass`, every required scenario appears,
the citation locator validation is `pass`, correlated IDs resolve in the
authorized API/Temporal/PostgreSQL diagnostics and the recorded release
fingerprints match the deployed candidate. A `blocked` or `not_verified` record
does not qualify Issue #508.
