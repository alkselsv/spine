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
