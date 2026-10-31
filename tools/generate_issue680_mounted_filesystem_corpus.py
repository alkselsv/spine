"""Generate the deterministic synthetic corpus for issue #680 qualification."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import tarfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

PACKAGE_NAME = "issue-680-synthetic-v1"
MIB = 1024 * 1024
FIXED_MTIME = 1_700_000_000


class AdapterProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    default_max_file_bytes: int
    hard_ceiling_bytes: int
    read_chunk_bytes: int
    read_timeout_seconds: int
    scan_timeout_seconds: int
    max_concurrent_reads_per_worker: int
    max_reconciliation_attempts_per_item: int


class CorpusEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    relative_path: str
    type: Literal["regular", "symlink", "fifo"]
    purpose: str
    byte_length: int | None = None
    sha256: str | None = None

    @model_validator(mode="after")
    def validate_content_metadata(self) -> CorpusEntry:
        content_metadata = (self.byte_length, self.sha256)
        if self.type == "regular" and any(value is None for value in content_metadata):
            raise ValueError("regular entries require length and checksum")
        if self.type != "regular" and any(
            value is not None for value in content_metadata
        ):
            raise ValueError("non-regular entries cannot carry content metadata")
        return self


class SyntheticCorpusManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["issue-680-synthetic-corpus:v1"]
    ticket: Literal[680]
    classification: Literal["synthetic_non_production"]
    generator: Literal["deterministic"]
    approved_adapter_profile: AdapterProfile
    entries: tuple[CorpusEntry, ...]


class ScenarioRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    spine_mounts_or_unmounts: Literal[False]
    spine_writes_source: Literal[False]
    backend_operator_controls_mutations: Literal[True]
    expected_identity: Literal["path_scoped"]
    missing_prerequisite_status: Literal["NOT_VERIFIED"]


class SyntheticScenarioPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["issue-680-scenario-plan:v1"]
    ticket: Literal[680]
    rules: ScenarioRules
    phases: tuple[
        Literal["preflight"],
        Literal["containment"],
        Literal["streaming_limits"],
        Literal["rename"],
        Literal["unstable_read"],
        Literal["mount_interruption"],
        Literal["recovery"],
        Literal["cleanup"],
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_pattern(path: Path, size: int, label: str) -> None:
    seed = hashlib.sha256(label.encode("utf-8")).digest()
    block = (seed * ((64 * 1024 // len(seed)) + 1))[: 64 * 1024]
    with path.open("wb") as target:
        remaining = size
        while remaining:
            chunk = block[: min(len(block), remaining)]
            target.write(chunk)
            remaining -= len(chunk)


def _regular_entry(corpus: Path, path: Path, purpose: str) -> CorpusEntry:
    return CorpusEntry(
        relative_path=path.relative_to(corpus).as_posix(),
        type="regular",
        purpose=purpose,
        byte_length=path.stat().st_size,
        sha256=_sha256(path),
    )


def _normalize_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = FIXED_MTIME
    info.pax_headers = {}
    if info.isdir():
        info.mode = 0o755
    elif info.isreg():
        info.mode = 0o644
    elif info.issym():
        info.mode = 0o777
    elif info.isfifo():
        info.mode = 0o600
    return info


def _add_tree(archive: tarfile.TarFile, source: Path, arcname: str) -> None:
    archive.add(
        source,
        arcname=arcname,
        recursive=False,
        filter=_normalize_tar_info,
    )
    if source.is_dir() and not source.is_symlink():
        for child in sorted(source.iterdir(), key=lambda item: item.name):
            _add_tree(archive, child, f"{arcname}/{child.name}")


def _write_archive(package: Path, archive_path: Path) -> None:
    with archive_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(
                fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
            ) as archive:
                _add_tree(archive, package, PACKAGE_NAME)


def generate(output_dir: Path) -> tuple[Path, Path, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    package = output_dir / PACKAGE_NAME
    corpus = package / "corpus"
    archive_path = output_dir / f"{PACKAGE_NAME}.tar.gz"
    checksum_path = output_dir / f"{PACKAGE_NAME}.tar.gz.sha256"
    outside = package / "outside-scope-synthetic.txt"
    targets = (package, archive_path, checksum_path, outside)
    if any(target.exists() or target.is_symlink() for target in targets):
        raise FileExistsError("refusing to overwrite issue #680 qualification data")

    for directory in (
        corpus / "regular",
        corpus / "nested" / "level-one" / "level-two",
        corpus / "limits",
        corpus / "lifecycle",
        corpus / "special",
    ):
        directory.mkdir(parents=True, exist_ok=False)

    stable = corpus / "regular" / "stable-small.txt"
    stable.write_text(
        "Synthetic qualification source for Spine issue 680.\n"
        "No corporate, customer, credential, or production data is present.\n",
        encoding="utf-8",
    )
    nested = corpus / "nested" / "level-one" / "level-two" / "proposal.md"
    nested.write_text(
        "# Synthetic proposal\n\n"
        "This document exists only to exercise nested regular-file discovery.\n",
        encoding="utf-8",
    )
    rename = corpus / "lifecycle" / "rename-before.txt"
    rename.write_text(
        "Synthetic rename candidate. Identity must remain path-scoped.\n",
        encoding="utf-8",
    )
    retention = corpus / "lifecycle" / "canonical-retention-anchor.txt"
    retention.write_text(
        "Synthetic anchor used to prove mount interruption cannot delete canonical state.\n",
        encoding="utf-8",
    )
    chunk_boundary = corpus / "limits" / "chunk-boundary-plus-one.bin"
    exact_default = corpus / "limits" / "default-limit-exact.bin"
    over_default = corpus / "limits" / "default-limit-plus-one.bin"
    unstable = corpus / "lifecycle" / "unstable-read-candidate.bin"
    _write_pattern(chunk_boundary, 64 * 1024 + 1, "issue-680:chunk-boundary")
    _write_pattern(exact_default, 32 * MIB, "issue-680:default-limit-exact")
    _write_pattern(over_default, 32 * MIB + 1, "issue-680:default-limit-plus-one")
    _write_pattern(unstable, 32 * MIB, "issue-680:unstable-read")

    os.symlink("regular/stable-small.txt", corpus / "symlink-internal")
    os.symlink("../outside-scope-synthetic.txt", corpus / "symlink-escape")
    os.mkfifo(corpus / "special" / "synthetic.pipe", mode=0o600)

    regular_files = (
        (stable, "baseline regular read"),
        (nested, "nested traversal"),
        (rename, "path-scoped rename"),
        (retention, "canonical retention across mount interruption"),
        (chunk_boundary, "64 KiB chunk boundary plus one byte"),
        (exact_default, "32 MiB default limit inclusive boundary"),
        (over_default, "32 MiB default limit rejection"),
        (unstable, "operator-controlled unstable read"),
    )
    manifest = SyntheticCorpusManifest(
        schema_version="issue-680-synthetic-corpus:v1",
        ticket=680,
        classification="synthetic_non_production",
        generator="deterministic",
        approved_adapter_profile=AdapterProfile(
            default_max_file_bytes=32 * MIB,
            hard_ceiling_bytes=256 * MIB,
            read_chunk_bytes=64 * 1024,
            read_timeout_seconds=10,
            scan_timeout_seconds=10,
            max_concurrent_reads_per_worker=4,
            max_reconciliation_attempts_per_item=2,
        ),
        entries=(
            *(_regular_entry(corpus, path, purpose) for path, purpose in regular_files),
            CorpusEntry(
                relative_path="symlink-internal",
                type="symlink",
                purpose="reject symlink even when target stays inside root",
            ),
            CorpusEntry(
                relative_path="symlink-escape",
                type="symlink",
                purpose="reject symlink escape without following target",
            ),
            CorpusEntry(
                relative_path="special/synthetic.pipe",
                type="fifo",
                purpose="reject non-regular special file",
            ),
        ),
    )
    (package / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    scenario_plan = SyntheticScenarioPlan(
        schema_version="issue-680-scenario-plan:v1",
        ticket=680,
        rules=ScenarioRules(
            spine_mounts_or_unmounts=False,
            spine_writes_source=False,
            backend_operator_controls_mutations=True,
            expected_identity="path_scoped",
            missing_prerequisite_status="NOT_VERIFIED",
        ),
        phases=(
            "preflight",
            "containment",
            "streaming_limits",
            "rename",
            "unstable_read",
            "mount_interruption",
            "recovery",
            "cleanup",
        ),
    )
    (package / "scenario-plan.json").write_text(
        json.dumps(scenario_plan.model_dump(mode="json"), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    (package / "README.md").write_text(
        "# Issue 680 synthetic qualification corpus v1\n\n"
        "Platform Operations stages this synthetic package on the backend and "
        "exposes the approved leaf read-only to spine-source-reader. Spine must "
        "not extract it into the mounted root, mount or unmount the share, or "
        "mutate source objects. Preserve symlinks and the FIFO. The outside-scope "
        "target is intentionally excluded from the archive.\n",
        encoding="utf-8",
    )

    _write_archive(package, archive_path)
    outside.write_text(
        "Synthetic object outside the corpus root; never package or ingest this file.\n",
        encoding="utf-8",
    )
    checksum = _sha256(archive_path)
    checksum_path.write_text(f"{checksum}  {archive_path.name}\n", encoding="ascii")
    return package, archive_path, checksum


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    package, archive_path, checksum = generate(args.output_dir)
    print(package)
    print(archive_path)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
