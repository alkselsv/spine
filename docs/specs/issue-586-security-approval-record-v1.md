# Issue #586 — Security pricing authorization approval record v1

**Status:** PROPOSED / PENDING_SECURITY_APPROVAL
**Independent from:** [Product Owner decision](issue-586-product-owner-decision-v1.md), [Finance checklist](issue-586-finance-approval-checklist-v1.md), [Data record](issue-586-data-approval-record-v1.md)

This record governs who may read or act on pricing information. It does not approve rates, source correctness, taxes, discounts, or pricing-product direction.

## Proposed security boundary

Current source-content permission remains under current `AccessPolicy`; pricing business authority is separately granted by an approved, versioned pricing permission policy. Neither access to a document nor the administrator role creates pricing authority. All authorization context is server-derived and scoped to one `workspace_id`, `environment_id`, actor/service principal, purpose and operation. Recheck source authorization at read and protected disclosure/decision boundaries. Denial must not reveal protected candidate existence, price values or unrelated workspace metadata.

| Operation | Proposed least-privilege rule | Security decision |
|---|---|---|
| Read PricingBasis/source price | Named human/principal may read only if current AccessPolicy allows source content and pricing policy grants pricing read in exact workspace/environment. | PENDING — approve principals/roles and metadata scope |
| Calculate Estimate | Dedicated operation-scoped service principal processes only approved input versions and approved source references; no source mutation and no inherited authority from user/creator. | PENDING — approve principal, purpose and delegation |
| Maintain price source | Source maintainer can propose immutable successor data/rules but cannot make own publication authoritative without Finance approval. | PENDING — approve maintainer/approval separation |
| Request manager override | Authorized manager may submit typed request/reason/evidence against exact Estimate version. | PENDING — approve manager role bindings |
| Approve override | Separate authority and actor from requester; proposed requester cannot approve own override. | PENDING — approve/revise segregation rule |
| Approve Estimate | Approves only exact current artifact version/digest after server reauthorization and current run/artifact CAS. | PENDING — approve actor/role and independence rule |
| View audit | Auditor/authorized reviewers see attributable decision lineage; protected source values and customer-sensitive content remain access-controlled. Diagnostics remain content-free. | PENDING — approve fields and reader set |

## Segregation of duties proposed

1. Override requester and approver are distinct canonical human identities.
2. A maintainer who authored a price-source/rule-set successor cannot independently approve its pricing authority binding.
3. Estimate approver is authorized for the exact financial scope and approves the exact current version. Reuse of one human across multiple roles requires separate role-scoped decisions and does not bypass a segregation rule unless Security explicitly records an exception.
4. Final rules must use canonical human identity, not username/email alias equality; service principals cannot impersonate a human approval.

## Security approval record

- **Decision:** PENDING — select `ACCEPT`, `REJECT`, or `ACCEPT_WITH_CHANGES` per row and segregation rule.
- **Security Owner identity and delegated authority reference:** PENDING.
- **Exact package/record version and digest:** PENDING.
- **Decision time:** PENDING.
- **Approved principal/role bindings, purposes and environment scope:** PENDING.
- **Audit visibility and approved exception rationale:** PENDING.

## Security acceptance criteria

- [ ] Explicit identities/roles and scopes exist for read, calculate, source maintenance, override request, override approval and Estimate approval.
- [ ] Current AccessPolicy is checked independently from pricing permission; neither substitutes for the other.
- [ ] Workspace/environment isolation and denial-before-disclosure are confirmed.
- [ ] Override and source-maintenance segregation are decided; any exception is attributable and role-scoped.
- [ ] Audit readers/fields are approved; diagnostics do not disclose protected price/source content.
- [ ] Human approvals and service authority are not inferred from Product Owner or Finance decisions.
