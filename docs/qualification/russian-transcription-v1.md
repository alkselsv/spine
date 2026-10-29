# Synthetic Russian transcription evaluation v1

Dataset `russian-meeting-transcription-synthetic@1.0.0` is a deterministic,
privacy-safe scoring contract for the R2 transcription path. Its eight checked-in
mono WAV and malformed-media fixtures
cover short and long scripted speech, pauses, background noise, low-volume
unintelligible speech, verified silence, corrupt media, anonymous multiple
speakers, and a malicious spoken instruction. Weak, silent and corrupt cases
carry no transcript hypothesis and expect clarification or abstention. Each
fixture pins its synthetic generation recipe by SHA-256,
reference segment bounds, permissible text variants, anonymous speaker policy,
and (where applicable) requirement labels with exact supporting segment IDs.

Speech fixtures were rendered locally with Piper TTS 1.8.0 using the
`ru_RU-denis-medium` and `ru_RU-dmitri-medium` voices at pinned timestamps.
Their model cards identify CC0 training datasets; see the
[denis model card](https://huggingface.co/rhasspy/piper-voices/blob/v1.0.0/ru/ru_RU/denis/medium/MODEL_CARD)
and [dmitri model card](https://huggingface.co/rhasspy/piper-voices/blob/v1.0.0/ru/ru_RU/dmitri/medium/MODEL_CARD).
The TTS package and voice weights are generation-time tools only and are not
Spine dependencies. To rebuild the WAVs, supply those pinned voice models to
`tools/generate_russian_transcription_audio.py`.

The checked-in fake baseline exercises result normalization and metric
aggregation; it does not run an acoustic recognition model and makes no provider
calls. Candidate ASR qualification runs against these assets with a pinned
model runtime and reports its own model and configuration identities.

Run the offline baseline with:

```bash
uv run spine-eval-transcription
```

The JSON report includes the canonical dataset digest and separates WER, CER,
and exact transcript rate from requirement precision/recall/F1, evidence
precision/recall, and abstention/clarification outcome accuracy. Unsupported or
silent input must not be turned into invented transcript evidence. The
malicious instruction span is reference text, but cannot support an extracted
requirement. Speaker labels are anonymous and are never resolved to people.

Default CI uses only this synthetic dataset and the deterministic fake. No
production recording, customer identity, credential, or external ASR call is
required.
