"""Versioned, fail-closed applicability policy for the R1 regression gate.

This module describes which suites and execution evidence are required for a
change.  It deliberately does not execute product or provider integrations.
The mandatory offline safety suite is invoked by :func:`run_r1_applicability_gate`
on every invocation; changed-file classification can only add requirements.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Final, Literal

from pydantic import Field, ValidationError, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.evals.offline_runner import DatasetIdentity, EvaluatorIdentity


R1_GATE_APPLICABILITY_VERSION: Final[str] = "r1-gate-applicability:v1"
R1_GATE_APPLICABILITY_REPORT_VERSION: Final[str] = "r1-gate-applicability-report:v1"
DEFAULT_R1_MANIFEST_ID: Final[str] = "r1-gate-applicability-default"
MANDATORY_OFFLINE_SUITE_ID: Final[str] = "r1-offline-safety"
MANDATORY_OFFLINE_SUITE_VERSION: Final[str] = "v1"
MANDATORY_OFFLINE_EVIDENCE_ID: Final[str] = "offline-regression-gate:v1"

ChangeClass = Literal[
    "parser_locator",
    "source_revision",
    "projection_publication",
    "retrieval",
    "acl",
    "prompt",
    "model",
    "capability",
    "evaluator",
    "dataset",
    "gate_report",
]
ProviderQualityStatus = Literal["not-applicable", "not-confirmed"]
EvidenceStatus = Literal["passed", "failed", "unavailable"]

SUPPORTED_CHANGE_CLASSES: Final[tuple[ChangeClass, ...]] = (
    "parser_locator",
    "source_revision",
    "projection_publication",
    "retrieval",
    "acl",
    "prompt",
    "model",
    "capability",
    "evaluator",
    "dataset",
    "gate_report",
)
_REQUIRED_COMPONENTS: Final[frozenset[str]] = frozenset(
    {
        "parser",
        "source-revision",
        "projection",
        "retrieval",
        "acl",
        "prompt",
        "model",
        "capability",
        "evaluator",
        "dataset",
        "gate",
    }
)
_EXPECTED_EVALUATORS: Final[dict[str, str]] = {
    "retrieval.recall_at_k": "v1",
    "retrieval.mrr": "v1",
    "qa.answer_outcome": "v1",
    "qa.citations": "v1",
    "qa.citation_precision": "v1",
    "qa.citation_recall": "v1",
    "qa.locator_validity": "v1",
    "qa.groundedness": "v1",
}
_EXPECTED_DATASET = DatasetIdentity(
    dataset_id="qa-r1-synthetic",
    version="1.0.0",
    digest="4f8ccc5202ea3471c0ffbc06dd2b534ecdc38cb498724e2da801181543778141",
)


class VersionPin(DefinitionModel):
    """A named immutable component version recorded in gate evidence."""

    key: str = Field(min_length=1, max_length=128)
    version: VersionIdentifier


class SuiteRequirement(DefinitionModel):
    """One versioned suite and the evidence it must produce."""

    suite_id: VersionIdentifier
    suite_version: VersionIdentifier
    mandatory: bool = True
    evidence_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence_ids(self) -> "SuiteRequirement":
        if any(not evidence_id.strip() for evidence_id in self.evidence_ids):
            raise ValueError("suite evidence identifiers must be non-empty")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("suite evidence identifiers must be unique")
        return self


class ChangeClassRule(DefinitionModel):
    """Path-independent binding from a change class to versioned coverage."""

    change_class: ChangeClass
    component_key: str = Field(min_length=1, max_length=128)
    suite_ids: tuple[VersionIdentifier, ...] = Field(min_length=1)
    path_prefixes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_rule(self) -> "ChangeClassRule":
        if any(not prefix.strip() for prefix in self.path_prefixes):
            raise ValueError("change classification prefixes must be non-empty")
        if len(set(self.suite_ids)) != len(self.suite_ids):
            raise ValueError("change-class suite identifiers must be unique")
        return self


class ApplicabilityManifest(DefinitionModel):
    """The immutable, versioned policy consumed by the R1 gate."""

    schema_version: Literal[R1_GATE_APPLICABILITY_VERSION] = R1_GATE_APPLICABILITY_VERSION
    manifest_id: VersionIdentifier
    component_versions: tuple[VersionPin, ...] = Field(min_length=1)
    evaluator_versions: tuple[EvaluatorIdentity, ...] = Field(min_length=1)
    dataset_versions: tuple[DatasetIdentity, ...] = Field(min_length=1)
    suites: tuple[SuiteRequirement, ...] = Field(min_length=1)
    change_classes: tuple[ChangeClassRule, ...] = Field(min_length=1)
    mandatory_suite_id: VersionIdentifier = MANDATORY_OFFLINE_SUITE_ID


class ExecutionEvidence(DefinitionModel):
    """Disclosure-safe evidence that one manifest-bound suite actually ran."""

    evidence_id: str = Field(min_length=1, max_length=256)
    suite_id: VersionIdentifier
    suite_version: VersionIdentifier
    manifest_id: VersionIdentifier
    manifest_version: str = Field(min_length=1, max_length=128)
    status: EvidenceStatus
    component_versions: tuple[VersionPin, ...] = Field(min_length=1)
    evaluator_versions: tuple[EvaluatorIdentity, ...] = Field(min_length=1)
    dataset_versions: tuple[DatasetIdentity, ...] = Field(min_length=1)


FailureCode = Literal[
    "stale_manifest",
    "incomplete_manifest",
    "inconsistent_manifest",
    "evaluator_catalog_mismatch",
    "dataset_catalog_mismatch",
    "unknown_change_class",
    "missing_execution_evidence",
    "unexpected_execution_evidence",
    "stale_execution_evidence",
    "evidence_suite_mismatch",
    "evidence_failed",
    "mandatory_offline_unavailable",
]


class ApplicabilityFailure(DefinitionModel):
    """Safe reason why applicability cannot be accepted."""

    code: FailureCode
    message: str = Field(min_length=1, max_length=512)
    change_class: ChangeClass | None = None
    suite_id: str | None = None
    evidence_id: str | None = None


class ApplicabilityReport(DefinitionModel):
    """Machine-readable applicability decision and its exact inputs."""

    schema_version: Literal[R1_GATE_APPLICABILITY_REPORT_VERSION] = (
        R1_GATE_APPLICABILITY_REPORT_VERSION
    )
    policy_version: Literal[R1_GATE_APPLICABILITY_VERSION] = R1_GATE_APPLICABILITY_VERSION
    manifest_id: str
    changed_classes: tuple[ChangeClass, ...] = ()
    selected_suites: tuple[SuiteRequirement, ...] = ()
    selected_suite_ids: tuple[str, ...] = ()
    required_evidence_ids: tuple[str, ...] = ()
    execution_evidence_ids: tuple[str, ...] = ()
    component_versions: tuple[VersionPin, ...] = ()
    evaluator_versions: tuple[EvaluatorIdentity, ...] = ()
    dataset_versions: tuple[DatasetIdentity, ...] = ()
    mandatory_offline_executed: bool
    provider_quality_status: ProviderQualityStatus
    passed: bool
    failures: tuple[ApplicabilityFailure, ...] = ()


def _pin(key: str, version: str) -> VersionPin:
    return VersionPin(key=key, version=version)


def _suite(
    suite_id: str,
    evidence_ids: tuple[str, ...],
    *,
    mandatory: bool = True,
) -> SuiteRequirement:
    return SuiteRequirement(
        suite_id=suite_id,
        suite_version="v1",
        mandatory=mandatory,
        evidence_ids=evidence_ids,
    )


def build_default_r1_applicability_manifest() -> ApplicabilityManifest:
    """Build the checked-in R1 policy from explicit version pins."""

    suites = (
        _suite(MANDATORY_OFFLINE_SUITE_ID, (MANDATORY_OFFLINE_EVIDENCE_ID,)),
        _suite("issue-194-ingestion", ("issue-194:qualification:v1",)),
        _suite("issue-90-projection-lifecycle", ("issue-90:qualification:v1",)),
        _suite("issue-211-context-projection", ("issue-211:qualification:v1",)),
        _suite("g02-retrieval-acl-safety", ("g02:execution:v1",)),
        _suite("g03-answer-groundedness", ("g03:execution:v1",)),
        _suite("g06-adapter-parity", ("g06:execution:v1",)),
        _suite("r1-evaluator-contract", ("r1-evaluator-catalog:v1",)),
        _suite("r1-dataset-contract", ("qa-r1-dataset:v1",)),
        _suite("r1-gate-applicability", ("r1-gate-applicability:v1",)),
    )
    return ApplicabilityManifest(
        manifest_id=DEFAULT_R1_MANIFEST_ID,
        component_versions=(
            _pin("parser", "issue-194:v1"),
            _pin("source-revision", "source-revision:v1"),
            _pin("projection", "context-projection:v1"),
            _pin("retrieval", "context-broker:v1"),
            _pin("acl", "access-policy:v1"),
            _pin("prompt", "prompt-version:v1"),
            _pin("model", "model-profile:v1"),
            _pin("capability", "answer-question:v1"),
            _pin("evaluator", "r1-evaluator-catalog:v1"),
            _pin("dataset", "qa-r1-synthetic:1.0.0"),
            _pin("gate", R1_GATE_APPLICABILITY_VERSION),
        ),
        evaluator_versions=tuple(
            EvaluatorIdentity(evaluator_key=key, evaluator_version=version)
            for key, version in sorted(_EXPECTED_EVALUATORS.items())
        ),
        dataset_versions=(_EXPECTED_DATASET,),
        suites=suites,
        change_classes=(
            ChangeClassRule(
                change_class="parser_locator",
                component_key="parser",
                suite_ids=("issue-194-ingestion",),
                path_prefixes=("src/spine/ingest/", "src/spine/ingestion/", "tests/ingest/", "tests/ingestion/"),
            ),
            ChangeClassRule(
                change_class="source_revision",
                component_key="source-revision",
                suite_ids=("issue-194-ingestion",),
                path_prefixes=("src/spine/domain/source", "src/spine/application/canonical", "tests/domain/source", "tests/application/canonical"),
            ),
            ChangeClassRule(
                change_class="projection_publication",
                component_key="projection",
                suite_ids=("issue-90-projection-lifecycle", "issue-211-context-projection"),
                path_prefixes=("src/spine/memory/", "src/spine/application/projection", "tests/memory/", "tests/application/projection", "tests/contracts/memory/"),
            ),
            ChangeClassRule(
                change_class="retrieval",
                component_key="retrieval",
                suite_ids=("g02-retrieval-acl-safety", "g06-adapter-parity"),
                path_prefixes=("src/spine/memory/retrieval", "src/spine/application/retrieval", "tests/memory/retrieval", "tests/application/retrieval"),
            ),
            ChangeClassRule(
                change_class="acl",
                component_key="acl",
                suite_ids=("g02-retrieval-acl-safety", "g06-adapter-parity"),
                path_prefixes=("src/spine/auth/", "src/spine/application/authorized", "src/spine/application/access", "tests/auth/", "tests/application/authorized"),
            ),
            ChangeClassRule(
                change_class="prompt",
                component_key="prompt",
                suite_ids=(MANDATORY_OFFLINE_SUITE_ID,),
                path_prefixes=("src/spine/agents/prompts/", "src/spine/agents/prompt", "tests/agents/prompt"),
            ),
            ChangeClassRule(
                change_class="model",
                component_key="model",
                suite_ids=(MANDATORY_OFFLINE_SUITE_ID,),
                path_prefixes=("src/spine/agents/runtime/model", "src/spine/models/", "tests/agents/runtime/model"),
            ),
            ChangeClassRule(
                change_class="capability",
                component_key="capability",
                suite_ids=("g03-answer-groundedness", "g06-adapter-parity"),
                path_prefixes=("src/spine/domain/capabilities/", "src/spine/application/answer", "tests/domain/capabilities/", "tests/application/answer"),
            ),
            ChangeClassRule(
                change_class="evaluator",
                component_key="evaluator",
                suite_ids=("r1-evaluator-contract", MANDATORY_OFFLINE_SUITE_ID),
                path_prefixes=("src/spine/evaluations/", "src/spine/evals/", "tests/evaluations/", "tests/evals/"),
            ),
            ChangeClassRule(
                change_class="dataset",
                component_key="dataset",
                suite_ids=("r1-dataset-contract", MANDATORY_OFFLINE_SUITE_ID),
                path_prefixes=("src/spine/evals/data/", "tests/evals/data/", "docs/evals/"),
            ),
            ChangeClassRule(
                change_class="gate_report",
                component_key="gate",
                suite_ids=("r1-gate-applicability", MANDATORY_OFFLINE_SUITE_ID),
                path_prefixes=(".github/", "docs/qualification/", "src/spine/evals/regression_gate.py", "src/spine/evals/applicability.py", "src/spine/evaluations/release_gates/", "tests/qualification/"),
            ),
        ),
    )


DEFAULT_R1_APPLICABILITY_MANIFEST = build_default_r1_applicability_manifest()


def classify_changed_files(
    changed_files: Sequence[str],
    *,
    manifest: ApplicabilityManifest = DEFAULT_R1_APPLICABILITY_MANIFEST,
) -> tuple[ChangeClass, ...]:
    """Classify paths conservatively; an unmatched non-empty path is unknown."""

    found, _ = _classify_paths(changed_files, manifest.change_classes)
    return tuple(change_class for change_class in SUPPORTED_CHANGE_CLASSES if change_class in found)


def _classify_with_unknowns(
    changed_files: Sequence[str],
    manifest: ApplicabilityManifest,
) -> tuple[tuple[ChangeClass, ...], bool]:
    found, unknown = _classify_paths(changed_files, manifest.change_classes)
    return (
        tuple(change_class for change_class in SUPPORTED_CHANGE_CLASSES if change_class in found),
        unknown,
    )


def _classify_paths(
    changed_files: Sequence[str],
    rules: Sequence[ChangeClassRule],
) -> tuple[set[ChangeClass], bool]:
    found: set[ChangeClass] = set()
    unknown = False
    for path in changed_files:
        normalized = path.replace("\\", "/")
        rule = _most_specific_rule(rules, normalized)
        if rule is not None:
            found.add(rule.change_class)
        elif normalized in {"pyproject.toml", "uv.lock"}:
            found.add("gate_report")
        else:
            unknown = True
    return found, unknown


def _most_specific_rule(
    rules: Sequence[ChangeClassRule],
    normalized_path: str,
) -> ChangeClassRule | None:
    matches = [
        rule
        for rule in rules
        if any(
            normalized_path == prefix or normalized_path.startswith(prefix)
            for prefix in rule.path_prefixes
        )
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda rule: max(
            len(prefix)
            for prefix in rule.path_prefixes
            if normalized_path == prefix or normalized_path.startswith(prefix)
        ),
    )


def validate_applicability_manifest(
    manifest: ApplicabilityManifest,
) -> tuple[ApplicabilityFailure, ...]:
    """Return safe semantic validation failures; any failure is fail-closed."""

    failures: list[ApplicabilityFailure] = []
    # ``model_copy`` on the shared immutable base intentionally preserves the
    # caller's unvalidated payload.  Inspect the raw mapping here so a stale
    # manifest is reported safely instead of asking Pydantic to serialize it.
    raw = dict(manifest.__dict__)

    if raw.get("schema_version") != R1_GATE_APPLICABILITY_VERSION:
        failures.append(ApplicabilityFailure(code="stale_manifest", message="manifest policy version is stale"))
    if raw.get("manifest_id") != DEFAULT_R1_MANIFEST_ID:
        failures.append(ApplicabilityFailure(code="stale_manifest", message="manifest identity is not the active policy"))

    component_versions = raw.get("component_versions", ())
    component_keys = {
        _as_mapping(item).get("key") for item in component_versions
    }
    missing_components = _REQUIRED_COMPONENTS - component_keys
    if missing_components:
        failures.append(
            ApplicabilityFailure(
                code="incomplete_manifest",
                message="manifest is missing required component version pins",
            )
        )
    if len(component_keys) != len(component_versions):
        failures.append(
            ApplicabilityFailure(code="inconsistent_manifest", message="component version pins are not unique")
        )

    evaluator_versions = raw.get("evaluator_versions", ())
    evaluator_catalog = {
        _as_mapping(item).get("evaluator_key"): _as_mapping(item).get("evaluator_version")
        for item in evaluator_versions
    }
    if evaluator_catalog != _EXPECTED_EVALUATORS:
        failures.append(
            ApplicabilityFailure(
                code="evaluator_catalog_mismatch",
                message="evaluator catalog/version pins do not match the offline contract",
            )
        )

    dataset_versions = raw.get("dataset_versions", ())
    if tuple(_as_mapping(item) for item in dataset_versions) != (
        _EXPECTED_DATASET.model_dump(mode="python"),
    ):
        failures.append(
            ApplicabilityFailure(
                code="dataset_catalog_mismatch",
                message="dataset identity/version pins do not match the offline contract",
            )
        )

    suites = raw.get("suites", ())
    suite_ids = {
        _as_mapping(item).get("suite_id") for item in suites
    }
    if len(suite_ids) != len(suites):
        failures.append(ApplicabilityFailure(code="inconsistent_manifest", message="suite identifiers are not unique"))
    mandatory_suite = raw.get("mandatory_suite_id")
    mandatory = next((_as_mapping(item) for item in suites if _as_mapping(item).get("suite_id") == mandatory_suite), None)
    if (
        mandatory_suite != MANDATORY_OFFLINE_SUITE_ID
        or mandatory is None
        or mandatory.get("suite_version") != MANDATORY_OFFLINE_SUITE_VERSION
        or not mandatory.get("mandatory")
        or MANDATORY_OFFLINE_EVIDENCE_ID not in mandatory.get("evidence_ids", ())
    ):
        failures.append(
            ApplicabilityFailure(
                code="inconsistent_manifest",
                message="mandatory offline safety suite is missing or weakened",
                suite_id=str(mandatory_suite),
            )
        )
    rules = raw.get("change_classes", ())
    rule_classes = {_as_mapping(item).get("change_class") for item in rules}
    if rule_classes != set(SUPPORTED_CHANGE_CLASSES):
        failures.append(
            ApplicabilityFailure(
                code="incomplete_manifest",
                message="manifest does not cover every supported change class",
            )
        )
    if len(rule_classes) != len(rules):
        failures.append(ApplicabilityFailure(code="inconsistent_manifest", message="change classes are not unique"))
    for item in rules:
        rule = _as_mapping(item)
        if rule.get("component_key") not in component_keys:
            failures.append(
                ApplicabilityFailure(
                    code="inconsistent_manifest",
                    message="change class references an unpinned component",
                    change_class=rule.get("change_class"),
                )
            )
        if any(suite_id not in suite_ids for suite_id in rule.get("suite_ids", ())):
            failures.append(
                ApplicabilityFailure(
                    code="inconsistent_manifest",
                    message="change class references an unknown suite",
                    change_class=rule.get("change_class"),
                )
            )
    return tuple(failures)


def _ordered_suite_ids(
    manifest: ApplicabilityManifest,
    changed_classes: Iterable[ChangeClass],
) -> tuple[str, ...]:
    selected = {MANDATORY_OFFLINE_SUITE_ID, manifest.mandatory_suite_id}
    rules = {
        item.get("change_class"): item
        for item in (_as_mapping(rule) for rule in manifest.change_classes)
    }
    for change_class in changed_classes:
        rule = rules.get(change_class)
        if rule is not None:
            selected.update(rule.get("suite_ids", ()))
    return tuple(
        str(suite.get("suite_id"))
        for suite in (_as_mapping(item) for item in manifest.suites)
        if suite.get("suite_id") in selected
    )


def _as_mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    dumped = model_dump(mode="python") if callable(model_dump) else {}
    return dict(dumped) if isinstance(dumped, Mapping) else {}


def build_r1_applicability_report(
    *,
    changed_files: Sequence[str],
    execution_evidence: Sequence[ExecutionEvidence],
    manifest: ApplicabilityManifest = DEFAULT_R1_APPLICABILITY_MANIFEST,
    mandatory_offline_executed: bool = False,
) -> ApplicabilityReport:
    """Validate applicability without executing a suite."""

    manifest_failures = list(validate_applicability_manifest(manifest))
    changed_classes, has_unknown = _classify_with_unknowns(changed_files, manifest)
    failures = list(manifest_failures)
    if has_unknown:
        failures.append(
            ApplicabilityFailure(
                code="unknown_change_class",
                message="a changed path has no versioned applicability rule",
            )
        )

    selected_suite_ids = _ordered_suite_ids(manifest, changed_classes)
    suites_by_id = {
        str(suite.get("suite_id")): suite
        for suite in (_as_mapping(item) for item in manifest.suites)
    }
    required_evidence_ids = tuple(
        evidence_id
        for suite_id in selected_suite_ids
        for evidence_id in suites_by_id.get(suite_id, {}).get("evidence_ids", ())
    )
    selected_suites: list[SuiteRequirement] = []
    for suite_id in selected_suite_ids:
        suite_data = suites_by_id.get(suite_id)
        if suite_data is None:
            continue
        try:
            selected_suites.append(SuiteRequirement.model_validate(suite_data))
        except ValidationError:
            failures.append(
                ApplicabilityFailure(
                    code="inconsistent_manifest",
                    message="selected suite definition is invalid",
                    suite_id=suite_id,
                )
            )
    evidence_by_id: dict[str, ExecutionEvidence] = {}
    for evidence in execution_evidence:
        if evidence.evidence_id in evidence_by_id:
            failures.append(
                ApplicabilityFailure(
                    code="inconsistent_manifest",
                    message="execution evidence identifiers are not unique",
                    evidence_id=evidence.evidence_id,
                )
            )
        evidence_by_id.setdefault(evidence.evidence_id, evidence)
        if evidence.evidence_id not in required_evidence_ids:
            failures.append(
                ApplicabilityFailure(
                    code="unexpected_execution_evidence",
                    message="execution evidence is not required by the selected suites",
                    evidence_id=evidence.evidence_id,
                )
            )

    for suite_id in selected_suite_ids:
        suite = suites_by_id.get(suite_id)
        if suite is None:
            failures.append(
                ApplicabilityFailure(code="inconsistent_manifest", message="selected suite is not defined", suite_id=suite_id)
            )
            continue
        for evidence_id in suite.get("evidence_ids", ()):
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                failures.append(
                    ApplicabilityFailure(
                        code="missing_execution_evidence",
                        message="required suite execution evidence is missing",
                        suite_id=suite_id,
                        evidence_id=evidence_id,
                    )
                )
                continue
            if evidence.manifest_id != manifest.manifest_id or evidence.manifest_version != manifest.schema_version:
                failures.append(
                    ApplicabilityFailure(
                        code="stale_execution_evidence",
                        message="execution evidence is bound to a different manifest version",
                        suite_id=suite_id,
                        evidence_id=evidence_id,
                    )
                )
            if evidence.suite_id != suite.get("suite_id") or evidence.suite_version != suite.get("suite_version"):
                failures.append(
                    ApplicabilityFailure(
                        code="evidence_suite_mismatch",
                        message="execution evidence does not match the required suite version",
                        suite_id=suite_id,
                        evidence_id=evidence_id,
                    )
                )
            if evidence.status != "passed":
                code: FailureCode = (
                    "mandatory_offline_unavailable"
                    if suite_id == manifest.mandatory_suite_id and evidence.status == "unavailable"
                    else "evidence_failed"
                )
                failures.append(
                    ApplicabilityFailure(
                        code=code,
                        message="required suite execution evidence did not pass",
                        suite_id=suite_id,
                        evidence_id=evidence_id,
                    )
                )
            if evidence.component_versions != manifest.component_versions:
                failures.append(
                    ApplicabilityFailure(
                        code="stale_execution_evidence",
                        message="execution evidence has different component version pins",
                        suite_id=suite_id,
                        evidence_id=evidence_id,
                    )
                )
            if evidence.evaluator_versions != manifest.evaluator_versions:
                failures.append(
                    ApplicabilityFailure(
                        code="stale_execution_evidence",
                        message="execution evidence has different evaluator version pins",
                        suite_id=suite_id,
                        evidence_id=evidence_id,
                    )
                )
            if evidence.dataset_versions != manifest.dataset_versions:
                failures.append(
                    ApplicabilityFailure(
                        code="stale_execution_evidence",
                        message="execution evidence has different dataset version pins",
                        suite_id=suite_id,
                        evidence_id=evidence_id,
                    )
                )

    mandatory_executed = mandatory_offline_executed or MANDATORY_OFFLINE_EVIDENCE_ID in evidence_by_id
    if manifest.mandatory_suite_id == MANDATORY_OFFLINE_SUITE_ID and not mandatory_executed:
        failures.append(
            ApplicabilityFailure(
                code="missing_execution_evidence",
                message="mandatory offline safety suite execution was not recorded",
                suite_id=MANDATORY_OFFLINE_SUITE_ID,
                evidence_id=MANDATORY_OFFLINE_EVIDENCE_ID,
            )
        )

    provider_quality_status: ProviderQualityStatus = (
        "not-confirmed"
        if {"prompt", "model", "retrieval"} & {
            _as_mapping(rule).get("component_key")
            for rule in manifest.change_classes
            if _as_mapping(rule).get("change_class") in changed_classes
        }
        else "not-applicable"
    )
    return ApplicabilityReport(
        manifest_id=manifest.manifest_id,
        changed_classes=changed_classes,
        selected_suites=tuple(selected_suites),
        selected_suite_ids=selected_suite_ids,
        required_evidence_ids=required_evidence_ids,
        execution_evidence_ids=tuple(evidence.evidence_id for evidence in execution_evidence),
        component_versions=manifest.component_versions,
        evaluator_versions=manifest.evaluator_versions,
        dataset_versions=manifest.dataset_versions,
        mandatory_offline_executed=mandatory_executed,
        provider_quality_status=provider_quality_status,
        passed=not failures,
        failures=tuple(failures),
    )


def run_r1_applicability_gate(
    *,
    changed_files: Sequence[str],
    run_offline_gate: Callable[[], object] | None = None,
    execution_evidence: Sequence[ExecutionEvidence] = (),
    manifest: ApplicabilityManifest = DEFAULT_R1_APPLICABILITY_MANIFEST,
) -> ApplicabilityReport:
    """Run the mandatory offline gate first, for every changed-file set."""

    if run_offline_gate is None:
        from spine.evals.regression_gate import run_offline_regression_gate

        run_offline_gate = run_offline_regression_gate
    try:
        result = run_offline_gate()
        passed = bool(getattr(result, "passed"))
        status: EvidenceStatus = "passed" if passed else "failed"
    except Exception:  # pragma: no cover - defensive gate boundary
        status = "unavailable"
    mandatory_evidence = ExecutionEvidence(
        evidence_id=MANDATORY_OFFLINE_EVIDENCE_ID,
        suite_id=MANDATORY_OFFLINE_SUITE_ID,
        suite_version=MANDATORY_OFFLINE_SUITE_VERSION,
        manifest_id=manifest.manifest_id,
        manifest_version=manifest.schema_version,
        status=status,
        component_versions=manifest.component_versions,
        evaluator_versions=manifest.evaluator_versions,
        dataset_versions=manifest.dataset_versions,
    )
    return build_r1_applicability_report(
        changed_files=changed_files,
        execution_evidence=(mandatory_evidence, *execution_evidence),
        manifest=manifest,
        mandatory_offline_executed=True,
    )


__all__ = [
    "ApplicabilityFailure",
    "ApplicabilityManifest",
    "ApplicabilityReport",
    "ChangeClassRule",
    "DEFAULT_R1_MANIFEST_ID",
    "DEFAULT_R1_APPLICABILITY_MANIFEST",
    "ExecutionEvidence",
    "MANDATORY_OFFLINE_EVIDENCE_ID",
    "MANDATORY_OFFLINE_SUITE_ID",
    "R1_GATE_APPLICABILITY_REPORT_VERSION",
    "R1_GATE_APPLICABILITY_VERSION",
    "SUPPORTED_CHANGE_CLASSES",
    "SuiteRequirement",
    "VersionPin",
    "build_default_r1_applicability_manifest",
    "build_r1_applicability_report",
    "classify_changed_files",
    "run_r1_applicability_gate",
    "validate_applicability_manifest",
]
