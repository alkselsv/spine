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
