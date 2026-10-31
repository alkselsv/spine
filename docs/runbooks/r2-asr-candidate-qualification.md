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

## Local candidate adapters and operator runner (#758)

The `spine-qualify-asr-offline` command consumes an operator-authored
`asr-candidate-runtime:v1` JSON manifest and a local dataset asset directory:

```bash
spine-qualify-asr-offline \
  --manifest /operator-private/gigaam-runtime.json \
  --dataset-assets /checkout/src/spine/evals/data \
  --output /operator-private/gigaam-qualification.json
```

The command recognizes historical dataset v1.0.0 and preserves its published
digest, but returns `NOT_VERIFIED` without inference because v1 has no typed
per-gate coverage contract. The v2 dataset currently also returns
`NOT_VERIFIED`: category minimums require a separate attributable Protocol
Owner decision and are intentionally unset. Therefore the shipped command does
not load either ASR runtime or perform inference against the current dataset.

When a future frozen dataset has approved minima, the operator must stage each
candidate separately and provide a manifest that pins official source and
runtime commits plus local checkout paths. The runner verifies each checkout's
Git `origin` and `HEAD` without network access and verifies that the installed
candidate module resolves from the pinned, clean runtime checkout before
importing it. The dependency lock uses `asr-candidate-dependency-lock:v1`: it
records the Python version, exact installed distribution set and versions, and
each distribution's installed file-tree digest. The runner checks those values
against local package metadata/files before loading either model runtime.
Stage the locked candidate dependencies in the isolated environment before the
run; do not install packages from the qualification command. Also pin the
complete model asset inventory and SHA-256 hashes, candidate dependency lock,
local preprocessing executable, language/decoder/precision configuration,
host profile, license review, and hash-bound no-egress evidence.
The runner independently checks host facts, namespace/routes/interfaces where
the declared boundary is a network namespace, dataset split and asset hashes,
and the immutable configuration digest before loading a runtime. Reports use
`asr-candidate-qualification:v1`, are written once with mode 0600, and receive
a SHA-256 sidecar. Do not put private host paths, credentials, audio, or raw
runtime logs into a report.

GigaAM uses only the operator-staged local `v3_rnnt` checkpoint and rejects
chunks over 25 seconds. The official loader deserializes the local checkpoint;
an operator must review the staged checkpoint provenance and serialization
risks before any eventual execution. The adapter does not invoke remote code
or call the model downloader. Whisper uses a local `large-v3` directory,
`local_files_only=True`, Russian transcription mode, and an explicit compute
type. Candidate dependencies are lazy imports and are not added to Spine's
ordinary runtime dependencies. No command in routine CI stages assets, installs
candidate runtimes, or enables external network access.

Current local qualification is `NOT_EXERCISED`: no approved model assets,
candidate runtime installations, sufficient v2 category minima, or approved
isolated benchmark evidence are present. A real model run requires the
operator-owned isolated benchmark activity after #758 and #759 are integrated;
this runner and its fake-backed adapter tests are not model-quality evidence.

Two gates also remain deliberately closed pending evidence-contract decisions:
`preprocessing_effects` is `NOT_VERIFIED` until an approved second preprocessing
profile supports a real before/after inference comparison; codec-format WER is
reported separately and is not treated as that comparison. The long-form
recovery check replays the frozen annotated failure boundary against actual
model outputs, but does not establish durable worker retry behavior.
