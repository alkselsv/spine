---
status: accepted
---

# Projection control-plane handoff

Administrator projection recovery commands cross the HTTP boundary only as a
typed, idempotent `projection.recovery_requested` outbox intent containing the
command identity, projection kind, action and command digest. The control-plane
service never selects a snapshot, candidate, physical namespace, access policy,
activation generation or active pointer; existing projection recovery workers
and PostgreSQL-owned publication services remain responsible for execution and
activation. Status reads resolve the PostgreSQL publication record and expose
only its bounded operational metadata, so an accepted command is not presented
as a completed recovery until canonical reconciliation records say so.

The `successor` action is the exception only to snapshot selection: in the same
PostgreSQL transaction as the operator command, Spine captures a fresh immutable
canonical boundary and emits `projection.successor_requested` pinned to that
boundary. It still does not select a candidate, namespace or active pointer.
The worker rejects changed, foreign, missing or ambiguous lineage before physical
I/O, projects only current content members, retains boundary tombstones as
non-disclosable snapshot members, and publishes through the ordinary validation,
sealing and active-pointer CAS path. Historical or quarantined deliveries are
never revived or mutated. An administrator can correlate the returned successor
command with bounded, content-free intent/run/candidate/receipt/snapshot/
activation identifiers through the successor-status route; those diagnostics do
not expose source content or provider payloads.
