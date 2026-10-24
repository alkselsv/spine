---
status: accepted
---

# Preserve format-specific source locator lineage

Projection publications may contain source revisions parsed under different
locator contracts. The exact locator contract is therefore pinned per processing
receipt, physical projection source and evidence reference; a projection config's
legacy locator value is only a default for producers that need one and is not a
projection-wide equality constraint. This preserves PDF, DOCX, XLSX, Markdown and
text evidence without weakening source-revision, receipt, tenant or digest checks.

## Considered options

Normalizing every parser to one locator shape was rejected because page, OOXML
block, worksheet cell and physical-line coordinates carry different stable
semantics. Splitting one publication by locator contract was rejected because it
would turn a parser detail into an operator-visible publication boundary.

## Consequences

Validation compares a locator contract along the canonical per-source chain:
processing receipt, physical command, source projection reference and disclosed
evidence. Projection config identity and digest remain independently pinned.

Existing active snapshots do not require a destructive rebuild solely because
of this validator correction: they already pin every canonical source reference
and locator schema. A workspace that needs newly supported mixed-format evidence
uses the normal successor publication path. That path captures a fresh canonical
boundary, rebuilds a shadow candidate from the pinned per-source processing
receipts and activates it only after validation and compare-and-swap publication.

Rebuild failure leaves the last active publication authoritative. Operational
rollback likewise uses a new successor built with the previously qualified
parser/config versions; operators never rewind an active pointer or mutate a
published snapshot in place. Reverting this validator in code is safe only
before a mixed-format successor is activated, because the previous validator
cannot validate a publication whose sources intentionally use different locator
contracts.

The regression gate is the pinned `evidence-lineage-synthetic` dataset version
`1.1.0`, evaluator `v1`, retrieval contract `structured-retrieval:v1` and
projection config `config:v1`. It must retain exact decision and safe rejection
rates of `1.0` for both a valid format-specific locator chain and a forged
cross-lineage locator. Compared with dataset `1.0.0`, version `1.1.0` adds two
cases, keeps the exact-decision rate at `1.0` (delta `0.0`) and keeps the safe
rejection rate at `1.0`; the checked qualification artifact records those
values.

`ProjectionConfigVersion.locator_contract_version` remains the persisted field
name for schema compatibility, but its semantics are now a producer default.
Any future config schema version should rename it to
`default_locator_contract_version` rather than changing the current schema in
place.
