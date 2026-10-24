# Issue #578 — projection publication fault injection

This qualification seam exists only to collect the two missing deployed
observations required by #562. It does not define a second publication path.
The ordinary successor command still resolves a canonical boundary, writes the
physical projection, validates and seals its candidate, assembles an immutable
snapshot and advances the PostgreSQL active pointer with CAS.

Injection is disabled when
`SPINE_PROJECTION_PUBLICATION_FAULT_PROFILE_JSON` is unset or empty. Repository
Compose passes the variable only to `outbox-dispatcher`. An enabled profile is
accepted only when its commit SHA, deployment fingerprint and Cognee provider
profile digest exactly match the independently loaded runtime bindings and the
deployment profile declares `local_development`. The profile additionally binds
one Workspace, Environment, projection kind and server-generated successor
command identity. Mismatch fails closed before physical provider I/O and emits
only the bounded code `projection.qualification.binding_rejected`.

The only supported boundaries are:

- `before_physical_validation`, with reason
  `projection.qualification.before_physical_validation`;
- `after_validation_before_activation`, with reason
  `projection.qualification.after_validation_before_activation`.

Each configured boundary is consumed through the tenant-scoped PostgreSQL
idempotency ledger before its failure is raised. The same transaction appends a
`command.outcome` audit event carrying only the bounded reason code; its opaque
audit ID is attached to the in-process injected-fault outcome. The first delivery
therefore fails with the configured safe reason; a retry or worker restart
observes the durable receipt and continues instead of injecting the same fault
again. No
request content, provider payload, credential, network location or raw exception
is stored in the profile or returned in status.

Example shape (replace placeholders only with the approved disposable SOURCE
deployment's non-secret references):

```json
{
  "schema_version": "spine-projection-publication-fault-profile:v1",
  "profile_id": "qualification-issue562",
  "profile_version": "v1",
  "commit_sha": "<40 lowercase hex characters>",
  "deployment_fingerprint": "<64 lowercase hex characters>",
  "provider_profile_digest": "<64 lowercase hex characters>",
  "workspace_id": "<isolated workspace UUID>",
  "environment_id": "<isolated environment UUID>",
  "projection_kind": "qa",
  "successor_command_identity": "successor-<32 lowercase hex characters>",
  "rules": [
    {
      "boundary": "before_physical_validation",
      "reason_code": "projection.qualification.before_physical_validation"
    },
    {
      "boundary": "after_validation_before_activation",
      "reason_code": "projection.qualification.after_validation_before_activation"
    }
  ]
}
```

## #562 operator procedure

1. Pin the exact merged commit, deployment profile/fingerprint, provider profile
   digest and disposable Workspace/Environment. Complete the ordinary readiness
   checks before enabling a fault.
2. Stop the dispatcher so the accepted successor cannot be claimed before its
   command identity is known. Create the successor through the supported control
   API and read its safe command status; do not query or edit PostgreSQL directly.
3. Build the profile with that command identity and the approved boundary rule.
   Set the environment variable and force-recreate only `outbox-dispatcher`.
   A startup binding failure invalidates the attempt; do not weaken the profile.
4. Start delivery and observe the first bounded retryable outcome through the
   existing command/delivery status and retain the safe audit correlation. For
   `before_physical_validation`, verify
   that the candidate is not sealed and the previous active publication remains.
   For `after_validation_before_activation`, validated immutable evidence may be
   retained, but the active snapshot and activation generation must remain the
   previous values.
5. Allow the normal retry, or restart the dispatcher and redrive through the
   supported successor command. The durable one-shot receipt prevents a second
   injection. Record only safe command, run, candidate, receipt, snapshot and
   activation correlations. Confirm one logical successor result and unchanged
   quarantined delivery rows.
6. For the second boundary, create a new successor command and update the profile
   to its exact command identity. Never reuse an old profile for a changed
   canonical head or a different Workspace/Environment.
7. Cleanup: stop the dispatcher, unset
   `SPINE_PROJECTION_PUBLICATION_FAULT_PROFILE_JSON`, force-recreate the
   dispatcher and verify ordinary readiness. Remove the disposable deployment
   only through its existing teardown procedure. Do not mutate provider state,
   candidate rows, activation pointers or idempotency receipts.

The #562 report must pin the resulting evidence to the exact commit and runtime
bindings. Offline tests prove the contract but cannot substitute for these two
deployed observations.
