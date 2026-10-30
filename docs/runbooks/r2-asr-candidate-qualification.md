# R2 ASR candidate qualification (#683)

The repository contains the provider-neutral report contract and a safe CI
report for both candidates:

```powershell
spine-qualify-asr
```

The command validates the frozen synthetic dataset and emits one report for
GigaAM-v3 `v3_rnnt` and one for Whisper `large-v3` through faster-whisper. The
checked-in result is `NOT_VERIFIED`: CI does not download model assets, run a
model, process production recordings, call an external ASR service or claim
production capacity.

## Operator qualification boundary

An approved benchmark operator must prepare one isolated environment per
candidate and record, before inference:

- the exact official source commit, runtime/dependency lock, checkpoint and
  every staged asset SHA-256;
- the frozen language/configuration digest, `language="ru"`, translation off,
  automatic language detection off, preprocessing, chunking, decoder and
  precision settings;
- CPU/GPU, RAM/VRAM, operating system, Python/runtime versions and a verified
  no-egress boundary. A temporary benchmark host is not production capacity
  evidence;
- the exact frozen #674 dataset digest and development/qualification split.

Tune decoding, preprocessing, chunking, vocabulary hints, VAD and thresholds
only on the development split. Then freeze the complete profile and run each
candidate independently against the qualification split. Never apply hidden
text correction or a fallback model.

## Required report contents

Publish a separate candidate report with all independent gates and category
coverage: Russian WER/CER, critical fragments, adversarial semantic inversions,
terminology precision/recall, segment boundary P50/P95 and alignment coverage,
silence hallucinations, long-form/chunk-boundary behavior, preprocessing
effects, cold/warm/end-to-end timing and RTF, peak RAM/VRAM, supported formats,
asset integrity and offline loading.

Insufficient annotations, missing alignment coverage, unsupported adapter
signals or missing host/asset evidence are `NOT_VERIFIED`, never a pass. A
candidate is `PASS` only when every independent #661 gate passes. One semantic
inversion fails that run. These results are evidence for the human decision in
#690; they do not select a production model or authorize real audio.

The ASR report does not include downstream requirements-extraction quality.
That evidence remains a separate contract owned by #646/#682.
