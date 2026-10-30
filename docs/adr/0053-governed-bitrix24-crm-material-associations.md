---
status: accepted
---

# Governed Bitrix24 CRM material associations

## Context

Bitrix24 CRM activities can reference Disk files that are already imported as
canonical Spine source revisions. A filename, activity subject or textual
similarity is not sufficient evidence of that relationship. The CRM slice also
must not become a general CRM-management capability or grant access to source
content.

## Decisions

1. A connection may declare the conditional `crm` capability only together with
   qualified Disk capabilities and an immutable approved CRM entity-type scope.
   The adapter rechecks workspace, environment, connection generations, scope,
   entity type and acting-user authorization before every provider read.
2. CRM observation is content-free and retains exact activity, attachment,
   relationship, Disk file ID and Disk provider-revision evidence plus the
   target `SourceObject`/`SourceRevision` IDs. No filename or free-form CRM text
   participates in identity resolution.
3. Association acceptance is a separate append-only handoff. PostgreSQL accepts
   only the current, canonically accepted Disk revision whose stored provider
   identity and revision exactly match the claim and whose current access policy
   is present. Cross-workspace, stale, inaccessible and mismatched targets fail
   closed.
4. Acceptance grants neither source disclosure nor `PricingBasis` authority.
   Source and relationship revocations are immutable records that remain
   visible after acceptance. Replayed IDs return the persisted evidence or fail
   on a digest/payload conflict.

## Consequences

- CRM-linked materials can be used as provenance for later business workflows
  without enabling CRM mutation or silently making imported content trusted.
- The Disk source lifecycle remains canonical and independently revocable; a
  relationship cannot resurrect a stale or inaccessible revision.
- The relationship adapter is provider-specific, while the handoff and
  persistence boundaries remain typed and replaceable.

## Source

Ticket #671 under the R2 provider-neutral source specification (#655).

## Versioned CRM relationship evidence v2

This section adds a v2 contract for new CRM relationship evidence. Existing Decisions 1–4 remain the historical v1 contract and are not rewritten. Existing v1 rows, field meanings, acceptance/revocation history and replay continue through the v1 reader. Unknown persisted versions fail closed. No v1 field receives new meaning; no v1 row is backfilled with a receipt, provider revision, mapping profile or v2 lifecycle event.

### 1. Versioned records and separate identity

V2 records persist schema_version = spine.crm-relationship-evidence:v2 plus a closed record_kind. Dispatch by both before payload validation; reject unsupported versions/kinds without coercion or fallback.

V2 has four identities:
- Logical relationship: stable identity for one exact activity/entity/FILES-item relationship in provider, connection, workspace and environment scope.
- Revision-specific claim: one exact FILES item associated with one exact canonical SourceObject/SourceRevision.
- Observation receipt: one Spine-created point-in-time verification attempt.
- Lifecycle event: one durable authorized governance transition.

The logical relationship key is lowercase SHA-256 Hex64 over domain-separated canonical JSON of workspace/environment, provider instance and logical connection, exact activity identifier namespace/value, raw binding entityTypeId plus its qualified normalizer contract, entity identifier namespace/value, exact FILES-item namespace/value, and stable mapping-profile identity. Integer identifiers use strict base-10 serialization. A profile identity may span immutable versions only while identifier semantics remain constant; changed semantics require a new profile identity. Persist natural-key fields with the digest and reject digest/payload conflicts. Exclude connection version, receipt/time, Disk version and SourceRevision. Do not use filename, path, subject, content similarity or numeric coincidence.

Claim, receipt and lifecycle IDs are distinct immutable IDs with independent idempotency keys and payload digests. Same key and payload replays the stored record; changed payload conflicts. Repeated observation does not rewrite history. New Disk SourceRevision or replaced FILES item requires a new claim. No claim transfers automatically.

### 2. Spine point-in-time observation receipt

CrmRelationshipObservationReceipt:v1 is an immutable Spine-issued record for one bounded attempt. It pins tenant/environment; provider instance, logical connection and exact connection version; authorization/configuration and acting-human authority generations; exact activity and normalized entity pair; exact namespaced FILES item; immutable mapping profile/version; binding-list page and positive-pair evidence; attachment ownership proof when used; observed Disk object/version evidence; exact canonical source IDs; sanitized outcome; digests and observation time.

The receipt stores bounded references, digests, counts and allowlisted outcomes only. It excludes secrets, signed URLs, full raw payloads, filenames and source content. observed_at is descriptive, never authority ordering. The receipt is not a provider-native revision, atomic CRM/Disk snapshot, perpetual relationship proof, source read permission, AccessPolicy authority or PricingBasis authority.

Only provider revision/version fields actually obtained under the approved Disk contract and matched to the exact canonical SourceRevision may be retained. Never synthesize ACTIVITY_REVISION, RELATIONSHIP_REVISION or Disk revision values.

Relationship-dependent use requires fresh bounded verification under an immutable Operations profile. Recheck current Spine authority and target-specific binding/FILES/Disk evidence at profile-defined boundaries. Relevant identity or generation disagreement returns typed STALE_CONTEXT, NOT_VERIFIED, INACCESSIBLE or AUTHORITY_UNAVAILABLE. Repeated matching observations reduce uncertainty but prove neither atomicity nor perpetual consistency. Numeric freshness, retry and page budgets need separate Operations approval.

### 3. Exact positive binding and type normalization

Only a successful crm.activity.binding.list result for the exact requested activity that contains the exact requested raw (entityTypeId, entityId) pair is positive evidence. Preserve method, raw values, page evidence and normalizer ID/version. crm.enum.ownertype documents numeric type IDs used by universal entityTypeId and activity OWNER_TYPE_ID/ownerTypeId; that mapping applies only to those named method fields. Do not infer symbolic/short-code mappings for other methods. Unknown or ambiguous normalization fails closed.

Allow only exact activityId and bounded documented pagination control. A successful well-formed page containing the target pair is positive evidence; unrelated sibling pages need not be enumerated to prove that positive. Complete successful bounded pagination is required before classifying absence as NOT_VERIFIED. Permission-filtered, incomplete, malformed, failed or page-bound-exhausted absence remains uncertain. Absence never proves provider revocation/deletion or creates a source tombstone. OWNER fields, activity BINDINGS, siblings, another entity and cached old positives cannot substitute.

### 4. Immutable per-flow FILES profile

Bitrix24CrmFilesMappingProfile:v1 is immutable, versioned and scoped to one approved provider/connection and exact activity flow. It binds approved entity/activity types, exact read-method set, FILES field/item identity namespace, normalizer, one mapping strategy, ownership proof, Disk identity/version mapping, and trusted approval/qualification references. Missing, stale, superseded or ambiguous profile denies.

Exactly one strategy is selected:
- direct_disk_object: use only the exact qualified FILES field/item documented as a Disk object identifier;
- attached_object: treat the exact FILES value as an attachment ID only when that flow is qualified, call disk.attachedObject.get, validate exact MODULE_ID/ENTITY_TYPE/ENTITY_ID ownership, and use returned OBJECT_ID as a separate Disk namespace.

Official CRM docs describe activity FILES as diskfile with a Drive file ID; attached_diskfile and attached-object records are distinct documented types. This is shape evidence, not activation of any portal flow or proof that v1 DISK_FILE_ID is correct. An attached-object mapping requires exact documentation or #679 observation and ownership validation. No heuristic, fallback, filename/subject match, sibling inference or numeric-ID equivalence. Successor versions append immutably. No real or production profile is approved here.

### 5. One claim per exact item and source revision

One claim means one exact FILES item to one exact current canonical SourceRevision. It records exact tenant/environment, current connection and acting-human authority generations, activity/entity pair, namespaced FILES identity, ownership proof, profile/version, strategy, Disk object/version evidence, canonical SourceObject/SourceRevision and evidence digests.

Validate items independently. One item error does not contaminate independently proven siblings. Partial success is allowed only where item boundaries and evidence are independently unambiguous; response-level parse/identity ambiguity makes affected items uncertain. Sibling evidence cannot support another claim. Cross-workspace association is forbidden. A changed Disk SourceRevision always requires a distinct claim.

### 6. Derived current eligibility and AccessPolicy

CrmRelationshipEligibility:v1 is a derived per-claim decision, not immutable history or permission. Eligibility requires current connection/configuration/authorization and acting-human authority; current independent entity/flow/method approvals; current profile; fresh exact positive binding and ownership proof; exact Disk identity/version matching current canonical SourceRevision; current source lifecycle/retention eligibility; and no effective governance withdrawal.

Closed outcomes include ELIGIBLE_VERIFIED, NOT_VERIFIED, INACCESSIBLE, STALE_CONTEXT, GOVERNANCE_WITHDRAWN, SOURCE_INELIGIBLE, AUTHORITY_UNAVAILABLE and PROVIDER_REVOCATION_UNQUALIFIED. Missing or ambiguous required authority fails closed. Neither provider timestamps nor observed_at select currentness.

ELIGIBLE_VERIFIED means relationship evidence and its exact source revision satisfy this contract. It grants no content disclosure. The consumer independently checks current ADR-0011 AccessPolicy for the exact actor, purpose and operation at use/disclosure. Do not copy an actor's AccessPolicy allow into relationship state. Relationship inaccessibility/uncertainty does not mutate SourceObject, SourceRevision, CurrentSourceHead, AccessPolicy, source lifecycle or tombstones. CRM relationship eligibility cannot accept, revise, supersede or otherwise change a canonical SourceRevision or CurrentSourceHead.

### 7. Governance withdrawal and reapproval

Temporary provider inaccessibility, missing/filtered verification, Spine governance withdrawal and future provider-native revocation are distinct. No provider-native revocation is enabled absent a separately qualified evidence contract.

Withdrawal requires a trusted currently authorized actor, exact workspace/environment/relationship and optional claim, authority version, expected lifecycle head, immutable event identity and bounded reason/evidence references. It is Spine governance, not proof of provider/source deletion, AccessPolicy revocation or tombstone authority. Append event and required audit/outbox with CAS.

Reapproval is a separate authorized immutable event naming the exact predecessor and current lifecycle generation. It requires current authority/profile/connection/source head and fresh exact positive evidence. CAS conflict/stale authority fails closed. No positive read clears withdrawal; no silent resurrection or AccessPolicy transfer.

### 8. Append-only persistence, ordering and recovery

V2 persistence is additive. Store immutable identities, claims, receipts, mapping-profile versions/approval refs and lifecycle events separately from mutable eligibility/authority heads. Every tenant row has workspace/environment. Use composite scoped FKs across relationship, claim, receipt, event, profile/connection, canonical SourceRevision and eligibility; RLS/FORCE per repository patterns; immutable-history privileges/triggers; unique idempotency keys; digests and changed-payload conflicts.

PostgreSQL allocates monotonic local verification-attempt and lifecycle generations and owns expected-current CAS. These order Spine commands only; they are not provider revisions. Late attempts cannot advance the eligibility head. Currentness mismatch returns typed stale/ambiguous outcome. Never order by MAX(observed_at).

Relationship-record append, eligibility-head CAS and required audit/outbox/idempotency result commit in one PostgreSQL UoW. CRM relationship persistence has no authority to change canonical SourceRevision or CurrentSourceHead; exact source identity/currentness is read and fenced through the existing S2 source boundary. Unknown commit is reconciled using original key/digest by reading the exact row/head; exact payload returns prior result, changed payload conflicts. Never blindly emit a replacement. Reader-first additive rollout retains v1/v2 readers before enabling v2 writes. Rollback disables v2 paths while retaining v2 history and v1 interpretation. PostgreSQL integration proves RLS, composite FKs, immutability, transaction/CAS races, audit/outbox and unknown-commit recovery; fake tests alone are insufficient.

### 9. Compatibility and authority boundaries

This amendment preserves v1 history and replay and does not authorize new live v1 execution. The audited v1 path's service-principal fallback and missing commit-time current-authority revalidation require a separate activation guard/fix before live wiring. Other known v1 concerns are preserved as security debt: AccessPolicy-row presence is not consuming-actor disclosure authorization; revocation does not verify a trusted governance actor; historical revocation ordering uses observed_at; and ACTIVITY_REVISION, RELATIONSHIP_REVISION and DISK_FILE_REVISION are local/synthetic fields not proven provider-native. This amendment does not fix or reinterpret those paths. No v1 row is converted or backfilled. New v2 data uses explicit v2 dispatch, requires a current approved acting human, and has no service-principal fallback or cached ALLOW.

#716 owns v2 contracts, persistence, CAS, eligibility, lifecycle and migrations. #717 owns read-only exact binding, qualified normalization, per-flow FILES mapping, per-item evidence and bounded revalidation. Offline deterministic tests are required; PostgreSQL-specific guarantees require PostgreSQL tests. #679 remains separately NOT_APPROVED until all listed prerequisites and explicit authorization are satisfied.

Provider Connection/acting-user authority controls provider reads. S2 controls Disk source/revision admission. S3 controls source withdrawal, holds, retention and tombstones. ADR-0011 AccessPolicy remains the sole source disclosure authority. This contract grants no disclosure, AccessPolicy mutation or PricingBasis authority and creates no second source registry. Entity approval does not imply flow/mapping approval. Default is DENY ALL.

### Consequences

The v2 design preserves historical v1 evidence while making current relationship evidence auditable and governance-revocable. It rejects timestamp ordering, owner/BINDINGS fallback, mutable v1 reinterpretation, sibling-bundled claims, FILES heuristics and provider-absence revocation. Offline acceptance does not qualify a portal, prove database behavior, approve concrete Data/Portal/Product scope, authorize #679 or enable production.
