# R2 Bitrix24 Disk file qualification

Issue #666 implements the bounded, read-only Bitrix24 Disk file slice. The
adapter admits a target through the provider-neutral connection boundary,
enumerates only the approved folder scope, reads one current Disk object and
emits the existing `external-observation:v2` binary representation.

The offline qualification uses synthetic official-response-shaped fakes. It
proves workspace/scope admission, current provider access failures, bounded
pagination, duplicate replay, signed-link refresh, exact object/version
evidence, immutable canonical revision replay and safe diagnostics. Download
URLs are ephemeral inputs to the service port and never enter observations or
canonical revision metadata.

Run the offline evidence with:

```bash
uv run pytest -q \
  tests/connectors/test_bitrix24_disk.py \
  tests/application/test_connector_ingestion.py
```

Live Bitrix24 qualification is not part of the default suite. It requires an
approved synthetic portal, a governed `disk` connection, an acting-user access
policy and explicit cleanup/authorization before it can be run.

## CRM-linked material (#671)

The CRM material slice is a separate read-only relationship adapter. It admits
only an approved CRM entity type and a current acting-user grant, then records
the exact activity, attachment, Disk file ID and Disk provider revision. It
does not read CRM content, resolve a relationship from a filename/subject, or
grant source disclosure or `PricingBasis` authority.

Offline qualification is synthetic and runs with:

```bash
uv run pytest -q \
  tests/connectors/test_bitrix24_crm_material.py \
  tests/application/test_bitrix24_crm_material_service.py \
  tests/qualification/test_issue671_bitrix24_crm_material.py
```

Acceptance is rejected for an unapproved entity, a cross-workspace target, an
inaccessible or stale Disk revision, or a mismatched activity attachment.
Relationship and source revocation are retained as visible append-only state.
