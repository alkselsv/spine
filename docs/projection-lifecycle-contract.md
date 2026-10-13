# Context projection lifecycle contract

Issue #201 extends the provider-neutral `ContextProjection` contract without
making a physical graph or vector store authoritative.

Each projection command carries an optional monotonic `revision_number` for
each source object. The deterministic adapter records the highest supplied
revision and fences older revisions. A tombstoned revision remains a fence
until a higher-numbered reappearance is supplied. The adapter accepts the
canonical boundary and statuses from the caller; it does not determine
canonical currentness or publication.

Physical work is exposed as immutable `ProjectionEffect` values. A physical
adapter implements `ProjectionEffectStore.apply`, returning the same effect
with an applied, replayed or failed status. `ProjectionResult.effects` retains
the safe effect statuses and `ProjectionResult.lifecycle` distinguishes normal
success, replay, recovery, recoverable partial failure and revision fencing.

Command identity is scoped to workspace and environment. Repeating the same
identity and digest replays the stored logical result; a digest or operation
conflict fails without another physical effect. If one effect fails after
earlier effects were applied, the result is `recoverable` and names only the
pending effect IDs. Retrying the same command resumes pending effects and
never reapplies completed effects.

The contract is deterministic under concurrent invocation of the same command:
the adapter serializes its lifecycle decision and produces at most one logical
physical effect per command/effect identity. The deterministic physical store
in `spine.memory.fake` is an offline qualification implementation with
controlled one-shot or persistent failures; it is not a production store.
