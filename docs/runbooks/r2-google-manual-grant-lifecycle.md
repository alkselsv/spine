# Google manual grant lifecycle (offline contract)

This runbook records the local credential and append-only reauthorization
contract implemented for issue #754. The ordinary test suite uses fake consent,
revocation and repository ports. It does not contact Google or require OAuth
credentials.

## Local credential files

`LocalGoogleCredentialStore` uses the absolute path
`~/.config/spine/google-qualification/` by default. It creates and verifies the
owned directory with mode `0700`; Desktop OAuth client configuration and refresh
token are stored in separate `0600` files named only from their opaque
references. The store refuses a Git worktree or common synchronized-folder root,
symlinks, unexpected owner/mode, oversized files and malformed client JSON.
Writes use exclusive temporary files and do not overwrite existing references.
The two-file write removes a newly written first file if the second write fails.

Keep this directory out of Git and any backup or synchronization service unless
that service has a separately approved encrypted handling policy. A safe
reference is metadata only; it does not authorize reading the corresponding
file. The existing connector resolves the references while executing a provider
read admitted by `GoogleReadAdmission`.

## Reauthorization and cleanup

An operator-triggered reauthorization command pins the connection ID, expected
current version, approval ID, successor credential/grant/scope references,
authorization and configuration generations, idempotency key and occurrence
time. The application creates exactly version `n + 1`, points to the expected
predecessor and advances authorization generation by one. It sends that version
through `GoogleConnectionRepository.register`, which checks the trusted context,
exact approval, optimistic predecessor and idempotent replay. A revoked
connection cannot be revived.

Remote revoke and local cleanup are explicit operations behind ports. The local
credential files are removed only after the revocation port confirms success;
success is reported only when both file removals are verified. Cancellation is
propagated so a caller can distinguish it from a completed cleanup. A failed or
unverified revoke leaves local files in place for an explicit retry.

## Offline verification

```bash
uv run pytest -q \
  tests/application/test_google_manual_authorization.py \
  tests/infrastructure/test_google_qualification_credentials.py \
  tests/domain/connections/test_google_connections.py \
  tests/infrastructure/connections/test_in_memory_google_connections.py
```

No live OAuth consent, credential creation, remote grant revocation or Google
qualification is part of this offline contract. Those actions require separate
owner and run-specific authorization.
