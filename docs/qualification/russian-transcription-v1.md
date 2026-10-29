# Synthetic Russian transcription evaluation v1

Dataset `russian-meeting-transcription-synthetic@1.0.0` is a deterministic,
privacy-safe scoring contract for the R2 transcription path. Its eight cases
cover short and long scripted speech, pauses, background noise, verified
silence, corrupt media, anonymous multiple speakers, and a malicious spoken
instruction. Each fixture pins its synthetic generation recipe by SHA-256,
reference segment bounds, permissible text variants, anonymous speaker policy,
and (where applicable) requirement labels with exact supporting segment IDs.

The checked-in fake baseline exercises result normalization and metric
aggregation; it does not run an acoustic model and its generated signal recipes
are not a claim of human speech recognition quality. It makes no provider calls.
Candidate ASR qualification requires separately generated/approved synthetic
speech assets and a pinned model runtime; it must report its own model and
configuration identities against this dataset contract.

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
