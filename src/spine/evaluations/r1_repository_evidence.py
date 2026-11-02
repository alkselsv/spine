"""Collect canonical, metadata-only evidence for the repository R1 gate."""

from __future__ import annotations

import hashlib
from pathlib import Path
from xml.etree import ElementTree

from spine.domain.evaluations.r1_repository_qualification import (
    MigrationTargetReceipt,
    R1RepositoryEvidence,
    R1SkipPolicy,
    RepositoryTestReceipt,
    SkipDisposition,
)


def collect_r1_repository_evidence(
    *,
    head_sha: str,
    clean_target_path: Path,
    upgrade_target_path: Path,
    junit_path: Path,
    skip_policy_path: Path,
) -> R1RepositoryEvidence:
    """Build canonical evidence from two target receipts and one pytest report."""

    clean = MigrationTargetReceipt.model_validate_json(clean_target_path.read_bytes())
    upgrade = MigrationTargetReceipt.model_validate_json(upgrade_target_path.read_bytes())
    policy_bytes = skip_policy_path.read_bytes()
    policy = R1SkipPolicy.model_validate_json(policy_bytes)
    policy_by_test_id = {entry.test_id: entry for entry in policy.entries}

    root = ElementTree.fromstring(junit_path.read_bytes())
    counts = _junit_counts(root)
    dispositions: list[SkipDisposition] = []
    for testcase in root.iter("testcase"):
        skipped = testcase.find("skipped")
        if skipped is None:
            continue
        test_id = _testcase_nodeid(testcase)
        reported_reason = skipped.get("message", "").strip()
        policy_entry = policy_by_test_id.get(test_id)
        if (
            policy_entry is not None
            and reported_reason
            and policy_entry.expected_reason == reported_reason
        ):
            dispositions.append(
                SkipDisposition(
                    test_id=test_id,
                    reported_reason=reported_reason,
                    reason_code=policy_entry.reason_code,
                    applicability=policy_entry.applicability,
                    policy_version=policy.policy_version,
                )
            )
        else:
            dispositions.append(
                SkipDisposition(
                    test_id=test_id,
                    reported_reason=reported_reason or "missing-skip-reason",
                    reason_code="unclassified-skip",
                    applicability="unclassified",
                    policy_version=policy.policy_version,
                )
            )

    return R1RepositoryEvidence(
        head_sha=head_sha,
        skip_policy_version=policy.policy_version,
        skip_policy_digest=hashlib.sha256(policy_bytes).hexdigest(),
        migration_targets=(clean, upgrade),
        repository_tests=RepositoryTestReceipt(
            passed=(
                counts["tests"]
                - counts["failures"]
                - counts["errors"]
                - counts["skipped"]
            ),
            failed=counts["failures"],
            errors=counts["errors"],
            skipped=counts["skipped"],
            skip_dispositions=tuple(dispositions),
        ),
    )


def _junit_counts(root: ElementTree.Element) -> dict[str, int]:
    names = ("tests", "failures", "errors", "skipped")
    if all(root.get(name) is not None for name in names):
        return {name: int(root.get(name, "0")) for name in names}
    suites = tuple(root.findall("testsuite"))
    return {
        name: sum(int(suite.get(name, "0")) for suite in suites) for name in names
    }


def _testcase_nodeid(testcase: ElementTree.Element) -> str:
    properties = testcase.find("properties")
    if properties is not None:
        for item in properties.findall("property"):
            if item.get("name") == "spine_nodeid" and item.get("value"):
                return str(item.get("value"))
    return "missing-junit-nodeid"
