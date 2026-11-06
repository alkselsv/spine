# Current disposition authority and fence v1

T4 binds a disposition operation to one exact workspace, environment, source
object, current revision, acceptance and CAS generation. Its authority binding
projects only the relevant references from the existing
`TombstoneAuthorization`, including its command digest, current deletion-policy
approval, current retention eligibility and issuer. Expiry is evaluated at the
serialized admission, rebind or store-permit point, after any wait for that
authority boundary. The projection is temporary operation state; completion
acknowledgement or expiry removes the binding with the operation record.

The `DispositionAuthorityRepository` is the serialization port: its admission
method must read current authority and generation and persist `DELETION_PENDING`
atomically. Each store attempt then rechecks the deletion-only authority and
source generation. Its store-effect permit is bound to the persisted operation
state version and serialized with cancellation: cancellation that commits first
prevents the effect; an already granted effect completes before a later
cancellation can commit. Revocation before admission denies work; revocation or
a generation change after admission keeps the existing fence and denies later
store attempts. A denied permit preserves an unresolved `UNKNOWN` store state so
a later fresh approval reconciles it before another delete attempt.
Read/processing revocation is deliberately independent and does not cancel
separately approved cleanup.

No new positive `AccessPolicy` is introduced. Authority denial is represented by
a safe `None`/`False`; an admission infrastructure error is surfaced as an
unknown outcome after a read-back attempt, never as proof that no fence exists.
Store-permit errors deny the side effect. The tested in-memory runtime
serializes admission, revocation, cancellation and a store permit for
deterministic interleaving coverage. It does not claim PostgreSQL transaction
locking; the concrete authority/fence transaction is assigned to #1042. Read,
publication, processing, disclosure and checkpoint consumers are integrated by
#1047.

## Executed evidence

- `tests/application/test_disposition_authority.py` covers exact current binding,
  stale revision/generation, revoked authorization/policy/issuer, retention
  eligibility, separate read revocation, denied admission, expiry during
  admission/rebind/store-permit waits, admission/revocation/cancellation races,
  stale worker denial, and fence persistence.
- `tests/application/test_disposition_coordinator.py` covers coordinator retry,
  cancellation and per-store state under the T3 protocol. The T4 authority tests
  also cover denied-permit preservation of `UNKNOWN` and reconciliation after a
  fresh approval.
- This fake-only evidence does not qualify PostgreSQL lock ordering, source
  persistence, object-store deletion or physical erasure.
