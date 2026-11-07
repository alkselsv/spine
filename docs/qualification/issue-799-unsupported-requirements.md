# Issue #799 — unsupported-requirements quality gate

The approved deterministic offline gate is **PASS** for the exact implementation
commit `9bc5c39c20478b01cfc38405d830e3f6d5837398`.

The Product/Project Owner approved
`unsupported-requirements-quality-gate:v1` in Issue #799 before measurement.
The immutable synthetic dataset is
`unsupported-requirements-synthetic:v1`, SHA-256
`a62131c37564e5e6d0cfb95890bb3b530598604b08e9672fb2b6f6940b444746`.
All 40 atomic cases ran through the existing `extract_requirements` v2 capability,
agent version `e5550bfb-3b0e-5a95-98b6-65231a3c1cf9`, and deterministic
implementation `requirements.extract.deterministic` with the default bounded
configuration.

## Result

- unsafe-supported count: `0`;
- unsafe-supported rate: `0 / 30 = 0%`;
- unsupported safe-handling recall: `10 / 10 = 100%`;
- supported precision: `10 / 10 = 100%`;
- supported recall: `10 / 10 = 100%`;
- clarification-needed recall: `10 / 10 = 100%`;
- complete execution: `40 / 40`;
- mandatory strata: contradiction `4 / 4`, missing/fabricated evidence `6 / 6`,
  stale/denied/missing authority `10 / 10`, weak/unknown recognition `4 / 4`,
  malicious instruction `3 / 3`.

The full confusion matrix and exact identities are recorded in
`issue-799-unsupported-requirements.json`.

## Reproduction

```bash
uv sync --extra dev
uv run python -m spine.evals.unsupported_requirements_runner \
  --tested-commit 9bc5c39c20478b01cfc38405d830e3f6d5837398
uv run pytest -q tests/evals/test_unsupported_requirements_gate.py
```

The committed-artifact regression test regenerates the report and requires exact
equality with the JSON evidence.

## Scope and linkage

This closes only the deterministic synthetic unsupported-requirements gate left
`NOT_VERIFIED` by #646. It does not rewrite the historical #646 functional report.
ASR WER/CER remains separate and cannot satisfy this gate. No customer recording,
production identity, private payload, external provider, model training, live
deployment, or production approval was used or qualified.
