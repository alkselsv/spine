from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.application.recovered_application_qualification import (
    ApplicationCanaryObservation,
    ProjectionRecoveryObservation,
    RecoveredApplicationQualificationRequest,
    RecoveredApplicationQualificationService,
)


NOW = datetime(2026, 10, 27, 18, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("50900000-0000-4000-8000-00000000000a")
ENVIRONMENT_ID = UUID("50900000-0000-4000-8000-00000000000e")
SOURCE_REVISION_ID = UUID("a2b9e71c-6991-5c9d-8345-2d058020aea9")


def request(**overrides: object) -> RecoveredApplicationQualificationRequest:
    values: dict[str, object] = {
        "run_reference": "run:issue509-application-canary-001",
        "authorization_reference": "approval:issue509-application-canary-001",
        "target_commit": "8" * 40,
        "target_reference": "target:issue509-application-001",
        "recovery_set_id": "recovery:issue509-005",
        "restore_evidence_reference": "restore:" + "b" * 64,
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "source_revision_id": SOURCE_REVISION_ID,
        "projection_kind": "qa",
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(overrides)
    return RecoveredApplicationQualificationRequest.model_validate(values)


class Evidence:
    def __init__(self, verified: bool = True) -> None:
        self.verified = verified
        self.calls = 0

    async def verify(self, qualification: RecoveredApplicationQualificationRequest) -> bool:
        self.calls += 1
        return self.verified


class Target:
    def __init__(self, events: list[str], *, seal_fails: bool = False) -> None:
        self.events = events
        self.seal_fails = seal_fails

    async def prepare(self, qualification: RecoveredApplicationQualificationRequest) -> None:
        self.events.append("prepare")

    async def seal(self, qualification: RecoveredApplicationQualificationRequest) -> None:
        self.events.append("seal")
        if self.seal_fails:
            raise RuntimeError("target details must not cross the seam")


class Projection:
    def __init__(self, events: list[str], *, verified: bool = True) -> None:
        self.events = events
        self.verified = verified

    async def recover_and_activate(
        self, qualification: RecoveredApplicationQualificationRequest
    ) -> ProjectionRecoveryObservation:
        self.events.append("recover_and_activate")
        return ProjectionRecoveryObservation(
            verified=self.verified,
            projection_snapshot_reference="snapshot:issue509-application-001",
            activation_generation=1 if self.verified else None,
            evidence_reference="evidence:projection-application-001",
        )


class Canary:
    def __init__(self, events: list[str], *, verified: bool = True) -> None:
        self.events = events
        self.verified = verified

    async def read_permitted_source(
        self,
        qualification: RecoveredApplicationQualificationRequest,
        projection: ProjectionRecoveryObservation,
    ) -> ApplicationCanaryObservation:
        self.events.append("read_permitted_source")
        return ApplicationCanaryObservation(
            verified=self.verified,
            source_revision_id=(qualification.source_revision_id if self.verified else None),
            evidence_count=1 if self.verified else 0,
            diagnostic_reference="diagnostic:issue509-application-001",
        )


def service(
    *,
    evidence: Evidence | None = None,
    target: Target | None = None,
    projection: Projection | None = None,
    canary: Canary | None = None,
    events: list[str] | None = None,
) -> RecoveredApplicationQualificationService:
    recorded = events if events is not None else []
    return RecoveredApplicationQualificationService(
        restore_evidence=evidence or Evidence(),
        target=target or Target(recorded),
        projection=projection or Projection(recorded),
        canary=canary or Canary(recorded),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_unverified_restore_evidence_blocks_before_target_mutation() -> None:
    events: list[str] = []

    result = await service(evidence=Evidence(False), events=events).qualify(request())

    assert result.status == "blocked"
    assert result.safe_failure_code == "restore_evidence_unverified"
    assert result.target_sealed is True
    assert events == []


@pytest.mark.asyncio
async def test_verified_restore_runs_application_canary_then_reseals_target() -> None:
    events: list[str] = []

    result = await service(events=events).qualify(request())

    assert result.status == "verified"
    assert result.safe_failure_code is None
    assert result.projection_snapshot_reference == "snapshot:issue509-application-001"
    assert result.activation_generation == 1
    assert result.canary_evidence_count == 1
    assert result.target_sealed is True
    assert events == [
        "prepare",
        "recover_and_activate",
        "read_permitted_source",
        "seal",
    ]


@pytest.mark.asyncio
async def test_failed_application_read_is_not_verified_and_target_is_resealed() -> None:
    events: list[str] = []
    canary = Canary(events, verified=False)

    result = await service(canary=canary, events=events).qualify(request())

    assert result.status == "not_verified"
    assert result.safe_failure_code == "application_canary_failed"
    assert result.canary_evidence_count == 0
    assert result.target_sealed is True
    assert events[-1] == "seal"


@pytest.mark.asyncio
async def test_expired_authorization_blocks_before_evidence_or_target_access() -> None:
    evidence = Evidence()
    events: list[str] = []

    result = await service(evidence=evidence, events=events).qualify(
        request(expires_at=NOW)
    )

    assert result.status == "blocked"
    assert result.safe_failure_code == "authorization_expired"
    assert evidence.calls == 0
    assert events == []


@pytest.mark.asyncio
async def test_seal_failure_is_indeterminate_and_never_claims_qualification() -> None:
    events: list[str] = []
    target = Target(events, seal_fails=True)

    result = await service(target=target, events=events).qualify(request())

    assert result.status == "indeterminate"
    assert result.safe_failure_code == "target_seal_failed"
    assert result.target_sealed is False
    assert "target details must not cross the seam" not in result.model_dump_json()
