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

## Proposed decision text for Protocol Owner review

**Status: PROPOSED — NOT APPROVED.** The Project Owner approved the #661 quality
thresholds and evaluation policy, but the repository does not identify an
attributable Protocol Owner decision approving sample minima. The values below
are therefore a proposal for signature only. They must not be copied into the
dataset manifest as approved minima until an authorized person records the
decision and effective version.

### Proposed minimums

| Gate/category | Proposed minimum qualification evidence | Why this minimum is useful | Current limitation |
|---|---|---|---|
| Russian WER/CER, separately for clean, noisy, overlap and long-form | 100 independent utterance lineages per category; at least 10 approved synthetic voice profiles and 10 speech-recipe families across the category; report word/character denominators and a lineage-cluster bootstrap interval | 100 distinct utterances provides a nontrivial corpus denominator and supports category-specific resampling. Re-encodes and noise variants of one source utterance remain one lineage. | Current qualification split has 3 clean and 1 long-form lineage; noisy and overlap each have 0. |
| Each critical fragment category, including amount/currency, quantity, date/deadline, unit, negation, obligation, permission, condition and constraint | 150 independently generated positive spans per category, across at least 100 utterance lineages and 10 approved voice profiles; publish exact numerator/denominator and per-category errors | With zero misses, 150 independent Bernoulli opportunities give a one-sided 95% upper miss-rate bound of about 1.98% (rule-of-three approximation), close to the fixed 98% exact-match target. | Current categories have 0–3 annotated cases; several categories have no positive span. |
| Each adversarial dimension: negation, bounds, permission, inclusion and obligation | 150 independent counterfactual pairs per dimension. Count a pair once as the sampling unit; an inversion in any pair is a run failure under #661. | Zero inversions across 150 independent pairs gives an approximate one-sided 95% upper inversion-rate bound of 1.98%; the strict zero-inversion rule remains unchanged. Pair members and paraphrases are not separate independent samples. | Current coverage is one negation and one obligation case; bounds, permission and inclusion have none. |
| Terminology precision/recall | At least 100 positive terminology spans over at least 20 canonical terms (at least 5 distinct contexts per term) and 100 explicitly annotated hard-negative/distractor contexts; report micro-average and per-term/category results | Positive contexts support recall; explicit negative opportunities and observed predictions make false positives auditable. 100 opportunities also avoids treating one or two terms as a representative vocabulary. | Current data has one annotated terminology case and no declared exhaustive vocabulary or hard-negative inventory. |
| Segment timestamp P50/P95 | At least 100 independent speech lineages and 1,000 reference segments; 100% of eligible reference segments receive an annotation attempt; at least 98% must be uniquely alignable before quantile qualification | At 1,000 observations, the empirical P95 is informed by roughly 50 tail observations. The 98% annotation alignment floor prevents reporting quantiles from a selectively easy subset. | Four eligible cases currently have segment timestamps, but there is no approved alignment rule or denominator policy. |
| Silence: pure-silence and verified in-speech intervals | At least 150 independent pure-silence fixtures and at least 150 verified silence intervals across at least 100 independent speech lineages; include separate quiet-speech controls | With zero observed false words, 150 independent trials bound the event rate to roughly 2% at one-sided 95% under the independence approximation; quiet-speech controls detect a system that suppresses speech to satisfy silence. | One pure-silence fixture and four cases with internal intervals are present; quiet-speech coverage is not a qualification category. |
| Long-form boundaries, pause handling, reassembly and recovery fixtures | At least 30 independent long-form lineages, at least 3 annotated chunk boundaries per lineage, and 30 synthetic partial-failure checkpoints with expected final segment identities | Thirty recordings cover varied boundaries and pauses without counting chunks from one recording as independent recordings. Recovery cases exercise several checkpoint positions. | One long-form lineage is present. The current #758 runner does not inject a durable worker failure/retry or prove deduplication, so its recovery gate must still remain `NOT_VERIFIED`. |
| Supported WAV/PCM, MP3 and M4A/AAC-LC | At least 100 independent source lineages per format, with exact source/decoded hashes and per-format metrics; paired encodings of one speech source count as one lineage | Codec behavior is measured on a varied lexical set; format re-encodes cannot inflate the independent utterance count. | WAV has 5 cases; MP3 and M4A each have one variant of the same short-request lineage. |
| Preprocessing effects | At least 100 paired independent lineages per approved profile pair, using identical source speech and a frozen comparison protocol | Paired inference isolates preprocessing changes from lexical and speaker variation. | Current v2 has source/preprocessed audio pairs, but no approved second profile; #758 correctly leaves the measurement gate `NOT_VERIFIED`. |
| Performance and offline-integrity observations | Operator evidence, not fixture-count minima: one complete, reproducible isolated run per candidate and host/configuration, with cold/warm measurements, resource samples, asset hashes, dependency/source provenance and independently verifiable no-egress evidence | These claims are properties of the executed candidate and host. Synthetic fixture labels cannot substitute for runtime observations. | No candidate runtime/assets or approved benchmark host evidence is staged. |

These proposed counts are floors for dataset eligibility, not guarantees of
representativeness and not replacements for any #661 threshold. The 95% bounds
above assume independent sampling units; synthetic cases sharing text templates,
voice profiles, recipes or source audio are clustered and reduce effective sample
size. Reports should include the cluster structure and bootstrap intervals by
utterance lineage. A small synthetic qualification set cannot establish a zero
future-error rate, generalize to human speakers, or replace production monitoring
and human review. The #661 point thresholds and strict zero-inversion rule remain
unchanged; intervals are additional uncertainty reporting unless the Protocol
Owner separately approves an acceptance-rule change.

### Denominator and split rules proposed for approval

1. An independent unit is a unique source-utterance lineage, not an audio file.
   Codec versions, resampled files, added-noise versions, chunks, segment slices,
   and counterfactual pair members retain the source lineage and do not multiply
   the case count. A new wording is independent only when its semantic template
   and source generation recipe are independently authored and versioned.
2. Assign whole lineage groups and semantic-template families to one split.
   Qualification text, voice profile, recipe family and derived audio may not
   leak into development under another case ID. Tuning or configuration changes
   after viewing qualification results require a new dataset version and a new
   untouched qualification split.
3. For timestamp alignment, the denominator is every reference segment in every
   eligible qualification case. Dataset-side unalignable annotations count
   against the 98% alignment-coverage floor and cannot be silently removed.
   Duplicate reference IDs are invalid data. Candidate missing references,
   unmatched references and duplicated/extra predicted segments must be counted
   and reported; they cannot be excluded from the denominator. If eligible
   annotation alignment is below 98%, timestamp quantiles are `NOT_VERIFIED`.
   The Protocol Owner must confirm whether a missing candidate boundary also
   makes the timestamp gate fail or only contributes to the separately reported
   coverage/error counts; no such rule is encoded here as approved.
4. Case minima are applied to unique source lineages; annotation minima are
   applied to distinct target spans/pairs. A case with several repeated spans
   cannot satisfy an independent-case minimum by itself.
5. Development results can choose preprocessing/chunk settings; qualification
   results are read-only evidence. Any reuse for tuning retires that qualification
   split for subsequent claims.

### Decision authority and signature

No named Protocol Owner authority for these numerical sufficiency choices was
found in #661, #674, #683, or the current dataset contract. This proposal is not
an approval by Project Owner `dima55279`, Product Owner `Kraken-777-bit`, or this
implementation PR. Complete the following only after the authorized Protocol
Owner decides:

- Protocol Owner identity and authority reference: ____________________
- Decision ID and date: ____________________
- Approved / rejected / deferred per proposed category: ____________________
- Accepted deviations, if any, and rationale: ____________________
- Effective dataset/evaluator schema versions: ____________________
- Supersedes decision: ____________________
- Signature/attributable GitHub decision link: ____________________

Until that decision exists, every category remains `NOT_VERIFIED` and v2.0.0
must not be edited to add minima or fixtures. New speech examples require a new
versioned dataset artifact and digest. In addition, v2.0.0 currently stores
`annotated_case_ids` and one `minimum_annotation_count`; it does not separately
encode a minimum count of annotation spans, unique source lineages, voice
profiles, or recipe families. The next schema must represent those counts
separately (with derived and auditable membership) before the proposed decision
can be enforced without conflating an utterance with an annotation.

## Synthetic voice asset staging requirements

Local inventory found FFmpeg 4.4.2, but no installed Piper runtime and no locally
staged Piper voice-weight/config assets in the checked local model-cache
locations. No TTS asset was downloaded or used. FFmpeg can deterministically
re-encode or preprocess existing fixtures; it cannot create independent Russian
speech or missing lexical content.

Before generating new independent speech, an operator must stage and approve:

- Piper TTS runtime `1.8.0` (matching the frozen v1 recipe), with exact source
  revision or wheel filename and SHA-256, dependency lock, and offline install
  inventory.
- The official Russian `ru_RU/denis/medium` and `ru_RU/dmitri/medium` model
  weights and matching `.onnx.json` configuration files referenced by the v1
  model cards, each with source revision/URL, license review, byte length and
  SHA-256. No checkpoint/config hash is currently present in v1/v2 provenance.
- Any additional approved Russian voice profiles needed to meet the proposed
  diversity floor, each with its own source, license, model/config hashes and
  deterministic settings. Do not infer a new voice's identity from a file name.
- Frozen text/recipe manifest, seed/noise settings, Piper options, output sample
  format, command line and generator revision. The transcription reference must
  be manually authored/verified against the synthetic text recipe, never copied
  from ASR output.
- An asset manifest reviewed for integrity, commercial/use licensing and
  content/identity safety before the dataset-generation environment is allowed
  to see the files.

Until these prerequisites are staged, new independent lexical examples for
amounts, quantities, units, permissions, constraints, adversarial bounds and
inclusion, noisy speech and overlap cannot be generated from locally approved
assets. Existing WAV/MP3/M4A files remain usable only as the frozen historical
fixtures they are; re-encoding them does not fill independent lexical coverage.
