# Issue #799 — unsupported-requirements quality gate

The approved gate was measured against the accepted #674 dataset and deterministic
runner. The result is **NOT_VERIFIED**, not PASS.

The Product/Project Owner approved
`unsupported-requirements-quality-gate:v1` before measurement. The run is bound to
implementation commit `74870353e12508bd9eb0aad342c9ae833fb014af`, #674 dataset
`russian-meeting-transcription-synthetic@1.0.0`, and canonical dataset SHA-256
`0f2c70d2663a6bfb69afc13e34d65683e04d7ddfb1914520b8a1174646a290e7`.

## Measured result

- execution: `12 / 12` labelled atomic observations, no exclusions or failures;
- unsafe-supported count: `0`;
- unsafe-supported rate: `0 / 4 = 0%`;
- unsupported safe-handling recall: `1 / 1 = 100%`;
- supported precision: `7 / 7 = 100%`;
- supported recall: `7 / 8 = 87.5%`;
- clarification-needed recall: `2 / 2 = 100%`;
- inconclusive/abstention recall: `1 / 1 = 100%`.

The observed values do not permit PASS because the pre-approved coverage minimum is
40 atomic items with at least 10 supported, 10 unsupported, 10
clarification-needed and 5 inconclusive/abstention examples. #674 currently supplies
8, 1, 2 and 1 respectively. It also lacks the required three-example contradiction
and stale/denied/missing-authority strata; the other mandatory strata have only one
example each. These are `insufficient_labelled_coverage` and
`missing_mandatory_stratum`, so the aggregate must remain `NOT_VERIFIED` even though
the observed safety metrics did not miss a numerical threshold.

The full confusion matrix, per-class results, exact strata membership, empty
exclusions/failures and pinned runtime identities are in the adjacent JSON report.

## Reproduction

```bash
uv sync --extra dev
uv run python -m spine.evals.unsupported_requirements_runner \
  --tested-commit 74870353e12508bd9eb0aad342c9ae833fb014af
uv run pytest -q tests/evals/test_unsupported_requirements_gate.py
```

The runner verifies that the supplied SHA is an ancestor of the current checkout
and that every qualified implementation/dataset file is byte-identical to that
commit. An unrelated or stale SHA produces a fail-closed `NOT_VERIFIED` report.

## Scope and #646 follow-up

This report updates the outstanding #646 quality-gate status to a measured
`NOT_VERIFIED`; it does not rewrite #646's historical offline functional PASS.
Closing #799 requires an additive, independently reviewed #674 dataset release that
meets the already approved coverage without tuning on these results, followed by a
new pinned execution. ASR WER/CER remains separate. No customer recording, private
payload, external provider, model training, live deployment or production approval
was used or qualified.
