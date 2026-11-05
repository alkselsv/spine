---
status: accepted
---

# Governed email source identity and checkpoint semantics

This ADR records the offline email source contract approved for decomposition
under #816. It does not approve a live mailbox, provider credentials, provider
consent, corporate email processing, numeric operating limits, retention periods
or production readiness. Gmail and Yandex Mail remain read-only in this scope.

## Decision

Email uses the existing provider-neutral source, authorization, lifecycle,
retention, immutable-original, canonical-acceptance, outbox and Context Broker
boundaries. A mailbox is not a folder of files, and provider synchronization does
not create a second canonical source registry or publication path.

Every identity and synchronization operation is scoped to the exact workspace,
environment, immutable Provider Connection version, approved mailbox binding and
approved source scope. Provider admission and current source governance remain
separate from identity. A connection or provider grant alone does not grant
content processing, retrieval, disclosure or business-association authority.

## Message and conversation identity

The canonical source identity uses the qualified provider identity:

- Gmail: the exact connection and approved mailbox binding plus Gmail message ID.
- Yandex Mail over IMAP: the exact connection and approved mailbox binding,
  approved folder binding, `UIDVALIDITY` generation and UID.

This provider key identifies a Source Object; it is not a Source Revision ID.
Each admitted content observation belongs to that exact Source Object and is
accepted as an immutable Source Revision under ADR-0018. Replaying the same
qualified observation is idempotent; a later content observation cannot rewrite
an earlier Source Revision.

IMAP sequence numbers are transient positions and never identify a message. A
changed `UIDVALIDITY` invalidates the prior folder UID generation; reused UIDs
must not overwrite or continue an earlier Source Object. Reconciliation establishes
new qualified observations under the new generation.

Before enumeration, the Yandex Mail adapter may retain a folder-generation
observation from an exact, approved `EXAMINE` selection. The evidence is bound to
the workspace, environment, connection and connection version, authorization,
configuration and scope generations, mailbox binding and folder binding and
records both `UIDVALIDITY` and `UIDNEXT`. This operation
is read-only: it never enables `STORE` or `EXPUNGE`. Missing or invalid values,
`UIDNOTSTICKY`, an inconsistent response, a changed generation or a mismatched
binding yields a typed incomplete outcome. `UIDNEXT` is a synchronization hint,
not a message identity or deletion proof; sequence numbers are never retained as
identity or deletion evidence. Raw server diagnostics are not returned.

RFC 5322 `Message-ID` is optional, externally supplied evidence. It is neither a
canonical key nor a deduplication or merge instruction. Two provider messages
with the same `Message-ID` remain distinct provider-identified sources; a missing
or malformed value does not prevent qualified provider identity. Any normalized
or duplicate analysis is derived evidence and cannot replace provider identity.

A Gmail `threadId` is conversation evidence scoped to the authorized mailbox.
IMAP `References` and `In-Reply-To` values are message claims, not verified
conversation identity. Preserve the evidence kind and provenance when deriving
relationships. None of these values proves customer, project, participant,
ownership or other business association. Subject similarity and participant
addresses cannot establish those associations.

## Content revisions, state and artifact lineage

A Source Revision represents immutable message content and only explicitly
revision-bearing source metadata. Label changes, folder membership, flags and
other operational state changes do not create content revisions. They produce
content-free lifecycle/state observations under the existing lifecycle authority;
an observation alone does not change current eligibility or authorize deletion.
Content change is assessed against the qualified provider message identity and
immutable original evidence, not sender-supplied headers or mutable state.

Spine-owned Gmail state uses a tenant-scoped PostgreSQL ledger. Label and
relationship observations are append-only and bind the qualified Gmail message
key to the exact current raw-MIME Source Revision at first acceptance. A mutable
per-label head is only a derived ordering aid: the immutable observations remain
authoritative, and duplicate or older history delivery cannot move the head
backward. Replaying already accepted evidence remains valid after a later content
revision, because its original revision lineage is retained. Provider `threadId`
evidence and header-inferred `References`/`In-Reply-To` evidence use distinct,
non-upgradeable authority kinds. This ledger cannot create a Source Revision or
content projection intent; raw-byte changes continue through canonical source
ingestion and its transactional outbox boundary.

Store exact raw MIME as an immutable original, associated with its verified receipt
before canonical acceptance and not retrievable until canonical acceptance and
its transactional outbox intent commit. Parsing and sanitization produce
versioned derived artifacts and do not rewrite raw MIME. Each attachment original
and derivative retains the exact parent message Source Revision and MIME-part
lineage. Filename, disposition, header values or equal bytes cannot detach an
attachment from its parent or identify it as the same attachment in another
message.

Canonical acceptance and its transactional outbox intent are the sole publication
boundary for message and attachment effects. Provider visibility, a completed
download, a parsed artifact or a checkpoint never publishes content on its own.
Current Access Policy, lifecycle, retention and parent/child lineage checks
continue to govern processing, retrieval and citation.

## Message citation locator

`email-message-locator:v1` is the additive stable locator contract for message
body evidence. Its `StableLocator.kind` is `message`. The bounded canonical value
uses this field order:

```text
em1|source_revision_uuid_hex|source_revision_digest|artifact_uuid_hex|
artifact_digest|mime_parser_profile_digest|html_sanitizer_profile_digest|
mime_part_path|p_or_h|start_codepoint|end_codepoint|
representation_digest|excerpt_digest
```

`p` identifies decoded inline `text/plain`; `h` identifies plain text produced by
the exact complete sanitized-HTML derivative. Fixed profile prefixes are restored
from the contract version. UUIDs use lowercase 32-character hexadecimal form,
digests use lowercase SHA-256, ranges are half-open Unicode codepoint offsets and
the representation and excerpt digests hash UTF-8 bytes. The complete locator is
also protected by the existing `canonical-projection-locator:v1` digest carried by
the evidence reference.

Resolution requires exact agreement between the citation evidence, tenant,
Source Revision ID and digest, original digest, processed artifact ID and digest,
parser and sanitizer profiles, MIME part, representation, range and both text
digests. Only complete non-attachment message body parts are eligible; nested
`message/rfc822` content remains outside this message-body locator. Resolution
reads the immutable processed artifact, never the mailbox or raw provider payload,
and occurs only after Citation Inspector has freshly revalidated AccessPolicy,
lifecycle and retention.

This contract is additive and does not reinterpret locators in active snapshots.
To emit it, an eligible email source is processed and projected through a normal
successor publication pinned to the exact artifact/profile. Rebuild or validation
failure leaves the last active publication authoritative. Operational rollback is
another successor using the previously qualified processing and locator versions;
published evidence is never mutated in place. Synthetic golden vectors and
`qa.email_citation_resolution:v2` are the deterministic regression gate for exact
resolution and fail-closed rejection.

## Attachment citation locator

`email-attachment-locator:v1` is the additive stable locator for a supported
parsed range whose original bytes came from one exact message MIME part. Its
`StableLocator.kind` is `custom`; its canonical value starts with `ea1` and pins
the parent Source Object/Revision, processed message artifact, MIME part, a digest
of the complete parent artifact/profile/part identity, attachment artifact and
digest, processing receipt and digest, parsed artifact, and child stable locator.
The child citation evidence independently pins the attachment SourceRevision,
revision digest and immutable-original digest.

Citation Inspector derives two exact disclosure dependencies from this locator:
the parent message revision first and the child attachment revision second. Both
must be present in the persisted Context Bundle and freshly pass AccessPolicy,
lifecycle, currentness, processing and retention checks. Only then may resolution
load immutable records and verify the chain `message artifact/MIME part →
attachment original → processing receipt → child SourceRevision → parsed artifact
range`. A wrong part, parent, digest, receipt, child revision, tenant or stale
artifact returns the same content-free unavailable result.

During authorized retrieval, an `ea1` child is publishable only when its parent
revision is also present in the authorized retrieval scope. The Context Broker
persists a content-free parent dependency reference and its policy generation in
the same bundle; a missing or unauthorized parent makes bundle creation
unavailable. A child ingestion command marked `email.attachment.process` may not
downgrade to an ordinary parsed locator when the attachment-lineage receipt is
missing or incomplete.

Several immutable authority-version receipts may legitimately reference the same
downstream child processing effect. Projection accepts them only when their
parent, attachment, child revision and parsed-artifact lineage is identical, then
pins the latest receipt by `(completed_at, processing_receipt_id)`. Conflicting
lineage is unavailable; authority-version history is never overwritten.

This format is emitted only by a successor projection after its parent and child
references have passed validation. Existing `em1` locators are not reinterpreted.
A rebuild failure leaves the last active publication authoritative; rollback is a
new successor using the last qualified locator/processing versions. The v2
synthetic citation evaluation gate covers both `em1` and `ea1` vectors. The
attachment set is pinned at ten cases: one exact-lineage success plus nine
content-free failures for wrong parent, MIME part, artifact digest, processing
digest, stale child, cross-workspace access, stale parsed artifact, revoked
parent and revoked child. The initial v2 qualification result is 10/10 passing;
v1 message vectors remain
unchanged, so the attachment-contract delta is additive rather than a baseline
replacement.

## Checkpoints and no-gap publication

A checkpoint is an opaque, versioned, generation-bound synchronization cursor,
scoped to the exact connection version, approved mailbox/folder scope and
provider-specific cursor generation. Provider cursor contents are interpreted
only by their adapter; they are not public API fields, diagnostic text or
canonical source identity. A changed connection/scope generation fences prior
attempts from committing a cursor.

The shared coordinator owns bounded attempts, page application, idempotent effect
recording and committed-checkpoint publication. Gmail and IMAP adapters interpret
their own history or UID-generation cursors. The committed checkpoint may advance
only after every source and lifecycle effect covered by it has durable canonical
acceptance and the corresponding outbox intent. Cursor progress is never evidence
that an effect was accepted.

For Gmail initial synchronization, capture a history baseline before enumerating
the bounded snapshot, finish all snapshot pages, drain the intervening history,
and only then commit the resulting cursor. Do not sample a cursor after the
snapshot in a way that could hide changes concurrent with pagination.

An incomplete page, partial folder scan, failed canonical effect, lost authority
or stale generation keeps the committed cursor unchanged and records a typed
partial or blocked outcome. An out-of-range Gmail history cursor and an IMAP
`UIDVALIDITY` change require reconciliation; neither silently advances the
checkpoint or creates mass tombstones. Absence is deletion evidence only under
the complete, qualified reconciliation and authorized lifecycle rules in
ADR-0018.

After a crash following canonical acceptance but before cursor commit, replay the
same qualified provider identities idempotently and then commit only after the
covered effects are durable. A crash before acceptance leaves the cursor held and
the effect eligible for retry. This at-least-once protocol permits a cursor to
lag accepted effects; it must never move ahead of them. Provider retry details,
cadence, page sizes and other numeric limits remain unapproved until separately
qualified.

## Relationship to existing decisions

This ADR adds email-specific identity, state and cursor semantics only. It does
not amend or supersede these accepted boundaries:

- ADR-0001: PostgreSQL remains canonical for Spine-owned state; the Context Graph
  remains a rebuildable projection behind the Context Broker.
- ADR-0004: canonical mutation and outbox intent remain a transactional unit.
- ADR-0011: provider access does not grant content processing or disclosure.
- ADR-0018: source revision acceptance, lifecycle observations/decisions,
  tombstones, reconciliation and rollback remain canonical.
- ADR-0043: stable locators remain format-specific and bound to an exact Source
  Revision; email part/range locators extend this contract additively.
- ADR-0044: provider reads still require exact, current admission before calls.
- ADR-0050: Provider Connection approval, generation and control-plane rules
  remain provider-neutral and mail-specific approval is separate.
- ADR-0057: raw MIME, parsed bodies, attachments and search derivatives require
  their own approved retention applicability; this ADR defines no duration.

No email capability is considered live-qualified by this decision. Human
decisions for product purpose, mailbox scope, provider consent, retention,
processing policy, operating limits and any production qualification remain
separate gates recorded in #816.

## Contract examples

- Gmail source identity v1 golden vector: connection
  `11111111-1111-1111-1111-111111111111`, mailbox binding
  `aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa`, and message ID
  `18f4a7d9c2b6e031` produce `gm1:ccjms2mlougmcl6n7kv7qtfvewqosybohifeewabzjrknd732cgq`.
  Repeating the same qualified key produces the same value; changing the
  connection, mailbox binding or provider message ID produces a different key.
- Two Gmail message IDs with the same RFC `Message-ID` identify two provider
  sources; the header does not merge them.
- A Yandex folder reports a new `UIDVALIDITY`: old UIDs are fenced, the cursor is
  held for reconciliation, and a reused UID is not continuity evidence.
- The provider page was accepted canonically, then the worker crashed before
  committing its cursor: retry replays the same source identity idempotently;
  only after all covered effects and outbox intents are durable may the cursor
  advance.
- A Gmail label or IMAP flag changes while raw message content is unchanged: keep
  the current content revision and append a state/lifecycle observation.
- A folder scan is incomplete: retain the prior committed cursor and do not infer
  deletion from missing UIDs.
