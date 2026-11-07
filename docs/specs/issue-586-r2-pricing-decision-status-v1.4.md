# Issue #586 — Pricing Decision Status Overlay v1.4

**Status:** PARTIALLY APPROVED; NOT IMPLEMENTATION-APPROVED.
**Decision request basis:** package v1.3, SHA-256 `ae8916b72b6f8c2ffb9811262ac484415efb44f074586a49d52563e868828041`.
**Product Owner approval basis:** package v1.1, SHA-256 `73c04fc138dcb36c34fff57169085ddbd87ada19b15d8be31dc4eae54b4e8325`.
**Finance/Tax record:** [decision record v3](issue-586-finance-decision-record-v3.md).
**Successor profile candidate:** [`r2-commercial-v2` v2](issue-586-calculation-profile-candidate-v2.json), SHA-256 `497afb60d1df45596c34db52ac4d6127ab069696101685948ac301cb218bac08`.

This is a status overlay. It does not alter the frozen v1.3 request or the package v1.1 Product Owner decision basis.

| Decision scope | Status | Evidence / boundary |
|---|---|---|
| Product Owner D1–D10 product scope | ACCEPTED | Recorded separately against package v1.1 digest above. |
| Finance/Tax F1–F8 | ACCEPTED | Individually confirmed in conversation on 2026-11-07 at the timestamps in Finance record v3. This is not a GitHub-authored approval/signature. |
| Finance/Tax F9 | ACCEPT_WITH_CHANGES | Required changes accepted; unseen successor JSON is not accepted or activated. |
| Finance/Tax F9-T1 precision residual | PENDING_FINANCE_TAX_CONFIRMATION | New material mathematical choice, not contained in the confirmed F1–F9 scope. |
| Security | PENDING | Permission grants, segregation, audit disclosure and workspace/environment isolation. |
| Data | PENDING | Stable identity, SourceRevision/row mapping, UOM/currency/tax metadata, provenance and currentness/revocation semantics. |
| Architecture | PENDING | Canonical authority ownership, activation/currentness, CAS/audit/idempotency atomicity, existing R2 workflow/artifact reuse and ADR decision. |
| Pricing implementation authorization | NOT GRANTED | No runtime code, migrations or production source onboarding authorized by these decisions. |

Finance accepted policy parameters include the four-currency allowlist (RUB/USD/EUR/CNY), Bank of Russia FX authority, 34-digit Decimal context, `ROUND_HALF_UP`, `.01` currency quantum, Russian Federation as first supported tax jurisdiction, discount thresholds/no stacking and the listed fail-closed outcomes. Actual price books, FX observations, tax rates/TaxBasis revisions, exemption evidence, effective-date bindings and source-specific authority records remain governed data/policy bindings and are not created here.

## Closure status

Issue #586 remains OPEN. Closure requires completion of Finance/Tax profile qualification and policy bindings, separate Security/Data/Architecture decisions, exact role attribution and satisfaction of the issue acceptance criteria. Documentation publication or PR merge alone is not an implementation approval. No production pricing authority or pricing engine is authorized.
