# Yandex Mail incremental UID polling

Yandex Mail incremental polling is a bounded optimization for discovering new
messages in one approved IMAP folder. It is not reconciliation and cannot replace
periodic complete, qualified folder reconciliation.

The poller starts only from a complete baseline produced by a snapshot or
reconciliation. The baseline carries the exact workspace, environment,
connection version, mailbox/folder and authority generations plus a durable
coverage receipt; the configured verifier must confirm that receipt before the
first checkpoint can be committed. The opaque checkpoint retains that baseline
lineage, `UIDVALIDITY`, and the next UID range to inspect. A baseline or
checkpoint from any other binding or generation is rejected. A changed
`UIDVALIDITY` requires reconciliation.

For every poll, the adapter first obtains a read-only `EXAMINE` observation. It
uses `UIDNEXT` only to bound work; `UIDNEXT` is not a message identity, accepted
effect, or deletion proof. The adapter then reads one finite UID interval. The
successor checkpoint is the first UID after that exact, completely covered
interval—not the provider high-water value sampled in isolation.

Each discovered UID maps to one generation-qualified canonical effect. Canonical
acceptance, its transactional outbox intent, and the durable email-sync receipt
must exist before PostgreSQL permits publication of the successor checkpoint. A
crash may therefore leave accepted effects ahead of the checkpoint; retry safely
replays those effects and receipts. The checkpoint must never move ahead of an
effect.

Partial responses, provider outages, invalid checkpoints, regressed high-water
values, stale authority generations, and incomplete effects hold the checkpoint.
Duplicate polling reuses deterministic page and effect identities. Gaps in a
completely searched new-UID interval are allowed, but they are not absence or
deletion evidence.

Incremental polling deliberately makes no claim about:

- flag changes;
- folder moves, copies, or membership changes;
- expunges or deletions;
- UIDs outside the exact covered interval.

Those changes remain the responsibility of periodic full reconciliation and the
separate authorized lifecycle-decision boundary. Polling cadence, reconciliation
cadence, page sizes, and live provider limits require separate operational
qualification; ordinary CI uses synthetic scripted provider responses only.
