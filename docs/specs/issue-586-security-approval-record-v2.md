# Issue #586 — Security Owner Pricing Authorization Record v2

**Decision:** Security-1 through Security-4 `ACCEPT`.
**Decision date:** 2026-11-07.
**Role attribution:** the human owner explicitly confirmed acting separately as Security Owner. These are conversational role-scoped confirmations, not an independent signature. Exact prompt wording was not supplied and is not reconstructed.
**Product Owner basis:** package v1.1 remains independently bound to SHA-256 `73c04fc138dcb36c34fff57169085ddbd87ada19b15d8be31dc4eae54b4e8325`.


The supplied conversational confirmations describe the Security decisions below but do not bind them to a separate package or document digest. This record transcribes only that supplied scope; it does not imply approval of later profile, ADR, implementation or configuration text.

## Decision record

| ID | Result | UTC confirmation | Accepted scope |
|---|---|---:|---|
| Security-1 | ACCEPT | 14:24:26 | Separate pricing capabilities for Viewer, Estimator, Price Source Maintainer, Pricing Manager, Discount Approver, Finance Owner, Estimate Approver and Security Auditor; least privilege/scoped access; administrative access is not pricing authority. |
| Security-2 | ACCEPT | 14:25:06 | No self-approval for discount, override, source publication or own Estimate; actor-identity independence; approval binds exact object version/digest; current authorization checked at commit; CAS/idempotency/audit; no API/UI/background/privileged bypass. |
| Security-3 | ACCEPT | 14:25:43 | Attributable scoped audit, no unnecessary commercial data in logs, denial does not reveal protected sources, audit visibility separately authorized, retention/holds/disposal follow applicable policies; exact field allowlist and retention remain distinct bindings. |
| Security-4 | ACCEPT | 14:26:33 | Trusted workspace/environment; current Identity/AccessPolicy; source read rights separate from pricing authority; recheck at approval commit; stale identities/commands cannot bypass revocation; scoped background execution; fail closed on missing/conflicting current authority. |

## Approved authorization principles

1. Capabilities are distinct and least-privilege: Pricing Viewer, Estimator, Price Source Maintainer, Pricing Manager, Discount Approver, Finance Owner, Estimate Approver and Security Auditor. Capabilities must be scoped to workspace, environment, purpose and operation.
2. Reading price content requires both current `AccessPolicy` permission and separate pricing-read authority. Calculating an Estimate does not grant read/maintain/approve privileges. Source maintenance can propose a new immutable version but cannot itself approve or activate financial authority.
3. Discount request, override request, source publication, and Estimate approval are separate actions. No requester approves their own request or Estimate. Independence uses canonical actor identity, not role-name inequality. Administrative access is not financial authority.
4. Authorization, current source authority and exact object version/digest are rechecked at the commit boundary. A stale token, prior session, queued command or background job cannot bypass current access/revocation. UI hiding is not an enforcement boundary.
5. Audit is attributable and scope-limited. Protected prices/source details are not included unnecessarily in logs; an access denial cannot disclose candidate existence/value or another workspace. Audit reading is separately authorized.
6. Workspace/environment isolation applies through API, UI, workflow, background execution and privileged operations. Missing or conflicting tenant/current authorization fails closed.

## Role-segregation note

The same human may hold Product, Finance, Data, Security and Architecture decision roles. That governance fact does not waive application-user segregation. Concrete runtime role grants, actor-principal mappings and any exception policy require deployment/security configuration and cannot be inferred from the human's governance confirmations.

## Boundaries and implementation status

This record approves Security policy principles; it does **not** prove runtime enforcement. No concrete user grants, workspace principals, service identities, API checks, PostgreSQL RLS, CAS, audit schema, field allowlist, retention period or production control has been inspected or authorized here. These remain implementation/configuration gates. Security approval does not authorize rates, validate source mappings, or approve the proposed ADR/profile JSON.

## Acceptance evidence and qualification

- [x] Distinct operations and least privilege approved as policy.
- [x] AccessPolicy remains separate from Finance authority.
- [x] Actor-based non-self-approval, exact version/digest and commit-time reauthorization approved.
- [x] Scoped audit and tenant isolation approved.
- [ ] Concrete principal/role bindings, audit field allowlist/retention and enforcement qualification remain to be supplied before production use.

No claim is made that API, UI, background jobs or database enforcement has been implemented or tested for pricing.
