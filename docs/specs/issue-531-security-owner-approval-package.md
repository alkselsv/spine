# Issue #531 — approved Security qualification contract

**Status: APPROVED ARCHITECTURAL CONTRACT — PROJECT PARTICIPANT DECISION**

The project participant recorded agreement to Security-owned qualification provisioning and S1–S8 in [PR #540](https://github.com/alkselsv/spine/pull/540#issuecomment-6326615206). This is not a formal GitHub Review and does not claim that the Security Owner personally reviewed or approved the PR.

This decision permits contract documentation, decomposition and implementation through repository workflow. It does not authorize provisioning any concrete identity, membership, grant, credential, database role, SOURCE/RESTORE target, backup, restore or canary.

## Approved contract

Use a bounded one-shot operator-invoked provision/revoke/cleanup interface. Require an independently authenticated operator, independently verified operation/change approval, and a strict versioned target profile. Do not create a general identity API, use #508 bootstrap as the operator entrypoint, or provision during API/worker/dispatcher startup.

Reuse canonical PostgreSQL identity, alias, membership, environment role, service-principal, authorization generation, audit, AccessPolicy and RLS contracts. Create or select only the principals and least-privilege bindings required by the approved profile. Human `read_content` and service `process_content` remain distinct. Content grants may only be created and activated through the existing approved AccessPolicy workflow; the provisioning interface may verify a grant reference but cannot manufacture or activate it.

SOURCE and RESTORE must use separate identity bindings, aliases, credentials, database roles and operator scopes. The restore verifier is independent and read-only. No SOURCE credential is copied to RESTORE. Backup/restore authority does not imply `read_content`.

Use inactive-first provisioning, idempotent operation identity/digest, recoverable partial failure, and explicit revoke/cleanup. Binding activation, authorization generation changes, and immutable audit evidence commit atomically. Required audit failure prevents activation. Canonical denial on revocation/expiry remains effective if external secret revocation or cleanup is delayed.

## S1–S8 decisions recorded

- **S1 — Interface:** approve a new bounded one-shot operator command/job with provision, revoke and cleanup operations; no startup or public API provisioning.
- **S2 — Operator authority:** require operator authentication and independent validation of the change/operation approval outside request-controlled values. The concrete operator channel, approver identity and approval reference are run-specific prerequisites.
- **S3 — Principals and grants:** use canonical human and service identities, aliases, scoped memberships and roles; add only a qualification-scoped binding if existing contracts cannot express the required temporary service authority. Admin does not imply content authority. AccessPolicy is the only source of content grants.
- **S4 — Expiry/revocation:** approve hard expiry option A. A canonical binding deadline is checked against database time on every applicable human and service authority resolution; at the deadline it is expired. Reaper and external secret expiry are supplementary only. Revocation atomically denies authority, records audit, and advances authorization generation. Durable workflows revalidate before every protected continuation and cannot renew or extend expired authority.
- **S5 — SOURCE/RESTORE:** separate principals, aliases, credentials, database roles, runtime bindings and operator scopes; verifier is independent/read-only; no credential copying.
- **S6 — AccessPolicy/RLS:** keep both enforcement layers enabled, independent and tested with allow and deny cases under restricted database roles.
- **S7 — Secrets:** use an external secret provider and keep values out of GitHub, logs, audit, arguments and reports. Provider selection and target/process-specific references and rotation are operational/configuration prerequisites; no provider is selected by this contract.
- **S8 — Evidence:** keep deterministic and PostgreSQL implementation evidence separate from target-bound live evidence and physical recovery qualification; require independent review for live readiness.

## Expiry and ADR follow-up

Canonical identity, alias, membership and role records do not currently have a general expiry timestamp. Hard expiry is an agreed change to canonical authorization behavior and requires a bounded schema representation plus resolver changes that cover human and service authority. Compare the immutable deadline to database time in the authority-resolution transaction; equality means expired. S-01 must inventory all human and service authorization resolution and consumption paths, including service-principal consumers that bypass `PostgreSQLAuthorizationDirectory`, before claiming complete coverage. Admitted durable work must obtain fresh authority at each activity/action boundary before every later protected read/action and stop protected work after expiry/revocation. Safe cancellation and cleanup may continue without expired authority.

Do not edit accepted ADR-0021 silently. Before schema or resolver implementation, record a separate ADR that explicitly extends the canonical authorization contract with qualification-binding expiry, complete human/service resolution-path coverage, database-time semantics, revocation/generation behavior and durable-work revalidation at activity/action boundaries. The ADR follow-up does not reopen the agreed decision to enforce hard expiry.

## Why #508 is not reusable as the provisioning interface

`spine-qualification-bootstrap` is limited to #508's disposable development qualification. It creates an administrator identity for its configured scopes and relies on migration/test-only seed behavior. It does not provide separate SOURCE/RESTORE principals, temporary service authority, hard expiry, revoke/cleanup or independent target-specific operator approval. Reuse its validation/test patterns only; do not extend its privilege or point it at #509 targets.

## Implementation and operation boundaries

The contract can be decomposed into canonical expiry/ADR/schema/resolver, bounded operator lifecycle, external-secret and SOURCE/RESTORE binding, and security integration-test slices. #531 remains the integration/qualification parent; #509 remains the physical backup/restore qualification issue. #529/#530 deterministic implementation can proceed independently.

Run-specific prerequisites still need separate authorization: exact target/profile inventories, Workspace/Environment scope, operator and approval references, deadline/run window, selected external secret provider and secret references, database roles, synthetic sources and policy references, cleanup owner, and evidence reviewer. Do not put secrets or sensitive identifiers in GitHub.
