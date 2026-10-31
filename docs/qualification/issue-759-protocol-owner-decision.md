# Protocol Owner Decision Packet — Synthetic ASR Coverage Sufficiency

Status: **DECISION REQUIRED**. No numeric values are approved by this packet.

## Context

The #661 quality thresholds are frozen and unchanged. They specify model quality
gates, but the reviewed contracts do not specify per-category dataset sample
minima or the minimum alignment coverage required to measure timestamp gates.
The v2 dataset therefore reports every coverage category as `NOT_VERIFIED`.
Choosing values here would create policy rather than implement the existing
contract.

## Decisions requested

The Protocol Owner should record an attributable decision for each item:

1. Minimum number of independent qualification cases for each category in
   `russian-transcription-gate-coverage:v2`.
2. Whether case count alone is sufficient, or whether minima also apply to
   distinct utterances, source speech recipes, speakers, durations, or media
   variants.
3. Minimum reference-segment alignment coverage for timestamp evaluation, and
   how unmatched, missing, duplicated, and unalignable reference segments enter
   its denominator.
4. Whether the same approved minimum applies across all categories or whether
   individual category decisions are required.
5. Decision ID, Protocol Owner identity/authority, approval date, effective
   dataset/evaluator version, and any supersession rule.

The owner may defer a category. A deferred or unanswered category remains
`NOT_VERIFIED`; it cannot be treated as a zero threshold or inferred from WER.

## Current evidence

- Dataset: `russian-meeting-transcription-synthetic@2.0.0`.
- Canonical SHA-256: `cb3c36145df14a2cbe4880dd2e5f64424cb4d45cddb504e44833a03dd6931d83`.
- Required coverage categories: 43.
- Approved category minima: none identified.
- Dataset coverage disposition: all 43 categories `NOT_VERIFIED`.
- Independent speech/audio extension and annotation remain required for the
  categories absent from the frozen qualification split.

This packet does not change thresholds, approve the current data as sufficient,
or authorize candidate inference or production use.
