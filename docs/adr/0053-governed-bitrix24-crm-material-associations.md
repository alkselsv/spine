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
