# Issue 41 Command Digest Golden Vectors

Algorithm version: `spine.command-digest.v1`

Canonical representation version: `spine.canonical-command.v1`

All digests are SHA-256 over the UTF-8 canonical command bytes emitted by
`spine.application.persistence.command_digest.canonical_command_bytes`.

| Operation | Schema | Vector | Digest |
| --- | ---: | --- | --- |
| `proposal.generate` | 1 | UUID, enum, UTC timestamp, normalized decimal, ordered list of objects | `85bbf2a77f831be7de4efde8d231dee132167df29ef965e485e01922bb497537` |
| `proposal.generate` | 1 | Unicode text, decimal zero normalization, bool, sorted object keys | `65bedc7b953ac5b8ea07ee113ad5c41034c2b568b9880d5828fbeaf99b65efd0` |

Changing any byte of this representation requires a new algorithm version rather
than silently changing the meaning of existing idempotency receipts.
