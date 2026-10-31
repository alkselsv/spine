# Synthetic Russian transcription qualification dataset v2

Dataset: `russian-meeting-transcription-synthetic@2.0.0`<br>
Schema: `russian-transcription-qualification:v2`<br>
Canonical SHA-256: `cb3c36145df14a2cbe4880dd2e5f64424cb4d45cddb504e44833a03dd6931d83`

This version adds a typed split, annotation, preprocessing and gate-coverage
contract. The #758 runner imports the same `RussianTranscriptionDatasetV2`
interface; it does not define a parallel manifest. Case and audio lineage are
immutable, and coverage is counted from named case IDs and validated against
the dataset annotations.

## Frozen v1 baseline

The v1 manifest and audio fixtures are not edited by the v2 generator. Loading
v1 still validates the canonical digest
`0f2c70d2663a6bfb69afc13e34d65683e04d7ddfb1914520b8a1174646a290e7` and checks
its fixture hashes. V2 copies the v1 audio bytes to versioned fixture names;
its only new audio encodings are local FFmpeg MP3 and AAC-LC conversions of the
synthetic `short-request` fixture and 16 kHz mono PCM preprocessing outputs.

The checked-in v1 report says eight fixtures, while the frozen manifest and
audio directory contain nine cases. V2 follows the manifest's nine-case
identity and does not rewrite that historical v1 content.

## Typed evidence in v2

Each case declares its `development` or `qualification` split, stable lineage
group, exact input hash/format, pinned v1 source fixture, expected transcript,
timestamped reference segments, allowed variants, anonymous speaker labels,
critical spans, adversarial meaning labels, terminology spans, verified silence
intervals, chunk/recovery expectations, and preprocessing source/output hashes.
The manifest records 43 gate/category coverage rows. Eligible and annotated
case IDs must exist, annotations must be eligible, and annotated IDs are
recomputed from typed labels. Eligibility is selected by the category's
versioned rule (such as qualification speech cases, long-form cases, or
operator-only evidence); a manifest cannot omit eligible cases. Audio hashes,
WAV metadata, compressed codec/profile/sample rate/duration and declared
zero-amplitude silence are checked before evaluation. Each MP3/M4A input has
its own hash-bound preprocessing output.
Audio digests use SHA-256; the manifest digest is SHA-256 over canonical JSON
(UTF-8, sorted keys, compact separators). Timestamps use milliseconds on the
original source audio timeline. Text span offsets count Unicode code points in
the exact reference segment string.

The local-only generator is:

```bash
PYTHONPATH=src python tools/generate_transcription_v2_audio.py
```

It requires an already installed FFmpeg, records the executable version and
SHA-256, and uses local files only. It does not run Piper, fetch packages, or
download audio/model assets. It writes only v2 assets, manifest and digest; it
does not modify v1 files.

Fixture integrity validation also requires the local `ffprobe` executable to
inspect MP3/M4A codec, profile, sample-rate, channel and duration metadata. The
repository R1 CI job installs the Ubuntu `ffmpeg` package (which includes
`ffprobe`) before running the offline suite; this is a media-inspection tool,
not an ASR/TTS runtime or model asset.

Validate fixture integrity and emit the typed coverage report with:

```bash
PYTHONPATH=src python -m spine.evals.transcription_russian_v2
```

## Observed qualification-split coverage

The `minimum_annotation_count` is intentionally unset for every category:
#661 did not approve numeric annotation-count minima. Counts below describe
what is present; they do not establish statistical sufficiency. Therefore every
coverage result is `NOT_VERIFIED` until an attributable Protocol Owner decision
sets applicable minima and those minima are met.

| Gate/category | Eligible cases | Annotated cases | Coverage |
|---|---:|---:|---|
| Russian clean | 3 | 3 | NOT_VERIFIED |
| Russian noisy | 0 | 0 | NOT_VERIFIED |
| Russian overlap | 0 | 0 | NOT_VERIFIED |
| Russian long form | 1 | 1 | NOT_VERIFIED |
| Critical amount/currency | 4 | 0 | NOT_VERIFIED |
| Critical quantity | 4 | 0 | NOT_VERIFIED |
| Critical date/deadline | 4 | 2 | NOT_VERIFIED |
| Critical unit | 4 | 0 | NOT_VERIFIED |
| Critical negation | 4 | 1 | NOT_VERIFIED |
| Critical obligation | 4 | 3 | NOT_VERIFIED |
| Critical permission | 4 | 0 | NOT_VERIFIED |
| Critical condition | 4 | 1 | NOT_VERIFIED |
| Critical constraint | 4 | 1 | NOT_VERIFIED |
| Adversarial negation | 4 | 1 | NOT_VERIFIED |
| Adversarial bounds | 4 | 0 | NOT_VERIFIED |
| Adversarial permission | 4 | 0 | NOT_VERIFIED |
| Adversarial inclusion | 4 | 0 | NOT_VERIFIED |
| Adversarial obligation | 4 | 1 | NOT_VERIFIED |
| Terminology spans | 4 | 1 | NOT_VERIFIED |
| Timestamp boundary/alignment | 4 | 4 | NOT_VERIFIED |
| Pure silence | 5 | 1 | NOT_VERIFIED |
| Verified silence intervals | 5 | 4 | NOT_VERIFIED |
| Long-form chunk/pause/reassembly | 1 | 1 | NOT_VERIFIED |
| Partial recovery | 1 | 1 | NOT_VERIFIED |
| Preprocessing baseline / comparison | 5 | 1 / 5 | NOT_VERIFIED |
| Supported WAV/PCM | 5 | 5 | NOT_VERIFIED |
| Supported MP3 | 1 | 1 | NOT_VERIFIED |
| Supported M4A/AAC-LC | 1 | 1 | NOT_VERIFIED |
| Performance/resource observations | 0 | 0 | NOT_VERIFIED |
| Runtime/source/dependency/no-egress observations | 0 | 0 | NOT_VERIFIED |

Asset loading, source/runtime/dependency provenance and no-egress are
operator evidence, not fixture labels, and remain 0/0 here.

## Limitations and next data decision

V2 is an honest contract and fixture extension, **not a sufficient ASR quality
qualification set**. Existing speech content is inherited from v1; the format
variants are one utterance each and do not add independent lexical coverage.
Amount/currency, quantity, units, permissions, bounds, inclusion inversions,
noisy speech in qualification, overlapping speech and approved terminology
categories are absent. Exact Piper voice-weight hashes are not recorded for
inherited v1 audio. No candidate output was used to create or annotate ground
truth.

A Protocol Owner must set coverage minima and approve the annotation sufficiency
rule before this data can support any quality-gate PASS. Additional, independently
generated synthetic speech is needed for the missing categories. Such audio must
be staged from approved local voice assets and separately hashed; this generator
does not acquire those assets. The #661 thresholds remain unchanged. Until then,
the #758 runner must report qualification-dependent gates as `NOT_VERIFIED`.
The unresolved minima and alignment-coverage questions are captured in
[`issue-759-protocol-owner-decision.md`](issue-759-protocol-owner-decision.md).
