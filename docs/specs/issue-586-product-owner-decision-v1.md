# Issue #586 — Project Owner product decision v1

**Status:** PROPOSED / PENDING_PRODUCT_OWNER_DECISION
**Decision package:** [Pricing authority D1–D10 v1](issue-586-r2-pricing-decision-package-v1.md)
**Finance policy checklist:** [Finance approval record](issue-586-finance-approval-checklist-v1.md)

This record requests the product-scoped decision. It is not an approval and cannot approve Finance, Security, Data or Architecture decisions. Record a separate decision for every role a person is authorized to perform.

## Product decision requested

| Topic | Proposed product policy | Owner decision |
|---|---|---|
| Model | Select Issue #586 Option 2: approved immutable rule-set selection by service/project class with deterministic precedence. | PENDING |
| Source classes | Exact approved customer/service/period contract rate; approved service/project class price book; approved workspace base price book; separately requested/approved manager override. Historical estimates are reference-only. | PENDING |
| Applicability | Every basis is scoped by trusted workspace/environment, price item/service, class, client/contract where allowed, effective interval and explicit `pricing_as_of`. No missing scope dimension acts as wildcard. | PENDING |
| Precedence | Contract-specific → class set → workspace base. Inside class tier: exact service+project → service-only → project-only. Equal applicable rank conflicts fail closed. Override is never automatic fallback. | PENDING |
| Pricing date | Proposed `pricing_as_of` is supplied by the typed quote/estimate command and represents the business quote date. It must be timezone/date normalized by an approved policy; effective intervals are `[from, to)`. No file timestamp or universal TTL is used. | PENDING |
| Missing/conflict | No complete estimate or approval without an eligible basis; typed, disclosure-safe blocker; no historic/LLM fallback. | PENDING |
| Manager override | A manager may request a reasoned manual price decision only when policy permits; a separately authorized human approves it. It never mutates the source price list. | PENDING |
| Estimate lifecycle | Immutable Estimate artifact versions; exact-version review decision. Any material amount/basis/policy change creates a successor and requires new approval; previous approval does not transfer. | PENDING |
| LLM boundary | LLM may suggest work composition or candidate IDs only. It cannot establish authority, invent a current price or perform monetary arithmetic. | PENDING |
| Workflow | Use the existing canonical R2 WorkItem/WorkflowRun and typed commands/events; do not add a second workflow engine or separate pricing workflow. | PENDING |

## Product Owner response

- **Decision:** PENDING — select `ACCEPT`, `REJECT`, or `ACCEPT_WITH_CHANGES` for each row above.
- **Decision reference/package version:** `issue-586-product-owner-decision-v1` / package digest: PENDING.
- **Deciding identity and basis of Product Owner authority:** PENDING.
- **Decision time:** PENDING.
- **Rationale and accepted changes:** PENDING.
- **Role scope:** Product Owner only, unless additional roles are separately recorded below as distinct decisions.

## Acceptance criteria for this record

- [ ] An attributable Product Owner decision selects or rejects Option 2.
- [ ] Classes, scope, rank, `pricing_as_of`, fail-closed behavior, override, immutability/reapproval and single-WorkflowRun boundaries are each accepted or changed explicitly.
- [ ] Changes identify which D IDs and Finance/Security/Data parameters they affect.
- [ ] No other role approval is inferred from this record.
