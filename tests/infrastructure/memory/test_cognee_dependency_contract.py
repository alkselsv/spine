from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


REPOSITORY_ROOT = Path(__file__).parents[3]
BASELINE_COGNEE_VERSION = "1.5.4"
AUDITED_COGNEE_VERSION = BASELINE_COGNEE_VERSION
REQUIRED_UPGRADE_GATES = {
    "acl-leakage": "tests/contracts/memory/test_acl_leakage_contract.py",
    "backend-contract": "tests/contracts/memory/test_backend_contract.py",
    "deletion": "tests/contracts/memory/test_deletion_contract.py",
    "projection-rebuild": "tests/contracts/memory/test_projection_rebuild_contract.py",
    "provenance-mapping": "tests/contracts/memory/test_provenance_mapping_contract.py",
    "retrieval": "tests/contracts/memory/test_retrieval_contract.py",
}


def _load_toml(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _declared_upgrade_gates(policy: str) -> dict[str, str]:
    gates: dict[str, str] = {}
    for line in policy.splitlines():
        if (
            not line.startswith("- `")
            or "`: `" not in line
            or not line.endswith("`")
        ):
            continue
        gate, test_path = line.removeprefix("- `").removesuffix("`").split("`: `")
        gates[gate] = test_path
    return gates


def _missing_upgrade_suites(
    audited_version: str, repository_root: Path = REPOSITORY_ROOT
) -> list[str]:
    if audited_version == BASELINE_COGNEE_VERSION:
        return []
    return [
        test_path
        for test_path in REQUIRED_UPGRADE_GATES.values()
        if not (repository_root / test_path).is_file()
    ]


def test_cognee_dependency_is_pinned_to_the_audited_public_contract() -> None:
    project = _load_toml(REPOSITORY_ROOT / "pyproject.toml")["project"]

    cognee_dependencies = [
        dependency
        for dependency in project["dependencies"]
        if dependency.startswith("cognee")
    ]

    assert cognee_dependencies == [f"cognee=={AUDITED_COGNEE_VERSION}"]


def test_cognee_lockfile_matches_the_declared_audited_contract() -> None:
    lock = _load_toml(REPOSITORY_ROOT / "uv.lock")
    packages = lock["package"]
    cognee_package = next(package for package in packages if package["name"] == "cognee")
    spine_package = next(package for package in packages if package["name"] == "spine")
    cognee_requirement = next(
        requirement
        for requirement in spine_package["metadata"]["requires-dist"]
        if requirement["name"] == "cognee"
    )

    assert cognee_package["version"] == AUDITED_COGNEE_VERSION
    assert cognee_requirement["specifier"] == f"=={AUDITED_COGNEE_VERSION}"


def test_cognee_upgrade_policy_requires_all_contract_gates() -> None:
    policy = (REPOSITORY_ROOT / "docs/cognee-upgrade-policy.md").read_text(
        encoding="utf-8"
    )
    declared_gates = _declared_upgrade_gates(policy)

    assert declared_gates == REQUIRED_UPGRADE_GATES


def test_future_cognee_upgrade_requires_executable_contract_suites() -> None:
    missing_suites = _missing_upgrade_suites(AUDITED_COGNEE_VERSION)

    assert not missing_suites, (
        "Cognee upgrades require executable contract suites: "
        + ", ".join(missing_suites)
    )


def test_dependency_guard_rejects_an_upgrade_without_contract_suites(
    tmp_path: Path,
) -> None:
    assert set(_missing_upgrade_suites("1.5.5", tmp_path)) == set(
        REQUIRED_UPGRADE_GATES.values()
    )
