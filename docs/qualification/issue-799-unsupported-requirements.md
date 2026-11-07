# Issue #799 — unsupported-requirements quality gate

The approved gate was measured by passing the accepted #674 labels and transcript
evidence through the deterministic #682 extraction behavior. The result is
**NOT_VERIFIED**, not PASS.

The Product/Project Owner approved
`unsupported-requirements-quality-gate:v1` before measurement. The run is bound to
implementation commit `97e6fe0bccd8fa6317a112818e71167003e3b4d5`, #674 dataset
`russian-meeting-transcription-synthetic@1.0.0`, and canonical dataset SHA-256
`0f2c70d2663a6bfb69afc13e34d65683e04d7ddfb1914520b8a1174646a290e7`.

## Measured result

- execution: `12 / 12` labelled atomic observations, no exclusions or failures;
- unsafe-supported count: `0`;
- unsafe-supported rate: `0 / 4 = 0%`;
- unsupported safe-handling recall: `1 / 1 = 100%`;
- supported precision: undefined (`0 / 0` predicted supported);
- supported recall: `0 / 8 = 0%`;
- clarification-needed recall: `0 / 2 = 0%`;
- inconclusive/abstention recall: `1 / 1 = 100%`.

The malicious spoken instruction was safely omitted under a typed abstention. It
therefore contributes to unsupported safe-handling recall but is reported as
predicted `inconclusive/abstention`, not as an explicit unsupported rejection.

The observed values do not permit PASS because the pre-approved coverage minimum is
40 atomic items with at least 10 supported, 10 unsupported, 10
clarification-needed and 5 inconclusive/abstention examples. #674 currently supplies
8, 1, 2 and 1 respectively. It also lacks the required three-example contradiction
stratum; every other mandatory stratum has only one example. No candidate was
classified as supported, so supported precision has a zero denominator. These are
`insufficient_labelled_coverage`, `missing_mandatory_stratum` and
`zero_denominator`, so the aggregate must remain `NOT_VERIFIED`. Threshold misses
are not promoted to `FAIL` when the approved measurement preconditions are absent.

The full confusion matrix, per-class results, exact strata membership, empty
exclusions/failures and pinned runtime identities are in the adjacent JSON report.

## Reproduction

```bash
uv sync --extra dev
uv run python -m spine.evals.unsupported_requirements_runner \
  --tested-commit 97e6fe0bccd8fa6317a112818e71167003e3b4d5
uv run pytest -q tests/evals/test_unsupported_requirements_gate.py
```

The runner verifies that the supplied SHA is an ancestor of the current checkout
and that every qualified implementation/dataset file is byte-identical to that
commit. An unrelated or stale SHA produces a fail-closed `NOT_VERIFIED` report.
The adjacent report is retained with its original tested commit identity. When that
identity is unavailable or its qualified paths have changed, record a separate
successor report rather than rewriting the historical artifact.

The original JSON report is historical evidence bound to
`094cf838e240102886b00224b97955e3822f0dfd`. That commit is no longer present in
the available Git object store, so its original measurement cannot be rerun from
this checkout. The historical report is retained byte-for-byte; its SHA-256 is
`eb1e0688576ce5d627db2fedb64164e797cb83ee203e86023bf6460f9a33b2d1`. A separate
successor report, `issue-799-unsupported-requirements-successor-844510f5.json`,
records a new run against reachable commit `844510f5ca17806e1fb17ea2f569f7fc6d7e3356`.
The successor preserves the same NOT_VERIFIED measurement outcome and is not a
replacement identity for the historical run.

## Scope and #646 follow-up

This report updates the outstanding #646 quality-gate status to a measured
`NOT_VERIFIED`; it does not rewrite #646's historical offline functional PASS.
Closing #799 requires an additive, independently reviewed #674 dataset release that
meets the already approved coverage without tuning on these results, followed by a
new pinned execution. ASR WER/CER remains separate. No customer recording, private
payload, external provider, model training, live deployment or production approval
was used or qualified.
