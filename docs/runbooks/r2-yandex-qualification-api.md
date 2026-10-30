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
- two adapter attempts with one explicit backoff interval;
- bounded request timeout and bounded `Retry-After` handling.

HTTP/provider payloads, signed URLs and authorization headers are absent from
object representations and raised errors. Statuses map only to the existing
content-free Yandex port errors.

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

No live call is part of #741. Real endpoint availability, the acting account ID,
the issued token's actual Disk scope, shared-folder behavior and provider rate
limits remain `NOT_VERIFIED` until a separately approved live qualification run.
The local `.env.qualification` token must never be committed or copied into a
report. Production, customer and personal data remain forbidden.
