# OpenAPI client handoff

`web/api/generated/` is reserved for generated TypeScript types and clients and
must not be edited by hand. Until the first business endpoint introduces the web
toolchain, the backend OpenAPI contract is the generation handoff.

Every protected R1 operation has a stable server-owned `operationId`, HTTP
Bearer security, and required UUID headers named `X-Workspace-ID` and
`X-Environment-ID`. A generated client must expose both scope headers as
required inputs and apply the bearer credential through its authentication
configuration; identity, roles and route policy are never request fields.

The backend compatibility test in `tests/api/test_authorization.py` verifies
these generator-facing OpenAPI fields. Once the selected TypeScript generator
is installed, CI must regenerate `web/api/generated/` from the same schema and
fail on a diff.
