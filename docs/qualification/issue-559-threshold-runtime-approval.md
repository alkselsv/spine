# Issue #559 threshold runtime and approval qualification

## Contract version and migration

The existing unversioned threshold policy/evidence pair is treated as v1 for
migration. It remains parseable and deterministic, but it cannot produce a
positive release decision. `ThresholdPolicyV2` and `ThresholdEvidenceV2` add an
exact runtime binding; `ThresholdGateResult:v2` reports that binding. Policies
must be recreated as v2 and receive a fresh Product Owner decision. A legacy
`PolicyApproval` with a matching `approver_id`, `authority` or policy digest is
not approval evidence.

The runtime binding contains:

- The SHA-256 identity of canonical `ProductionRuntimeProfile:v1` JSON and its
  schema version.
- The observed deployment fingerprint and commit SHA.
- Every accepted run `RuntimeVersionPins` value: observer seam, application,
  projection, model, prompt, validation policy and projection kind.

The v2 evidence builder accepts only `TrustedProductionRuntimePreflight` and a
completed `RealBenchmarkReportV3`. It requires every trusted prerequisite to be
ready, the observed profile to match the trusted composition, and the run's
dataset digest to match its baseline. Runtime profile strings are not assembled
from gate-call arguments.

## Approval source and verifier

The approved decision source is the GitHub Decision Record in
`alkselsv/spine#557`. The authority rule is the author of Issue #17. The
verifier reads both through authenticated GitHub REST requests and requires the
numeric author ID to match the pinned R1 Product Owner actor ID `167317615`.
Usernames, `authority="product_owner"` and caller-supplied booleans do not
establish authority.

A threshold approval record is a JSON object in a fenced
`spine-r1-threshold-decision:v1` block in a comment on #557. It binds decision
ID/status/time, policy ID/version/digest, dataset ID/version/digest, the full
runtime binding and release scope. Supersession and revocation name prior
decision IDs. The approval comment must be authored by the authority actor,
remain unedited (`updated_at == created_at`), and be the unique current decision
for that policy version and scope. API, parsing, identity, mismatch, conflict,
edit, revocation, supersession or currentness failures return no verified
approval; the pure gate then returns `not_approved`.

The verifier owns the authenticated GitHub adapter and emits
`VerifiedThresholdApproval`. The pure `evaluate_threshold_policy` receives that
typed record and has no GitHub or network dependency. The composition helper
performs verification before calling the gate. Mandatory safety evidence is
evaluated before approval or quality metrics.

## Verification scope

Tests use synthetic metadata and a fake decision source. They exercise exact
runtime and policy binding, approval actor/source/currentness, conflict,
revocation, supersession, unavailable source, safety priority, numerical
boundaries, applicability, coverage and private-case disclosure. No real
benchmark, provider call, numeric threshold selection or product approval is
part of this qualification. Until a matching numerical decision record exists
in #557, the trusted verifier correctly yields no approval.
