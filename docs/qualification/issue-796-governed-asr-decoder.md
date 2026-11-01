# Issue #796: governed private ASR decoder

Status: **offline implementation; concrete-host runtime qualification is
`NOT_VERIFIED`**.

`PinnedFFmpegPrivateAudioPreparation` implements the private runner's existing
`GovernedPrivateAudioPreparationPort`. It accepts an `AudioRecordingHandle` and
the verified #771 stage-two result, reads bytes only by its opaque `staged_ref`,
re-hashes the exact revision, and asks the injected current-source guard before
and after each content access. It accepts no path, URL, source discovery query,
or refresh operation.

The adapter requires absolute, non-writable FFmpeg and ffprobe executables with
configured SHA-256 identities and an explicit FFmpeg build-version label. It
inspects the bytes through ffprobe and accepts only WAV PCM16/PCM24, MP3 Layer
III, or M4A AAC-LC with exactly one audio stream.
It rejects a mismatch with the admitted media metadata. FFmpeg receives the
bytes over stdin with the `pipe` protocol allowlist, and emits deterministic
mono 16 kHz PCM16 over stdout. The Linux process runner uses a new network
namespace, address-space and CPU limits, a wall-time limit, bounded stdout, and
no core dumps. Missing namespace support fails closed. The ephemeral decoded
store creates owner-only files addressed by opaque object references and
removes the full decode and per-chunk WAV artifacts on release or abort.

Each planned chunk is a deterministic WAV/PCM16 object with its own SHA-256,
stable chunk ID, and original-recording timeline boundaries. Chunk intervals
are taken from the frozen processing profile and cannot exceed 25 seconds.
`AudioPreparationResult` binds the source digest, decoder build digest,
inspection, complete decoded digest/length, observed memory, working-set bound,
and all chunk references.

## Operator evidence required

Before any concrete-host run is considered qualified, an operator must retain
the following evidence for the exact deployment target:

- FFmpeg and ffprobe version output and executable SHA-256 digests;
- the frozen allowlist and processing-profile resource bounds;
- evidence that the decoder ran in the intended isolated worker with no network
  egress, plus the host/container namespace configuration;
- observed peak memory, output bytes, duration, wall time and disk usage for
  supported and rejected synthetic media;
- cleanup and deletion evidence covering success, malformed input, timeout,
  cancellation, revocation and retry;
- proof that denied records cannot reach content reads, decoder execution or
  candidate runtime import.

The repository tests use generated synthetic bytes and fake ports. They do not
qualify an operator host, the installed FFmpeg binaries, no-egress configuration,
or model runtime. Private evaluation reports therefore remain `NOT_VERIFIED`;
this implementation does not authorize real audio access or inference.
