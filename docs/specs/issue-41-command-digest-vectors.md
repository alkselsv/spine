# Issue 41 Command Digest Golden Vectors

Algorithm version: `spine.command-digest.v1`

Canonical representation version: `spine.canonical-command.v1`

All digests are SHA-256 over the UTF-8 canonical command bytes emitted by
`spine.application.persistence.command_digest.canonical_command_bytes`.

| Operation | Schema | Vector | Digest |
| --- | ---: | --- | --- |
| `proposal.generate` | 1 | UUID, enum value, UTC timestamp, normalized decimal, ordered list of objects | `b2bb4c9eb3cad3cf0048d916fe645107ca5c01edbb98537fb25d6cdf424e1c0e` |
| `proposal.generate` | 1 | Unicode text normalized to NFC, decimal zero normalization, bool, sorted object keys | `1c812fe4b9ecaa1dad183683c3a2838c19cefccb32a102d5fa7d6affef0632bf` |
| `proposal.generate` | 1 | String-valued enum `ready` | `64028fb37bbb756d346e33b30992b0af36f198d5d821c174e2fe77bd4d9f6a49` |
| `proposal.generate` | 1 | Integer-valued enum `1` | `c756a1e061fb0bf42bbd0eeb9fa3320bbdae0e6b0444b4dcaf2fe29f6fadd203` |
| `proposal.generate` | 1 | UUID-valued enum | `0cfb88433d9117ffcc2534800b5cf500281bfb6c2ed9043cd7766e31a642409c` |
| `proposal.generate` | 1 | Decimal-valued enum `12.3400` normalized to `12.34` | `e92379305a8439d225d3552f2ac8043021cf97a5b68e4d45484ce596a0088deb` |

Changing any byte of this representation requires a new algorithm version rather
than silently changing the meaning of existing idempotency receipts.
