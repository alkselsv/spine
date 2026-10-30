# R2 mounted-filesystem production policy

This is the approved production policy for the first mounted-filesystem slice
of Issue #655 and the prerequisite for live qualification in Issue #680. Spine
reads a scope that the platform has already mounted. The adapter never mounts,
unmounts, authenticates to, writes to or deletes from the source filesystem.

## Approved roots and identities

The production root namespace is:

```text
/mnt/spine/sources/<workspace_id>/<connection_version_id>
```

The qualification-only namespace is:

```text
/mnt/spine/sources/qualification/issue-680/<connection_version_id>
```

Angle-bracket components are lowercase canonical UUIDs. Before activation,
Platform Operations records the fully resolved absolute leaf path, Workspace,
Environment, Provider Connection version, resource-scope reference, mount
backend and safe mount-option fingerprint. A connection version is bound to
exactly one leaf root. Neither `/mnt/spine/sources` nor a Workspace directory is
configured as an adapter root or scanned recursively across connection
versions. Changing the leaf path creates a new immutable connection version.

Platform Operations owns the mount as the host's privileged mount authority.
The Spine worker reads it as the non-privileged logical service identity
`spine-source-reader`; the deployment record binds that name to the exact
workload identity and numeric UID/GID. The worker has no root identity,
`CAP_SYS_ADMIN`, mount permission or source write permission. The mount must be
enforced `ro,nodev,nosuid,noexec`; the leaf is root-owned, group-readable by the
reader identity and not writable by that identity. Platform readiness rejects a
missing option, writable probe result, symlinked/non-canonical root or identity
binding mismatch before the connection becomes active.

The mount mechanism and any SMB/CIFS credentials belong to Platform Operations
and its secret boundary. No share address, username, password, ticket, keytab or
mount command enters a Provider Connection, Spine configuration, observation,
diagnostic event or qualification artifact. A direct SMB/CIFS client is a
separate future adapter decision.

## Containment and resource limits

The configured leaf root must exist as a canonical absolute directory. Object
references are POSIX-style relative paths of 1–1024 characters. Absolute paths,
backslashes, NULs, empty segments, `.` and `..` are rejected. Every path
component is opened relative to an already checked directory descriptor with
no symlink following; traversal and symlink escape fail closed.

Only regular files are eligible. Directories are traversal structure only;
symlinks, devices, sockets, FIFO entries and other special files are never read
as sources. A file is accepted only when descriptor and path identity, size and
timestamps remain stable through the bounded read. Reconciliation attempts an
unstable item at most twice in total (one retry) and then quarantines it without
accepting mixed bytes.

The approved initial limits match the implemented adapter profile:

| Limit | Approved value |
| --- | ---: |
| Default maximum file size | 32 MiB |
| Hard configurable file-size ceiling | 256 MiB |
| Read chunk | 64 KiB |
| One file read timeout | 10 seconds |
| One tree scan timeout | 10 seconds |
| Concurrent file reads per worker process | 4 |
| Reconciliation read attempts per item | 2 |

Increasing a hard ceiling or concurrency requires a new policy version and
resource qualification. Decreasing a connection-specific file-size limit is an
ordinary immutable configuration-version change.

## Identity, rename and recovery

Identity is `path-scoped` to the connection version, approved root scope and
relative path. Inode/device metadata and checksums support stable-read and
change detection but never prove Source Object identity. A rename or move is
the old path's absence plus a new observation; it does not preserve continuity.
A reappearance receives a new occurrence and explicit non-equivalence lineage.
Backend file IDs may replace this baseline only after a separate policy revision
and Issue #680 evidence for the exact backend and mount profile.

A missing, unreadable, timed-out or identity-changed root makes the connection
`unavailable` or the scan `incomplete`. It never proves file deletion. After
reachability returns, Spine performs a full reconciliation and requires two
matching bounded metadata snapshots around canonical reads. A tombstone still
requires a complete confirmed scan generation and the separately admitted,
immutable removal policy. Retry and recovery reuse idempotency identities and
must create one logical effect per revision.

## Monitoring, retention and ownership

Platform Operations owns mount availability, read-only enforcement, backend
health, capacity and recovery. Its content-safe monitoring checks the approved
root identity, read-only status and reachability and alerts on consecutive
failed checks; it does not enumerate or log protected names. The Spine ingestion
owner monitors connection state, last complete reconciliation, scan age,
timeouts, quarantined counts and recovery completion through safe diagnostics.
Security owns the `spine-source-reader` workload binding and least-privilege
review. The data owner approves each concrete root, Workspace/Environment scope
and source-system retention policy. The same data owner owns the canonical
retention policy for the imported originals, Source Revisions, tombstones and
lifecycle evidence; Platform Operations executes its approved cleanup actions.

The adapter never applies retention or cleanup to source bytes. Imported
originals, Source Revisions, tombstones and lifecycle evidence follow Spine's
separately approved canonical retention policy even when a source disappears or
the mount is retired. Mount retirement first deactivates the connection, waits
for in-flight reads, records the final reconciliation state and then returns the
external source to Platform Operations; it never rewrites canonical history.

Issue #680 may use only synthetic files in its qualification namespace. The
qualification operator records backend and mount-option fingerprints, scenarios
and content-free evidence. After evidence is committed, Platform Operations
removes the qualification mount and synthetic files outside Spine. Repository
evidence must contain no mounted path below the approved root, filename, share
address, credential or file content.

## Issue #680 qualification gate

Qualification is `NOT_VERIFIED` unless all of the following are recorded for
the selected non-production backend:

- the concrete qualification leaf root and `spine-source-reader` binding;
- effective read-only mount options and denied write/mount operations;
- traversal, symlink and special-file rejection plus the approved limits;
- observed path-scoped rename, unstable-read and mount-interruption behavior;
- full recovery reconciliation with no deletion caused by interruption; and
- Platform Operations cleanup completion.

Qualification cannot introduce a direct SMB client or claim backend-stable
identity from path, inode or checksum evidence.
