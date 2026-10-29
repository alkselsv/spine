"""Build checked-in synthetic WAV fixtures from the pinned case manifest.

This qualification-only tool needs local Piper voice models and the Piper CLI. It
is never imported by Spine or run by the default test suite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import tempfile
from array import array
from pathlib import Path
import wave


SAMPLE_RATE = 22_050
PIPER_VERSION = "1.8.0"


def _read_samples(path: Path) -> array[int]:
    with wave.open(str(path), "rb") as source:
        if source.getframerate() != SAMPLE_RATE or source.getnchannels() != 1:
            raise ValueError("Piper output must be mono at 22050 Hz")
        frames = source.readframes(source.getnframes())
    samples = array("h")
    samples.frombytes(frames)
    return samples


def _write_wav(path: Path, samples: array[int]) -> bytes:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(SAMPLE_RATE)
        target.writeframes(samples.tobytes())
    return path.read_bytes()


def _synthesize(text: str, model: Path, config: Path, output: Path) -> array[int]:
    command = [
        "uv", "tool", "run", "--from", f"piper-tts=={PIPER_VERSION}", "piper",
        "--model", str(model), "--config", str(config), "--output_file", str(output),
        "--noise-scale", "0", "--noise-w-scale", "0",
    ]
    subprocess.run(command, input=text + "\n", text=True, check=True, capture_output=True)
    return _read_samples(output)


def generate(*, manifest: Path, data_root: Path, denis_model: Path, dmitri_model: Path) -> None:
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["audio_sample_rate_hz"] = SAMPLE_RATE
    models = {
        "speaker-a": (denis_model, denis_model.with_suffix(denis_model.suffix + ".json")),
        "speaker-b": (dmitri_model, dmitri_model.with_suffix(dmitri_model.suffix + ".json")),
    }
    audio_root = data_root / "audio"
    audio_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spine-russian-audio-") as temporary:
        scratch = Path(temporary)
        for case in raw["cases"]:
            relative_path = f"audio/{case['case_id']}.wav"
            fixture_path = data_root / relative_path
            if case["kind"] == "corruption":
                payload = b"RIFF\x08\x00\x00\x00WAVEfmt "
                fixture_path.write_bytes(payload)
            elif case["kind"] == "silence":
                samples = array("h", [0]) * round(case["duration_ms"] * SAMPLE_RATE / 1000)
                payload = _write_wav(fixture_path, samples)
            else:
                samples = array("h", [0]) * round(case["duration_ms"] * SAMPLE_RATE / 1000)
                for ordinal, segment in enumerate(case["reference_segments"]):
                    voice = segment.get("anonymous_speaker") or "speaker-a"
                    model, config = models[voice]
                    generated = _synthesize(
                        segment["reference_text"], model, config,
                        scratch / f"{case['case_id']}-{ordinal}.wav",
                    )
                    start_ms = segment["start_ms"]
                    start = round(start_ms * SAMPLE_RATE / 1000)
                    end = min(round(segment["end_ms"] * SAMPLE_RATE / 1000), len(samples))
                    count = min(len(generated), end - start)
                    samples[start:start + count] = generated[:count]
                    segment["end_ms"] = start_ms + round(count * 1000 / SAMPLE_RATE)
                if case["kind"] == "noise":
                    noise = random.Random(674)
                    for index, sample in enumerate(samples):
                        value = sample + noise.randint(-180, 180)
                        samples[index] = max(-32768, min(32767, value))
                payload = _write_wav(fixture_path, samples)
            case["audio_fixture_path"] = relative_path
            case["audio_sha256"] = hashlib.sha256(payload).hexdigest()
            case["synthetic_recipe"] = (
                f"piper-tts:{PIPER_VERSION}; voices denis/dmitri; deterministic noise scales; "
                f"case:{case['case_id']}"
            )
    manifest.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--denis-model", type=Path, required=True)
    parser.add_argument("--dmitri-model", type=Path, required=True)
    args = parser.parse_args()
    generate(
        manifest=args.manifest,
        data_root=args.data_root,
        denis_model=args.denis_model,
        dmitri_model=args.dmitri_model,
    )


if __name__ == "__main__":
    main()
