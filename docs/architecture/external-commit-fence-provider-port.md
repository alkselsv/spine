# External commit-fence provider port

Status: contract defined; no provider is qualified.

This document describes the application port in
`spine.application.commit_fence_provider_port`. Its versioned results bind every
operation to the exact `CommitFenceBinding` from EF-1 and record the provider
contract version. The port is not an authority registry, does not enroll a
provider and does not make an unsupported authority trusted.

## Operation contract

| Operation | Successful result | Required behavior |
| --- | --- | --- |
| `acquire(binding)` | `ACQUIRED` with opaque permit and bounded expiry | Acquire only for the exact binding and within its validity window. |
| `renew(permit)` (optional capability) | `RENEWED` with the same binding and a bounded expiry | Renew only while provider rules permit; ambiguous renewal denies commit and requires abort/reconciliation. |
| `validate_before_commit(permit)` | `VALID` with the same binding and still-live permit | Check immediately before commit. Caller must still enforce a bounded database transaction and conservative clock margin. |
| `finish(permit)` | `RELEASED` with the released opaque reference | Complete only after the commit outcome is known. A lost response is not proof of release. |
| `abort(permit)` | `RELEASED` with the released opaque reference | Roll back the consumer transaction before releasing. A lost response is not proof of abort or release. |

The provider guarantee needed for F2 is stronger than a lease timestamp: after a
revocation acknowledgement, no incompatible commit protected by a live permit
can succeed. The provider must serialize acknowledgement behind completion or
expiry of every conflicting permit. A provider-specific, written contract and
evidence of this ordering are required before qualification. TTL checks, repeated
epoch reads, local simulation and mocks do not establish it. No such provider
evidence is currently recorded; providers therefore remain unsupported/F4 and
production use remains denied.

## Failure states

Every state other than the operation's exact success state denies progress.
`AMBIGUOUS` and `INDETERMINATE` preserve the uncertainty reason but grant no
permit. Unknown state or provider behavior maps to `UNSUPPORTED`/F4.

| Condition | Port result | Consumer response |
| --- | --- | --- |
| Provider denies or is unavailable | `DENIED` (unavailability may be reported as indeterminate with `PROVIDER_UNAVAILABLE`) | Do not commit; roll back any open transaction. |
| Holder/epoch no longer current | `STALE_HOLDER` | Do not commit; discard the stale permit. |
| Binding or permit expired, including equality at expiry | `EXPIRED` | Do not commit; resolve authority and acquire a fresh permit for a retry. |
| Acquire/renew/release response is lost | `AMBIGUOUS` with `LOST_RESPONSE` | Treat the outcome as unknown; do not commit or retry under the old permit. Reconcile by provider reference and idempotency key, then obtain a fresh permit. |
| Provider clock bounds cannot be trusted | `INDETERMINATE` with `CLOCK_UNCERTAIN` | Deny commit and abort safely. Never widen the lease using a guessed clock margin. |
| Revocation acknowledgement is waiting for live holders | `INDETERMINATE` with `REVOCATION_ACKNOWLEDGEMENT_PENDING` | Do not claim revocation is complete. Keep the operation bounded and wait only under a provider-approved protocol. |
| Provider protocol/version is unsupported or unknown | `UNSUPPORTED` | F4: deny. Do not register or infer trust from matching binding fields. |
| Finish or abort has an uncertain outcome | `AMBIGUOUS` / `INDETERMINATE` | Roll back before release when aborting; reconcile commit status canonically before any retry. |

The application port cannot itself prove the provider's revocation ordering. A
future named-provider qualification must retain the provider's written contract
version, ordering evidence, expiry/renewal semantics, clock model, stale-holder
behavior and lost-response recovery. Until then, this interface is a typed
boundary only.
