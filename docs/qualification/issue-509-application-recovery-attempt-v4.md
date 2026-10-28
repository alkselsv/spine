# Issue #509 recovered application attempt v4

Status: **projection publication verified; application recovery qualification remains NOT_VERIFIED**.

The owner-authorized synthetic run restored a fresh isolated application target
from `recovery:issue509-005`. PostgreSQL TLS, ownership, ACL posture and runtime
readiness passed before any provider access.

The approved provider profile completed the physical Cognee projection. The
production projection consumer then persisted the canonical receipt and
completed normal snapshot publication and activation. The published projection
has activation generation 1. This closes the canonical-handoff failure recorded
by attempt v3 and confirms the diagnostic correction from the offline causal
control.

The single permitted-source application read was not executed. The restored
human qualification-authority records were already expired. Creating a new
authority record would have exceeded the run authorization, which prohibited
authority mutation, so the qualification failed closed without reading source
content. Publication success therefore does not by itself qualify application
recovery.

Cleanup completed after evidence capture. Runtime database access was revoked,
temporary operator and runtime authentication material was rotated to unknown
values, the isolated graph container and data volume were removed, and the
PostgreSQL target was stopped. No serving or cutover occurred.

The remaining gates are one separately authorized application read through the
normal Context Broker path and independent review of the final evidence. The
JSON companion contains only opaque identifiers, digests, counts and bounded
states; it contains no source content, authentication values or provider output.
