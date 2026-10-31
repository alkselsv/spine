# R2 Yandex Disk qualification API adapter

Ticket #741 adds the concrete read-only REST implementation behind the existing
`YandexDiskService` port. It is deliberately limited to isolated synthetic
qualification #678 and does not enable a production connector.

## Fixed authority boundary

`YandexQualificationApiBinding` pins one immutable workspace, environment,
connection/version, credential reference, grant reference, authorization and
configuration generation, resource-scope reference, OAuth application and the
exact root `/Spine-Qualification-678-v1`. The expected Yandex user ID is a
`SecretStr` runtime input and is not a public fixture.

Before every Disk operation the adapter resolves the approved external secret
reference and calls `GET https://login.yandex.ru/info?format=json`. Both `id`
and `client_id` must match the binding. A missing or mismatched value fails
closed before any request to `cloud-api.yandex.net`.

Only `disk:/Spine-Qualification-678-v1` and its descendants are readable.
Public resources, trash, application folders, sibling roots, traversal paths,
stale generations and mismatched tenant/reference tuples are rejected before
credential resolution. Download links must use HTTPS and an explicitly allowed
Yandex downloader host; redirects are disabled and the OAuth header is never
forwarded to the signed download URL.

## Operational profile

The immutable profile `spine-yandex-disk-qualification-ops:v1` enforces:

- one in-flight HTTP request;
- one request per second with burst two;
- at most 10 pages and 32 resources;
- at most 256 KiB per file and 2 MiB per provider JSON response;
- three adapter attempts (initial plus two retries) with full-jitter ceilings of
  one and two seconds; only transport/timeouts and HTTP 429 are retryable;
- bounded request timeout and bounded `Retry-After` handling.

HTTP/provider payloads, signed URLs and authorization headers are absent from
object representations and raised errors. Statuses map only to the existing
content-free Yandex port errors.

This qualification probe is intentionally exempt from the connector-ingestion
rule that retains raw provider payloads. It does not create a `SourceRevision`
or canonical source observation, and the approved #700 evidence profile forbids
raw provider responses and protected names/paths in reports, diagnostics and
alerts. The private identity and manifest candidates retain only the minimum
typed values required for local owner approval; `provider_payloads_recorded`
therefore remains `false`. A later ingestion run must preserve its own raw
payload and provenance through the normal observation contract.

## Offline verification

```bash
uv run pytest -q \
  tests/connectors/test_yandex_disk_api.py \
  tests/connectors/test_yandex_disk.py \
  tests/application/test_yandex_disk_reconciliation.py \
  tests/infrastructure/test_yandex_qualification_credentials.py
```

The suite uses synthetic responses and performs no network calls. It proves the
identity-before-Disk order, exact connection and root admission, strict response
mapping, signed-link confinement, streaming byte limit, sanitized error mapping
and the qualification composition's fixed limits.

## Live boundary

No live call was part of #741. No Disk API call is part of the later identity
preflight. Real Disk endpoint
availability, the issued token's actual Disk scope, shared-folder behavior and
provider rate limits remain `NOT_VERIFIED` until the separately approved live
qualification run. The local `.env.qualification` token must never be committed
or copied into a report. Production, customer and personal data remain
forbidden.

### Identity-only preflight

`spine-yandex-identity-preflight` is the only approved bootstrap path for the
private acting-user binding. It has two independent gates: `opt_in: true` in a
strict configuration document and
`SPINE_YANDEX_IDENTITY_PREFLIGHT_OPT_IN=1` in the invoking process. It resolves
only `local-env:yandex-disk-qa:v1` and can issue exactly one request to
`GET https://login.yandex.ru/info?format=json`; it has no Disk operation or
configurable provider endpoint.

The command checks the returned `client_id` against the approved ClientID. On
success it creates, without replacement, a mode-`0600` identity-candidate file
containing the private Yandex User ID. Its separate mode-`0600` report contains
only a SHA-256 identity fingerprint, counts of one identity request and zero
Disk requests, and content-free status fields. It explicitly records that the
raw identity is absent from the report and present only in the private candidate.
If report creation fails, the CLI removes the newly created candidate so the
operator can retry without manual recovery. Neither file may be committed or
attached to GitHub; `.env.qualification.*` is ignored for that reason.

The operator must inspect the candidate locally and explicitly approve that
identity before it is promoted into the runtime API binding. Until that approval
is recorded, no `cloud-api.yandex.net` request is permitted and #678 remains
`NOT_VERIFIED` for Disk behavior.

### Recursive metadata inventory

After the acting identity is approved, the recursive inventory runner is the
first Disk phase. It requires a strict configuration opt-in and a current
tenant-scoped `ProviderConnectionRepository.admit_read` result before resolving
the credential or calling Yandex. It also requires an injected cross-process
execution lease for the exact connection/scope/generations and an injected
request governor shared by all service instances in the local worker. Missing
or contended coordination fails closed before a provider request. No standalone
CLI is exposed until a canonical connection repository composition exists. The
runner performs two complete
recursive metadata traversals of the exact root. Every folder page is path-confined, every
listed item must be a direct child of that page, and the two ordered inventories
must be identical. The two passes share the approved ten-page, 32-resource and
retry budgets. Each pass has a five-minute deadline and the logical run has a
ten-minute deadline; authority is re-admitted before every retry. Cancellation
produces terminal `NOT_VERIFIED` evidence. The run also stops as `NOT_VERIFIED`
if it observes a changing inventory, does not match the approved aggregate
baseline of five files, four child directories and 1,416 bytes, or differs from
the exact owner-approved opaque manifest digest.

This phase does not request download links or file bytes. On success it writes a
mode-`0600` manifest candidate containing the protected exact paths and provider
metadata for local Fixture Owner review. Its separate safe report contains only
aggregate counts, request counts, fixed version identifiers and content-free
status fields. The manifest candidate must never be committed or attached to
GitHub. No content import is permitted until the Fixture Owner has inspected and
explicitly approved that candidate as the exact
`spine-yandex-qualification-fixtures-v1` manifest.

Both the identity report and inventory report carry the exact `workspace_id`
and `environment_id`. The private inventory candidate carries the same tenant,
connection/version and generation binding as the safe report.

Any metadata probe made before canonical Provider Connection admission is
preliminary operator evidence only. It cannot qualify the adapter, authorize a
content download, or be published as a passing #678 result. The admitted rerun's
safe report binds the provider/request-policy versions, connection/version,
authorization/configuration generations, acting-identity fingerprint and opaque
manifest digest; the private candidate carries the same configuration binding.
