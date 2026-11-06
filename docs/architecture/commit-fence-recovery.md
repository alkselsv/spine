# Commit-fence recovery

Status: implemented as a provider-neutral application policy; provider
qualification remains a separate requirement.

`FencedIdempotentOperationCoordinator` owns retries for mutations protected by
an external commit permit. Every attempt uses a newly resolved authorization
binding and a newly acquired permit, while preserving the exact operation
schema, command digest and idempotency key.

## State transitions

```text
acquire/renew failure
        → rollback (if a database UoW exists)
        → abort permit
        → denied or fresh attempt

protected write failure / cancellation before commit
        → await rollback and database close
        → abort permit
        → original failure

commit response unknown or cancellation during commit
        → await database close
        → canonical idempotency reconciliation with fresh authorization
          ├─ completed receipt → finish permit → return stored result
          ├─ absent receipt    → abort permit → fresh binding and retry
          ├─ claimed receipt   → keep permit unresolved and deny retry
          └─ digest conflict or denied reconciliation → deny retry

known commit with lost finish/abort response
        → keep permit recovery-pending
        → retry settlement only after the database outcome is known
```

An unresolved permit is never reused and never treated as released. The
coordinator does not replay a mutation from the local return value: a
successful recovery requires the canonical completed receipt. Error messages
and recovery states contain no provider payload or protected content.

The canonical receipt and the mutation remain in the same database transaction,
so a committed receipt is the stable result and an absent receipt is evidence
that the transaction rolled back. A receipt that is still claimed is
ambiguous and remains denied until an operator/provider reconciliation resolves
it. Revoked or unavailable fresh authorization prevents a new permit from
being acquired.
