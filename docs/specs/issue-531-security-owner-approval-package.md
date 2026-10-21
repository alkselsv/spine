# Issue #531 — Security Owner approval package

**Status: PROPOSED — SECURITY APPROVAL REQUIRED**

This package requests an architecture/interface decision only. It does not authorize provisioning any concrete identity, membership, grant, credential, database role, SOURCE/RESTORE target, backup, restore, or canary.

## Problem

Spine has canonical identity resolution and authorization enforcement, but no identified Security-owned operator interface for temporary synthetic qualification principals on independent SOURCE and RESTORE targets. Current identity/membership records have status and history but no built-in expiry. #508 bootstrap, local bearer authentication, and the migration/test seed helper do not meet this boundary.

## Recommendation

Choose bounded option B if Security cannot name an existing approved mechanism: a one-shot operator-invoked, qualification-only provision/revoke/cleanup command or owner-managed job. It should consume a strict versioned approved target profile plus an independently verified operation/change approval reference; reuse canonical identity, membership, role, AccessPolicy, audit, generation, and RLS contracts; keep SOURCE and RESTORE principals, credentials, database scopes, and operators separate; and never run during application startup.

The proposed expiry model is a canonical deadline checked on every relevant authority resolution, with AccessPolicy valid_until for content grants and scheduled cleanup/external credential expiry as additional defenses. This requires Security approval because the current identity/membership schema has no expiry enforcement.

## Why #508 is not sufficient

spine-qualification-bootstrap is limited to Issue #508's disposable development deployment. It creates one administrator human identity across two configured Workspace/Environment scopes. It does not create independent SOURCE/RESTORE identities, service-principal lifecycle, source-specific content grants, guaranteed expiry, revoke/cleanup, or target-specific operator authority. It uses migration database credentials and the migration/test seed helper. Do not extend its privilege or point it at #509 targets implicitly.

## Proposed boundary

- Operator-authenticated, independently approved invocation; strict versioned target/profile; no arbitrary database/path/identity selectors or inline secrets.
- Canonical human and required service bindings, scoped memberships and environment-local roles; admin is not read_content.
- read_content and process_content are distinct and are approved only through the existing AccessPolicy workflow.
- Separate SOURCE, RESTORE, verifier, API, worker, dispatcher, migration, backup, and restore authority where each is needed.
- Inactive-first activation; idempotent operation identity/digest; atomic canonical binding/generation/audit transaction; secret-provider failure leaves bindings inactive; revoke denies canonical resolution before external secret cleanup.
- Immutable, redacted evidence; credentials stay in an approved external secret mechanism.
- AccessPolicy and PostgreSQL RLS are both tested and remain enabled.
- Deterministic tests, disposable PostgreSQL integration, and live target qualification are separate evidence levels.

## S1–S8 decisions requested

| ID | Decision requested | Recommendation |
|---|---|---|
| **S1** | Name an existing Security-owned provisioning interface or approve a bounded new interface. | Verify reuse first; otherwise approve a one-shot operator boundary, not a general identity API. |
| **S2** | Identify eligible operator/job and how independent approval is verified. | Named operator or owner-managed job plus approved change/operation reference. |
| **S3** | Approve canonical human/service principal, alias, membership, role, and grant shapes. | Separate principals per target; least privilege; content grants remain policy-workflow-owned. |
| **S4** | Choose hard expiry enforcement and revoke/cleanup semantics. | Canonical deadline checked during authority resolution; scheduled revoke and secret expiry supplement it. |
| **S5** | Approve SOURCE/RESTORE identity, credential, database-role, network and operator separation. | Fully separate target bindings; verifier is independent and read-only. |
| **S6** | Confirm AccessPolicy and RLS must be verified together. | Require both allow and deny cases under restricted database roles. |
| **S7** | Name approved external secret provider and per-process access/rotation/expiry behavior. | Use existing approved provider or obtain separate approval before selecting one. |
| **S8** | Define implementation-readiness evidence separately from live deployment evidence. | Security decision/spec before implementation; tests before code readiness; target-specific evidence plus independent review before deployment readiness. |

Open design points requiring explicit Security response include: whether an existing mechanism is acceptable; operator authentication/approval source; canonical service-principal lifecycle; expiry schema and database-time semantics; handling already-admitted durable work at expiry; target role inventory; secret provider; and evidence reviewer.

## What approval unlocks

Security approval of S1–S8 would settle the bounded contract and allow it to be decomposed into implementation work. It would permit deterministic and PostgreSQL contract implementation after the approved boundary is recorded. It does not create or approve any real principal or grant.

Run-specific approval remains separate: exact target inventory/profile, operation/change reference, allowed synthetic source/action, secret references, external provider access, database roles, target isolation, run window, cleanup owner, and independent reviewer must be authorized for each operation. No live qualification is included here.

## Approval response

Please reply with one of:

- APPROVED
- APPROVED WITH CHANGES
- REJECTED / ALTERNATIVE

List the S1–S8 decisions accepted or changed and provide a non-sensitive approval reference. Do not post credentials, target secrets, private source identifiers, or policy payloads.

An architecture approval is not permission to provision specific SOURCE/RESTORE identities or grants. Each concrete operation requires separate operator authorization.
