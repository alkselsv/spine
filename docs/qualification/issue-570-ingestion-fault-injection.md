# Issue #570 — isolated ingestion fault injection

Fault injection is disabled unless `SPINE_INGESTION_FAULT_PROFILE_JSON` is set
on the isolated qualification API deployment. The v1 profile is immutable and
bound to the running Q&A deployment profile ID, version and fingerprint. Startup
fails closed if any binding differs. Both profile IDs must begin with
`qualification-`; it contains only synthetic workspace/environment IDs,
server-generated command identities, one of the three fixed boundaries and its
matching safe reason code.

The profile may arm several distinct commands in one deployment. Each rule is
consumed once per API process, and the ingestion failure ledger prevents a
second API replica from reinjecting a fault after its failure is durable. Do not
configure these profiles on a customer deployment. A restart resets process-local
one-shot state, so each qualification command must be unique within that run.

Example shape (replace all identifiers with the approved isolated deployment's
non-secret references):

```json
{
  "schema_version": "spine-ingestion-fault-profile:v1",
  "profile_id": "qualification-issue561",
  "profile_version": "r1",
  "deployment_profile_id": "qualification-q16-01",
  "deployment_profile_version": "r1",
  "deployment_fingerprint": "<64 lowercase hex characters>",
  "rules": [
    {
      "workspace_id": "<isolated workspace UUID>",
      "environment_id": "<isolated environment UUID>",
      "command_identity": "api.document.upload.<32 lowercase hex characters>",
      "boundary": "parser",
      "reason_code": "parser_retryable"
    },
    {
      "workspace_id": "<isolated workspace UUID>",
      "environment_id": "<isolated environment UUID>",
      "command_identity": "api.document.upload.<32 lowercase hex characters>",
      "boundary": "original_storage",
      "reason_code": "original_storage_unavailable"
    },
    {
      "workspace_id": "<isolated workspace UUID>",
      "environment_id": "<isolated environment UUID>",
      "command_identity": "api.document.upload.<32 lowercase hex characters>",
      "boundary": "pre_acceptance",
      "reason_code": "pre_acceptance_retryable"
    }
  ]
}
```

For the single-document API, `command_identity` is derived from the upload
`Idempotency-Key` as `api.document.upload.` plus the first 32 lowercase hex
characters of its SHA-256 digest. The administrator UI uses the batch upload
API; for a one-file run its item identity is
`api.document.batch-upload.` plus the first 24 lowercase hex characters of the
batch key's SHA-256 digest plus `.item-1`. The profile never receives request
content, filename, credentials or exception text. Choose a separate synthetic
idempotency key for each rule.

The parser and original-storage rules fail during processing after canonical
acceptance and create a retryable ingestion failure with a stable reason code.
Read status through the returned ingestion status URL, then use the existing
`POST /api/v1/documents/processing/retry` operator route with that failure ID and
the same workspace/environment/source/revision references. The one-shot fault is
cleared, and the retry command is idempotent by its `Idempotency-Key`.

The pre-acceptance rule fails after the immutable original is written and
admission is recorded, but before canonical acceptance. Status reports
`pre_acceptance_retryable`; the revision is not accepted or current. Retry the
same upload through `POST /api/v1/documents/uploads` with the original
`Idempotency-Key`. The existing original receipt is reused and only a successful
canonical acceptance can advance current state.
