# Issue 41 Command Digest Golden Vectors

Algorithm version: `spine.command-digest.v1`

Canonical representation version: `spine.canonical-command.v1`

All digests are SHA-256 over the UTF-8 canonical command bytes emitted by
`spine.application.persistence.command_digest.canonical_command_bytes`.

| Operation | Schema | Vector | Digest |
| --- | ---: | --- | --- |
| `proposal.generate` | 1 | UUID, enum value, UTC timestamp, normalized decimal, ordered list of objects | `b2bb4c9eb3cad3cf0048d916fe645107ca5c01edbb98537fb25d6cdf424e1c0e` |
| `proposal.generate` | 1 | Unicode text normalized to NFC, decimal zero normalization, bool, sorted object keys | `1c812fe4b9ecaa1dad183683c3a2838c19cefccb32a102d5fa7d6affef0632bf` |

Changing any byte of this representation requires a new algorithm version rather
than silently changing the meaning of existing idempotency receipts.
