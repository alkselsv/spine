# Issue #586 — Project Owner product decision v1

**Status:** ACCEPTED — PRODUCT OWNER SCOPE ONLY
**Decision record version:** v1.1
**Decision package accepted:** `issue-586-r2-pricing-decision-package-v1` v1.1, SHA-256 `73c04fc138dcb36c34fff57169085ddbd87ada19b15d8be31dc4eae54b4e8325`
**Current package status snapshot:** [Pricing authority D1–D10](issue-586-r2-pricing-decision-package-v1.md) v1.2
**Finance policy checklist:** [Finance approval record](issue-586-finance-approval-checklist-v1.md)

This record captures the explicit conversational decision supplied on 2026-11-07. It accepts only the Product Owner scope below. It does not approve Finance, Security, Data or Architecture decisions, financial parameter values, implementation, or closure of #586.

## Attribution and evidence

- **Decision:** ACCEPT.
- **Role:** Project Owner acting as Product Owner.
- **Identity:** The decision is attributed to the Project Owner represented in the supplied conversation record. A canonical human identity or GitHub account is not independently evidenced by that record and is not inferred here.
- **Decision time:** 2026-11-07 11:11:07 UTC.
- **Evidence type:** Explicit conversational confirmation, reported in the current ChatGPT conversation.
- **Evidence text:**

  **Question presented:** «Утверждаешь ли ты как Project Owner предложенную модель Option 2 и продуктовые правила D1–D10 в пределах полномочий Product Owner, оставляя Finance, Security, Data и Architecture на отдельное согласование?»

  **Response:** «Да»

- **Exact accepted package:** `issue-586-r2-pricing-decision-package-v1`, version **v1.1**, SHA-256 `73c04fc138dcb36c34fff57169085ddbd87ada19b15d8be31dc4eae54b4e8325` (file content at PR #1062 head `6aa624c15835d6a3f8e2cc6dac370e26a7cd4404`). The D1–D10 product-policy text accepted here is unchanged in the current status-only package update.
- **Attribution limitation:** This is not an electronic signature or a GitHub-authored approval. The issue comment records the supplied conversational evidence and identifies its source; it does not claim the Project Owner authored that GitHub comment.
- **Repository publication check:** Issue #586 asks for a resolution comment recording the human decision, rationale, roles and remaining parameters. `docs/agents/issue-tracker.md` permits issue comments but does not require that the comment be authored by the human decision owner. The recorded conversation is therefore the approval evidence; the GitHub comment is its attributed publication record.

## Product decision requested

| Topic | Accepted product policy | Remaining boundary |
|---|---|---|
| Model | Select Issue #586 Option 2: approved immutable rule-set selection by service/project class with deterministic precedence. | Finance must approve authority details and commercial parameters. |
| Source classes | Exact approved customer/service/period contract rate; approved service/project class price book; approved workspace base price book; separately requested/approved manager override. Historical estimates are reference-only. | Finance approves source authority; Data approves source identity/mapping. |
| Applicability | Every basis is scoped by trusted workspace/environment, price item/service, class, client/contract where allowed, effective interval and explicit `pricing_as_of`. No missing scope dimension acts as wildcard. | Finance/Data approve exact source fields, date policy and data mapping. |
| Precedence | Contract-specific → class set → workspace base. Inside class tier: exact service+project → service-only → project-only. Equal applicable rank conflicts fail closed. Override is never automatic fallback. | Finance separately approves source authority/order for financial facts. |
| Pricing date | `pricing_as_of` represents the business quote date and comes from the typed quote/estimate command, not source file metadata. | Finance/Data approve date normalization, effective interval boundaries and source-specific validity. |
| Missing/conflict | No complete Estimate or approval without eligible basis; typed blocker; no historical/LLM fallback. | Security approves disclosure; Finance/Data resolve source-specific policies. |
| Manager override | A manager may request a reasoned manual price decision only when policy permits; a separately authorized human approves it. It never mutates the source price list. | Finance/Security approve limits, approvers and segregation. |
| Estimate lifecycle | Immutable Estimate versions; exact-version review. Amount/basis/policy changes create a successor and require new approval; previous approval does not transfer. | Architecture confirms CAS/atomic persistence; Finance/Security approve review authority. |
| LLM boundary | LLM may suggest work composition or candidate IDs only. It cannot establish authority, invent a current price or perform monetary arithmetic. | No financial authority or calculation values are granted to a model. |
| Workflow | Use the existing canonical R2 WorkItem/WorkflowRun and typed commands/events; no second workflow engine or pricing workflow. | Architecture accepts exact command/event and transaction boundary. |

## Accepted scope and exclusions

The affirmative response accepts the Product Owner proposals in the table above as product direction. It expressly leaves actual tariffs, currency allowlist, FX provider/rates, Decimal precision/rounding mode, tax jurisdiction/rates/formulas, discount limits, approver identities, Security access permissions, Data mapping authority, and Architecture acceptance/ADR approval undecided. It does not authorize pricing implementation.

## Acceptance criteria for this record

- [x] The attributed Project Owner conversational decision selects Option 2 for Product Owner scope.
- [x] Classes, scope, product precedence, `pricing_as_of` business meaning, fail-closed behavior, override principle, immutability/reapproval, no LLM money arithmetic and single-WorkflowRun boundary are accepted for the Product Owner scope.
- [x] Financial, Security, Data and Architecture parameters remain separately pending.
- [x] No other role approval is inferred from this record.
