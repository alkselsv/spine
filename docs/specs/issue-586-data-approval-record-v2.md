# Issue #586 — Data Owner Pricing-Source Approval Record v2

**Decision:** Data-1 through Data-4 `ACCEPT`.
**Decision date:** 2026-11-07.
**Role attribution:** the human owner explicitly confirmed acting separately as Data Owner. The confirmations are conversational evidence and not an independent signature. Exact prompt wording was not supplied and is not reconstructed.
**Decision request basis:** Issue #586 role-scoped data decisions. The Product Owner package v1.1 remains independently bound to SHA-256 `73c04fc138dcb36c34fff57169085ddbd87ada19b15d8be31dc4eae54b4e8325`.


The supplied conversational confirmations describe the Data decisions below but do not bind them to a separate package or document digest. This record transcribes only that supplied scope; it does not imply approval of later profile, ADR, implementation or configuration text.

## Decision record

| ID | Result | UTC confirmation | Accepted scope |
|---|---|---:|---|
| Data-1 | ACCEPT | 14:18:58 | Stable PriceItem identity scoped to workspace/environment; exact SourceObject/SourceRevision mapping, immutable source version/digest, UOM/currency/tax metadata; row number is not identity; similar names do not auto-merge; ambiguity blocks; material metadata change requires revalidation. |
| Data-2 | ACCEPT | 14:19:29 | Verifiable PriceItem-to-source lineage, exact source revisions/record digests/mapping versions, deterministic transformation, human validation of LLM suggestions, versioned quality validation and independent Finance approval; source change cannot silently republish. |
| Data-3 | ACCEPT | 14:20:43 | Explicit supersession/revocation; revoked source unavailable to new calculations; no auto-restoration; historical Estimates immutable subject to retention; affected undelivered Estimates recalculate/reapprove; conflicting currentness fails closed. |
| Data-4 | ACCEPT | 14:21:47 | Typed PriceItem schema/type/precision/UOM/currency/tax validation; versioned Data Validation report; ambiguity blocks active PricingBasis; only explicit governed UOM conversions; Data validation, Finance authority and Security authorization remain distinct. |

## Accepted data rules

1. `PriceItem` identity is stable within its trusted `workspace_id` and `environment_id`; bind the governed business item key to the exact accepted `SourceObject` and `SourceRevision` and its content digest. A spreadsheet row index, A1 address or displayed label is only a locator inside that exact revision, never stable identity.
2. Preserve source representation and exact record digest. Parse numeric values only through an explicit versioned source mapping/normalization profile; do not infer from formatting, locale, neighbor rows or display text alone. Source-specific scale/domain policies must be explicit and versioned; the Data decision does not populate those per-source policies.
3. Require explicit UOM, currency, tax category and inclusive/exclusive metadata or approved mappings. UOM conversion is allowed only through an explicit governed conversion version. Missing or conflicting metadata blocks use.
4. Provenance must permit verification from the PricingBasis through PriceItem mapping to exact SourceObject/SourceRevision, row/field locator, record digest, mapping version, validation report and Finance approval. Context Profile access, provider connection, import, row association or source revision alone does not authorize pricing.
5. New source content, mapping changes or material metadata changes require a new immutable revision/mapping validation and Finance approval before activation. Data validation does not itself grant financial authority or access.
6. Revocation/supersession is explicit and append-only. A revoked source cannot be selected or automatically restored; no new calculation uses it. Historical Estimates are preserved; an affected undelivered Estimate requires successor calculation and fresh approval.
7. Duplicate identity, conflicting mappings, incomplete revision, unknown source format or unresolved UOM/currency/tax metadata is a typed blocked data issue. Never merge or select by name similarity, row order or model suggestion.

## Boundaries and outstanding runtime evidence

This approval is for data contract/validation policy only. No external price book, real source mapping, currency/tax mapping, PriceItem or SourceRevision has been inspected or approved by this record. Those are governed data instances that require a versioned Data Validation report and separate Finance approval. Security still authorizes every action. Architecture still governs atomic activation. Existing `SourceObject`/`SourceRevision` and stable-locator contracts are reused; this record creates no independent source store.

## Acceptance evidence and future qualification

- [x] Workspace/environment-scoped stable identity; row number not identity.
- [x] Exact source revision, digest, stable in-revision locator and provenance required.
- [x] Explicit UOM, currency and tax metadata or fail closed.
- [x] Current source authorization/lifecycle checked independently from Data validation.
- [x] Supersession/revocation preserves immutable Estimates and requires reapproval for affected undelivered estimates.
- [x] Ambiguity has no silent selection/promotion path.
- [ ] Actual per-source mappings, schema versions and Data Validation reports supplied and reviewed before source activation.

This role decision does not verify a runtime implementation or authorize production activation.
