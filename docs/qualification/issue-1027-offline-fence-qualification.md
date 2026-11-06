# EF-12A offline commit-fence diagnostics and evidence record

This document describes the offline schemas introduced for #1027. They record
evidence references and safe outcomes; they do not enroll a provider, grant
authority, or qualify F2. The production composition remains governed by
`docs/architecture/commit-fence-production-composition.md` and continues to
deny every external or unknown authority.

The typed owner is
`spine.application.commit_fence_qualification`. It reuses the EF-3 provider-port
operation states and failure reasons, and keeps qualification record parsing
and F4 classification at the application boundary. It adds no persistence,
authority registry, provider adapter, or production selection input.

## Diagnostics

`CommitFenceDiagnostic` is a frozen `extra="forbid"` model with schema version
`commit-fence-diagnostic:v1`. Its fields are only opaque UUID references, the
closed fence-mode/outcome/reason enums, and a UTC timestamp. Serialization uses
the explicit `to_operator_payload()` allowlist; it rejects schema subclasses and
contains no arbitrary provider object, exception, message, URL, source metadata
or payload to sanitize after logging. Denial outcomes require a bounded reason
code.

## Qualification record

The full synthetic, unqualified template is
[`issue-1027-offline-fence-template.json`](issue-1027-offline-fence-template.json).
Its record UUID is a synthetic example identifier. Provider and environment
references, evidence, parameters and reviewer decisions are all absent. The
record schema is versioned and closed; all variable text is restricted to
bounded code/version formats, and authority/scope/evidence identities are opaque
references.

| Record fields | External evidence mapped from #983 |
| --- | --- |
| `provider_reference`, `provider_identity` | Authority/provider identity and accountable authority owner; must identify the approval/terms authority, not a source-data connector. |
| `provider_contract_version`, `provider_contract` | Exact authoritative contract, service/API version, evidence reference and permit/revocation semantics. |
| `environment_reference`, `environment` | Named qualification environment, deployment boundary, runtime identity and provider configuration reference. |
| `authority_binding` | Exact issuer, subject, scope, workspace, environment, purpose and action, each represented by an opaque reference or bounded code and tied to evidence. |
| `permit_acquisition_release`, `revocation_acknowledgement_order` | Acquire/release behavior and documented commit-permit versus revocation-acknowledgement ordering, reviewed by Security. |
| `expiry_renewal_guarantees`, `stale_holder_generation_semantics` | Provider expiry/renewal rules and stale-holder/generation behavior. |
| `clock_uncertainty`, `transaction_commit_bounds`, `parameters` | Provider-approved clock model, uncertainty, safety margin, permit lifetime, maximum transaction duration, renewal support/interval, request timeout and retry bound. Every numeric parameter is `null` until provider/environment evidence supplies it; the verified form requires a value and evidence reference. |
| `timeout_retry_recovery` | Operations and integration evidence for outage, partition, cancellation, lost responses, unknown commits, canonical reconciliation and fresh-permit retry. |
| `provider_conformance_results` | Immutable provider- and environment-specific conformance run references, including both commit/revocation race orders and failure/recovery cases. Synthetic tests and PostgreSQL F1 races do not satisfy this field. |
| `adapter_version`, `composition_version`, `adapter_composition_evidence` | Exact provider adapter and evidence-issued composition version/scope, with proof that unqualified providers remain denied. This template does not add an enrollment path. |
| `reviewer_decisions` | Actual, separately attributed Provider/Authority, Security, Operations, Application/Integration and decision-owner decisions with evidence references and UTC decision times. The template explicitly lists each role as `NOT_PROVIDED`. |
| `remaining_unsupported_scopes` | Evidence-backed scopes that remain F4/DENIED after any future narrow qualification. The template marks this list `NOT_PROVIDED`; an empty tuple is never treated as proof that no unsupported scopes remain. |
| `qualification_status` | Fixed to `F2_NOT_QUALIFIED`; the schema has no `F2_QUALIFIED` value. |

Evidence status values are `NOT_PROVIDED`, `NOT_VERIFIED`, `VERIFIED`,
`EXPIRED`, `AMBIGUOUS`, `SYNTHETIC_ONLY` and `UNSUPPORTED`. A `VERIFIED`
evidence pointer requires an opaque evidence reference. Parameter and binding
sections also enforce their own completeness rules. No status is accepted as an
authority grant.

## Outcome and status meanings

- `OFFLINE_SCHEMA_VERIFIED` means only that the versioned local schema and its
  serialization/negative tests passed.
- `PROVIDER_NOT_VERIFIED` means actual provider identity, contract, or ordering
  evidence is absent or unverified.
- `ENVIRONMENT_NOT_VERIFIED` means the named qualification environment and its
  evidence are absent or unverified.
- `F2_NOT_QUALIFIED` is the only qualification status a record can carry,
  including a fully populated offline record.
- `F4_DENIED` is the evaluator outcome for all records in this offline module.
  Missing, expired, ambiguous, unsupported, incomplete, synthetic-only, or
  unknown-class evidence remains denied. Unknown schema versions and extra
  fields fail parsing and become a content-free F4 denial.

`parse_f2_qualification` accepts only a plain mapping matching the closed
record schema. It never returns validation messages, input fields or provider
payloads. `evaluate_f2_qualification` accepts the typed record and an injected
UTC evaluation time; it cannot return a grant or a successful F2 state. The
record is not an `ExternalCommitFencePort` and production composition accepts
only its existing exact F1 source types.

To begin actual provider qualification, an owner must supply the exact evidence
and role-specific decisions listed in [#983](https://github.com/alkselsv/spine/issues/983).
No names, numeric defaults, reviewer decisions or provider guarantees are
invented by this offline template.
