# Profiled filesystem original-object storage

`FilesystemOriginalObjectStore` is the conditional single-deployment adapter for
Issue #6. It is a physical storage implementation behind the provider-neutral
`OriginalObjectStore` port. It does not decide Access Policy, SourceRevision
lifecycle, retention, admission or semantic MIME/content validity.

## Supported profile

The supported test and deployment profile is a Linux POSIX environment whose
dedicated local volume provides all of the following primitives:

- regular-file `fsync`;
- directory `fsync` for the storage directories;
- `O_NOFOLLOW` and `O_DIRECTORY` when opening storage entries;
- hard links on the same filesystem; and
- atomic failure of an exclusive hard-link destination when it already exists.

The staged and finalized namespaces must be on the same filesystem. The
production volume, mount options, permissions, backup agent and restart
procedure must be approved separately. A temporary filesystem used by the
integration suite proves adapter behavior on the test platform; it does not
approve a production volume.

Other operating systems, filesystems that do not provide these primitives, a
storage root that is a symlink, and indeterminate filesystem errors are
unsupported or fail closed with a typed storage error.

## Namespaces and references

The adapter creates three private namespaces below its configured root:

- `staged` contains an upload directory, manifest and incomplete payload;
- `finalized` contains the published payload and its private manifest; and
- `deleted` contains deletion evidence used for stable in-process cleanup.

Scope directories are derived from a server-side hash of trusted
Workspace/Environment context. Upload, object, generation and temporary names
are server-generated opaque identities. Client filename, declared MIME and
other transport metadata never participate in physical path construction or
manifest identity. Public `ObjectReference` and inventory entries contain no
filesystem path or namespace detail.

Staged payloads have no ordinary read operation. A finalized payload is only
considered readable when its private finalized manifest and immutable receipt
agree. Symlinks are rejected while creating, scanning, publishing, reading,
verifying and deleting entries; operations never follow a symlink from the
storage root.

## Synchronization and publication

The adapter synchronizes staged file data before publication. Publication uses
an exclusive hard link from the staged payload to the finalized namespace, so
the destination cannot replace an existing name and readers see a complete
inode rather than a copied prefix. The finalized manifest is then created
exclusively, file-synchronized and followed by directory synchronization.

Exactly one concurrent publication can create an absent destination. A replay
that observes the same verified receipt returns that receipt after rechecking
the finalized bytes. A different upload identity, generation or byte identity
cannot replace the destination and produces a typed conflict or indeterminate
failure. A `WriteReceipt` is returned only after digest/length verification,
file synchronization and finalized directory-entry synchronization.

Reads and verification stream bounded chunks. Uploads enforce object and chunk
limits, a concurrent-operation semaphore, inactivity timeout and total
operation timeout. Input is consumed one chunk at a time, so backpressure does
not require buffering the complete object in memory.

## Restart and reconciliation

The staged manifest records the server upload/object identities, request-digest
identity, command fingerprint, expected integrity values and the last complete
observed stream result. A staged upload with a durable `interrupted` marker can
resume with the same stored upload identity. An aborted or integrity-conflict
marker is terminal. A publishing marker or a finalized payload without a
matching finalized manifest is indeterminate and is never reported as a
successful receipt.

Inventory enumerates only sanitized finalized receipt metadata. It is bounded
and restartable through an opaque cursor; staged bytes are not an authorization
bypass and are not returned as ordinary inventory objects. Missing, malformed,
symlinked or contradictory physical evidence fails closed for the affected
operation. Reconciliation and cleanup must compare inventory against canonical
PostgreSQL receipt/reference records before deleting anything.

## Corruption, backup and restore

Finalization verifies the staged bytes against the observed and expected
digest/length. Read and verify detect a finalized length or digest mismatch and
return `ObjectCorrupt`; missing bytes return `ObjectNotFound`. No corrupt or
partially published object is served as a successful receipt.

Backups must include PostgreSQL receipt/reference metadata and all finalized
payloads and manifests needed by the approved retention and historical-revision
policy. Staged uploads are non-canonical and may be excluded only under an
explicit operational rule. A restore is not readable until the restored
metadata, finalized manifests and payloads have been reconciled and verified.
This adapter does not implement a coordinated PostgreSQL/filesystem backup
fence or claim that an ordinary volume snapshot is a coherent recovery point.

## Limits of the guarantee

The profile proves adapter-level bounded streaming, exclusive no-replace
publication, symlink rejection and conditional file/directory synchronization
on the supported platform. It does not claim universal crash safety or
durability across power loss, controllers, RAID, kernels, filesystems,
multi-volume deployments, backup implementations or machine failure. If the
deployment cannot prove its selected volume and recovery assumptions, reads
must remain unavailable until approved reconciliation or a different adapter is
selected.
