# Governed Q&A API and worker runtime

The `spine-api` process and `spine-qa-worker` process share the strict
`spine-production-runtime-settings:v1` profile from
`SPINE_QA_DEPLOYMENT_SETTINGS_JSON`. The API admits Ask requests into canonical
PostgreSQL with the idempotency receipt and outbox intent. Its Temporal client
and `DurableAskComposition` use the existing Ask workflow contract; the API does
not answer inline. The worker registers that workflow and existing executor on
the profile's namespace and task queue.

The API hosts the existing bounded, authenticated capture channel. The API
observer registers before admission, and the worker publishes only its matching
execution capture through its separate workload credential. Capture is ephemeral
and is not workflow state. The API and worker compare release, commit, database,
authority, Temporal, capture, projection and model bindings at startup. Errors
expose bounded reason codes only.

For disposable local Compose use, copy the commented Q&A variables from
`.env.example` into `.env`. Set one release identity and commit SHA for both
processes, set `SPINE_QA_MODEL_PROFILE_ID` to a stable deployment profile
identity distinct from the provider model name, use the current Alembic head for
`migration_revision`, and update the profile fingerprint whenever any binding
changes. Generate distinct random worker and observer credentials. Keep the
deployment JSON free of secrets. The example identities and fingerprint are
placeholders, not deployment approval.

Use a unique Compose project name for every SOURCE qualification (for example,
`docker compose -p spine560 ...`). Compose volumes outlive containers, so reusing
a project name can silently select an older PostgreSQL or Neo4j state instead of
the declared isolated target. Supply the lease, handler and shutdown bounds as
one profile; startup rejects a handler that reaches the lease or a shutdown
bound that exceeds it.

Provision the disposable qualification authority through the explicit operator
profile before starting the runtime:

```bash
docker compose -p spine560 --profile qualification-provisioning run --rm \
  qualification-authority-provisioner
docker compose -p spine560 up --build --detach --wait
```

The provisioning command uses the migration-owner credential, creates the
versioned synthetic allow/deny identities, hard-expiring Qualification Authority
Bindings, one exact SOURCE Qualification Target Binding and its synthetic
metadata-only AccessPolicy reference, and is idempotent for the same inputs. It
is never a dependency of API or worker startup. Default startup runs only
`qualification-authority-verifier` through the runtime database role and fails
closed when records are missing, expired, revoked or do not match the exact
target references. Set `SPINE_QUALIFICATION_AUTHORITY_EXPIRES_AT` explicitly for
each run; changing authority or target inputs requires a fresh isolated Compose
project rather than mutating immutable fixture history.

The worker exposes content-free `/health/live` and `/health/ready` endpoints on
port 8081. Readiness separately reports PostgreSQL, Temporal reachability,
worker polling and whether a real application execution has completed in this
process. A healthy Temporal server or a polling worker alone does not claim that
Ask completed. The API retains its authenticated readiness endpoint and performs
bounded PostgreSQL authorization and schema checks before serving.

Human API authentication is selected by `SPINE_API_AUTHENTICATION_MODE`. Use
`oidc` with the strict `SPINE_OIDC_*` settings for a production authority. The
`local_development` adapter is accepted only when its configured deployment mode
is `development`; the profile records this mode and API startup rejects a
mismatch. Ingestion's service-principal alias is configured separately from the
human authentication provider.

This runtime startup does not provision identities or grants, approve a provider,
make paid provider calls, or qualify a physical deployment. The separate
operator profile is the only local path that provisions qualification fixtures. The
existing real Temporal/PostgreSQL integration suite remains the evidence for
canonical terminal completion; deployment-specific grants and credentials must
be supplied by their owners.
