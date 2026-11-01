---
status: accepted
---

# Accept requirements only as an exact immutable decision

Spine accepts commercial requirements through one atomic, append-only decision
bound to the exact human-reviewed Requirements Artifact version and digest, the
current evidence-validation references, the accepting human's current scoped
authority and the acceptance-policy version. The same transaction records the
idempotent command receipt and terminal Workflow Run outcome; a stale artifact,
revoked authority, unusable evidence, unresolved conflict or competing terminal
transition fails closed without changing canonical state.

## Considered options

A mutable approval flag or acceptance of whichever artifact is latest was rejected
because neither preserves what the human actually reviewed. Treating the UI's
enabled control or a previously successful authorization check as authority was
rejected because both may be stale. Recording the decision separately from the
terminal transition was rejected because replay or partial failure could expose an
accepted artifact without the matching workflow outcome.

Failed or revoked evidence is never converted in place. An authorized human uses
the explicit `reclassify_requirement` command to supply new independent text, a
manager-assertion or explicit-assumption basis, rationale and (for an assumption)
the consequence if false. This appends a successor artifact and a distinct,
deterministically command-derived requirement identity whose upstream lineage
points to the retained source-backed predecessor. A verbatim normalized copy of
the protected predecessor statement is rejected so removing references cannot
launder protected text; the distinct identity also prevents the source-derived
requirement itself from being converted in place.
The immutable reclassification command also records the human's explicit
independent-authorship attestation. The product starts with an empty editor, and
the server rejects statements with substantial lexical overlap after
case/punctuation normalization; uncertainty fails closed instead of silently
reclassifying source-derived prose.

## Consequences

Exact replay returns the persisted decision, while a conflicting command cannot
replace it. Downstream proposal work consumes the accepted artifact reference and
does not acquire additional source access; any later evidence or requirements
change requires a new artifact and a new workflow decision rather than mutation of
the accepted record.
