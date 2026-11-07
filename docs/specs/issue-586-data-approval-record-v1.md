# Issue #586 — Data pricing-source and provenance approval record v1 (historical proposal)

> Current role decision: [v2 record](issue-586-data-approval-record-v2.md). This file is preserved as the pre-decision proposal and its PENDING status is historical.

**Status:** PROPOSED / PENDING_DATA_APPROVAL
**Independent from:** [Product Owner decision](issue-586-product-owner-decision-v1.md), [Finance checklist](issue-586-finance-approval-checklist-v1.md), [Security record](issue-586-security-approval-record-v1.md)

This record asks the Data Owner to approve the shape, identity and lifecycle of structured source facts. It does not approve prices or grant content access.

## Proposed data contract

| Concern | Proposed invariant | Data decision |
|---|---|---|
| PriceItem identity | Stable governed `price_item_id` plus source-scoped stable item key; never row number, A1 coordinate, label similarity or title alone. For governed Sheets mapping, include `SourceObject` identity, stable spreadsheet/sheet IDs and immutable business row/item ID; use cell/row locator only inside exact revision. | PENDING — approve canonical key and normalization |
| SourceRevision | Every candidate binds exact accepted `SourceObject` + immutable `SourceRevision` ID/digest and provenance. A newer revision does not rewrite prior basis or Estimate. | PENDING — approve eligible source/revision classes and mapping |
| Structured values | Preserve source representation, locale/number format evidence as needed; parse into typed Decimal only under a versioned parser/normalization profile. Never infer numeric value from display text alone. | PENDING — approve representations and conversion rules |
| Unit of measure | Every PriceItem has explicit canonical UOM and versioned source-to-canonical conversion where permitted; missing/ambiguous/incompatible UOM blocks selection. No assumed conversion. | PENDING — approve UOM vocabulary/conversions |
| Currency/tax metadata | Currency, price inclusive/exclusive flag and tax category are explicit source fields/approved mapping references. Missing or conflicting values are not inferred from locale/format or neighbor rows. | PENDING — approve metadata schema and mapping |
| Current source authorization | Retrieval and use require current source lifecycle eligibility and current AccessPolicy; source registration, connection grant, association, Context Profile or prior permission is not permanent authorization. | PENDING — approve source currentness signals and recheck points with Security |
| Provenance quality | Retain exact authority approval, source revision, stable locator, row/item identity, source value digest, rule-set version/digest, effective period and parser/mapping profile version. | PENDING — approve required field set and quality threshold |
| Supersession | Publish an immutable successor binding/policy; never overwrite the old source record. New calculations use current approved successor. | PENDING — approve atomic activation/overlap policy with Finance |
| Revocation | Revocation stops new calculations. Preserve already-approved Estimate unchanged; create an explicit review/disposition signal for outstanding use. | PENDING — approve treatment of approved-but-unsent and draft estimates |
| Unknown/ambiguous source | Missing identity, duplicate IDs, conflicting row mappings, incomplete revision, unverified numeric representation or unresolved scope is quarantined/blocked as typed data issue; never choose arbitrary candidate. | PENDING — approve reason mapping and recovery owner |

### R2 source reuse

Reuse canonical `SourceObject`, `SourceRevision`, provenance, stable-locator and accepted association contracts. R2 Google row associations from #613 link exact revision evidence but explicitly do not establish PricingBasis authority. The Data approval binds a stable commercial item identity to an exact row/cell/field in one source revision; Finance separately approves whether that bound item is a valid current tariff.

## Data approval record

- **Decision:** PENDING — select `ACCEPT`, `REJECT`, or `ACCEPT_WITH_CHANGES` for each concern.
- **Data Owner identity and delegated authority reference:** PENDING.
- **Exact package/record version and digest:** PENDING.
- **Decision time:** PENDING.
- **Approved PriceItem key, UOM, currency/tax mapping and parser/profile references:** PENDING.
- **Supersession/revocation and ambiguity disposition:** PENDING.
- **Rationale and residual data-quality constraints:** PENDING.

## Data acceptance criteria

- [ ] Stable identity remains invariant under row sort/move and depends on approved business identifiers, not coordinates or text matching.
- [ ] Each selected item points to one exact authorized SourceRevision, provenance and stable in-revision locator.
- [ ] UOM, currency, inclusive/exclusive status and tax-category mapping are explicit or fail closed.
- [ ] Current source authorization and lifecycle status are rechecked; unauthorized or stale source cannot be used.
- [ ] Source correction, supersession and revocation preserve historical Estimate lineage and never mutate approved artifacts.
- [ ] Duplicate/conflicting/unknown values have an owner and typed blocked path; there is no silent selection or promotion from historic Estimate.
