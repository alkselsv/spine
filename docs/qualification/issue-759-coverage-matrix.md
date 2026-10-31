# Issue #759 — v2.0.0 coverage audit

Dataset: `russian-meeting-transcription-synthetic@2.0.0`<br>
Canonical SHA-256: `cb3c36145df14a2cbe4880dd2e5f64424cb4d45cddb504e44833a03dd6931d83`<br>
Coverage schema: `russian-transcription-gate-coverage:v2`

This matrix is computed from the checked-in manifest's 43 typed coverage rows.
“Eligible” and “annotated” count qualification **case IDs**, not independent
source utterance lineages or individual spans. No category has an approved
minimum. A positive annotation count therefore remains `NOT_VERIFIED`; it does
not establish sufficiency. The minimums in the adjacent Protocol Owner packet
are proposals only and are not loaded into this dataset.

| Gate / category | Eligible cases | Annotated cases | Approved minimum | Status | Missing work |
|---|---:|---:|---|---|---|
| Russian transcription / clean | 3 | 3 | None | NOT_VERIFIED | Approved lineage/voice/recipe minimum and more independent qualification utterances. |
| Russian transcription / noisy | 0 | 0 | None | NOT_VERIFIED | Add independent noisy qualification speech; the existing noisy case is development-only. |
| Russian transcription / overlap | 0 | 0 | None | NOT_VERIFIED | Add explicitly timed, synthetic overlapping speech and independent lineages. |
| Russian transcription / long form | 1 | 1 | None | NOT_VERIFIED | Add long-form recordings, distinct chunk boundaries and multiple independent lineages. |
| Critical fragments / amount and currency | 4 | 0 | None | NOT_VERIFIED | Add exact annotated amount/currency spans in new qualified speech. |
| Critical fragments / quantity | 4 | 0 | None | NOT_VERIFIED | Add exact quantity spans in new qualified speech. |
| Critical fragments / date and deadline | 4 | 2 | None | NOT_VERIFIED | Add independent annotated cases; decide the approved minimum. |
| Critical fragments / unit | 4 | 0 | None | NOT_VERIFIED | Add exact unit spans, including unit/value boundaries. |
| Critical fragments / negation | 4 | 1 | None | NOT_VERIFIED | Add independent positive spans across different negation constructions. |
| Critical fragments / obligation | 4 | 3 | None | NOT_VERIFIED | Add independent obligation cases and approved span/case minimum. |
| Critical fragments / permission | 4 | 0 | None | NOT_VERIFIED | Add permissions and contrasts with obligations/prohibitions. |
| Critical fragments / condition | 4 | 1 | None | NOT_VERIFIED | Add independent condition cases and conditional scope annotations. |
| Critical fragments / constraint | 4 | 1 | None | NOT_VERIFIED | Add independent constraints and exact scope annotations. |
| Adversarial inversion / negation | 4 | 1 | None | NOT_VERIFIED | Add independent counterfactual pairs; do not count pair members as independent. |
| Adversarial inversion / bounds | 4 | 0 | None | NOT_VERIFIED | Add lower/upper-bound inversions and paired reference interpretations. |
| Adversarial inversion / permission | 4 | 0 | None | NOT_VERIFIED | Add permission/denial inversions with paired interpretations. |
| Adversarial inversion / inclusion | 4 | 0 | None | NOT_VERIFIED | Add inclusion/exclusion inversions with paired interpretations. |
| Adversarial inversion / obligation | 4 | 1 | None | NOT_VERIFIED | Add independent obligation/optionality or responsible-party inversions. |
| Terminology / all annotated terms | 4 | 1 | None | NOT_VERIFIED | Add independent terms and hard negatives; establish an exhaustive frozen vocabulary. |
| Segment timestamps / boundary error | 4 | 4 | None | NOT_VERIFIED | Approve alignment floor/denominator and add the proposed independent cases/segments. |
| Segment timestamps / alignment coverage | 4 | 4 | None | NOT_VERIFIED | Define unmatched/missing/duplicate policy and verify alignment coverage on every reference segment. |
| Silence / pure silence | 5 | 1 | None | NOT_VERIFIED | Add independent silence fixtures and an approved count; preserve zero-word gate. |
| Silence / verified internal intervals | 5 | 4 | None | NOT_VERIFIED | Add independent speech lineages/intervals and quiet-speech controls. |
| Long form / chunk boundaries | 1 | 1 | None | NOT_VERIFIED | Add independent recordings with varied boundaries; one recording is one lineage. |
| Long form / pause handling | 1 | 1 | None | NOT_VERIFIED | Add pause patterns across independent recordings. |
| Long form / reassembly | 1 | 1 | None | NOT_VERIFIED | Add independent multi-chunk recordings and boundary/duplicate annotations. |
| Long form / partial recovery | 1 | 1 | None | NOT_VERIFIED | Add independent failure checkpoints; #758 also needs real retry/dedup evidence beyond fixture replay. |
| Preprocessing / baseline | 1 | 1 | None | NOT_VERIFIED | Approve paired comparison protocol and minimum independent source lineages. |
| Preprocessing / comparison | 5 | 5 | None | NOT_VERIFIED | Add a separately approved second profile and paired before/after inference; current pairs alone cannot pass. |
| Performance / cold start | 0 | 0 | None | NOT_VERIFIED | Operator-run measurements on an approved, fully identified host. |
| Performance / warm inference | 0 | 0 | None | NOT_VERIFIED | Operator-run measurements by frozen workload and candidate. |
| Performance / preprocessing | 0 | 0 | None | NOT_VERIFIED | Operator-run timing/resource evidence. |
| Performance / inference | 0 | 0 | None | NOT_VERIFIED | Real model inference measurements; fake backends do not qualify. |
| Performance / reassembly | 0 | 0 | None | NOT_VERIFIED | Operator-run timing evidence on the approved workload. |
| Performance / end to end | 0 | 0 | None | NOT_VERIFIED | Complete-run measurements and workload envelope. |
| Performance / resources | 0 | 0 | None | NOT_VERIFIED | Peak RAM/VRAM and failure/headroom evidence from an approved host. |
| Offline integrity / asset loading | 0 | 0 | None | NOT_VERIFIED | Approved staged assets, exact inventory hashes and real local-load evidence. |
| Offline integrity / source provenance | 0 | 0 | None | NOT_VERIFIED | Exact source revisions and provenance verified in the isolated run. |
| Offline integrity / dependency provenance | 0 | 0 | None | NOT_VERIFIED | Pinned runtime locks and installed dependency hashes from the isolated run. |
| Offline integrity / supported WAV/PCM | 5 | 5 | None | NOT_VERIFIED | Approved minimum and real candidate inference; existing WAV cases do not establish quality. |
| Offline integrity / supported MP3 | 1 | 1 | None | NOT_VERIFIED | More independent source lineages and real candidate inference. |
| Offline integrity / supported M4A/AAC-LC | 1 | 1 | None | NOT_VERIFIED | More independent source lineages and real candidate inference. |
| Offline integrity / network egress | 0 | 0 | None | NOT_VERIFIED | Independently verified host boundary and no-egress run evidence. |

## Integrity and provenance findings

- v1.0.0 manifest/audio bytes and historical digest remain unchanged.
- v2.0.0 already has a canonical digest and is treated as immutable. Any new
  fixtures, annotations, eligibility logic or approved minima require a new
  dataset version and digest; do not edit its hash-bound manifest in place.
- MP3 and M4A are both encodings of the same `short-request` lineage. They are
  useful codec cases but not two independent utterances.
- The existing `background-noise` case is on the development split and cannot be
  moved into qualification or reused after tuning. V2 has no overlap fixture.
- Piper TTS 1.8.0 is named in v1 recipe text, but local Piper runtime and
  voice-weight/config hashes are unavailable. FFmpeg 4.4.2 is installed and can
  transform existing audio only; it cannot supply missing speech content.
- No candidate outputs were used to create ground truth. No ASR/TTS assets were
  downloaded and no new speech was synthesized.

See [the Protocol Owner proposal and staged-asset requirements](issue-759-protocol-owner-decision.md).
