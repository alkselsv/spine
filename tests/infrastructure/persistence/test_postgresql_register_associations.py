from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin
from spine.domain.sources import (
    GoogleDriveFileKey,
    GoogleRegisterRowKey,
    RegisterAssociationAcceptance,
    RegisterLinkRole,
    RegisterObservedLinkClaim,
    google_source_identity,
)
from spine.infrastructure.persistence.postgresql_register_associations import (
    PostgreSQLRegisterAssociationRepository,
)

WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")
ROW_OBJECT = UUID("30000000-0000-4000-8000-000000000001")
ROW_REVISION = UUID("40000000-0000-4000-8000-000000000001")
PROVENANCE = UUID("50000000-0000-4000-8000-000000000001")
CLAIM_ID = UUID("60000000-0000-4000-8000-000000000001")
TARGET_OBJECT = UUID("70000000-0000-4000-8000-000000000001")
TARGET_REVISION = UUID("80000000-0000-4000-8000-000000000001")
ACTOR = UUID("90000000-0000-4000-8000-000000000001")
CONNECTION = UUID("e0000000-0000-4000-8000-000000000001")
TARGET_CONNECTION = UUID("b0000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


def _claim() -> RegisterObservedLinkClaim:
    return RegisterObservedLinkClaim(
        claim_id=CLAIM_ID,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        row_source_object_id=ROW_OBJECT,
        row_source_revision_id=ROW_REVISION,
        row_provider_identity=GoogleRegisterRowKey(
            connection_id=CONNECTION,
            spreadsheet_id="spreadsheet-456",
            sheet_id=17,
            immutable_business_id="ROW-001",
        ),
        row_revision_digest="a" * 64,
        row_original_sha256="c" * 64,
        role=RegisterLinkRole.DRIVE_MATERIAL,
        claimed_external_identifier="drive-file-123",
        external_identifier_type="google_drive_file_id",
        extraction_profile="register-links:v1",
        evidence_locator="column:final_document_id",
        evidence_digest="b" * 64,
        provenance_id=PROVENANCE,
        observed_at=NOW,
    )


def _acceptance(*, actor_id: UUID = ACTOR) -> RegisterAssociationAcceptance:
    return RegisterAssociationAcceptance(
        acceptance_id=UUID("a0000000-0000-4000-8000-000000000001"),
        claim_id=CLAIM_ID,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        row_source_object_id=ROW_OBJECT,
        row_source_revision_id=ROW_REVISION,
        target_source_object_id=TARGET_OBJECT,
        target_source_revision_id=TARGET_REVISION,
        target_provider_identity=GoogleDriveFileKey(
            connection_id=TARGET_CONNECTION,
            file_id="drive-file-123",
        ),
        role=RegisterLinkRole.DRIVE_MATERIAL,
        accepted_by=actor_id,
        accepted_at=NOW,
    )


def _context(
    *, actor_id: UUID = ACTOR, workspace_id: UUID = WORKSPACE
) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id=workspace_id, environment_id=ENVIRONMENT),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("register_association"),
        operation=PersistenceOperation("register_association_accept"),
        trace_id=UUID("f0000000-0000-4000-8000-000000000001"),
        acting_subject_id=actor_id,
        service_principal_id=None,
        provenance=TrustedContextProvenance(
            issuer_id=UUID("f1000000-0000-4000-8000-000000000001"),
            signature=b"trusted-test-context",
        ),
    )


class _Result:
    def __init__(self, row=None) -> None:
        self._row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self._row

    def first(self):
        return None if self._row is None else (self._row,)


class _UnitOfWork:
    def __init__(
        self,
        context: TrustedPersistenceContext,
        *,
        target_accepted: bool = True,
        target_identity_matches: bool = True,
        row_accepted: bool = True,
        row_revision_digest_matches: bool = True,
        row_original_digest_matches: bool = True,
    ) -> None:
        self._active_context = context
        self.target_accepted = target_accepted
        self.target_identity_matches = target_identity_matches
        self.row_accepted = row_accepted
        self.row_revision_digest_matches = row_revision_digest_matches
        self.row_original_digest_matches = row_original_digest_matches
        self.claim_row = None
        self.acceptance_row = None
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def _execute(self, statement):
        table = (
            statement.table if statement.is_insert else statement.get_final_froms()[0]
        )
        table_name = table.name
        if statement.is_insert:
            values = {
                column: bound.value for column, bound in statement._values.items()
            }
            if table_name == "register_link_claims":
                self.claim_row = values
            elif table_name == "register_association_acceptances":
                self.acceptance_row = values
            return _Result()
        params = statement.compile().params
        source_id = next(
            (
                value
                for key, value in params.items()
                if key.startswith("source_object_id")
            ),
            None,
        )
        revision_id = next(
            (
                value
                for key, value in params.items()
                if key.startswith("source_revision_id")
            ),
            None,
        )
        if table_name == "register_link_claims":
            return _Result(self.claim_row)
        if table_name == "register_association_acceptances":
            return _Result(self.acceptance_row)
        if table_name == "canonical_acceptances":
            accepted = (
                self.row_accepted if source_id == ROW_OBJECT else self.target_accepted
            )
            return _Result(UUID(int=1) if accepted else None)
        if table_name == "source_revision_provenance":
            return _Result(UUID(int=1))
        if table_name == "source_revisions":
            return _Result(
                {
                    "revision_digest": (
                        "a" * 64 if self.row_revision_digest_matches else "d" * 64
                    ),
                    "original_sha256": (
                        "c" * 64 if self.row_original_digest_matches else "e" * 64
                    ),
                }
            )
        if table_name == "source_objects":
            matches = (
                self.target_identity_matches if source_id == TARGET_OBJECT else True
            )
            if not matches:
                return _Result()
            return _Result(UUID(int=1))
        raise AssertionError(f"unexpected persistence table: {table_name}")


def _repository(uow: _UnitOfWork) -> PostgreSQLRegisterAssociationRepository:
    return PostgreSQLRegisterAssociationRepository(
        uow_factory=lambda context: uow,
        context_factory=lambda operation, identity: _context(),
    )


@pytest.mark.asyncio
async def test_postgresql_repository_persists_claim_and_acceptance_with_exact_replay() -> (
    None
):
    uow = _UnitOfWork(_context())
    repository = _repository(uow)
    claim = _claim()

    persisted_claim = await repository.record_claim(claim)
    accepted = await repository.accept_claim(
        claim_id=claim.claim_id, acceptance=_acceptance()
    )
    replay = await repository.accept_claim(
        claim_id=claim.claim_id, acceptance=_acceptance()
    )

    assert persisted_claim == claim
    assert accepted == replay
    assert uow.claim_row["row_source_revision_id"] == ROW_REVISION
    assert uow.acceptance_row["target_source_revision_id"] == TARGET_REVISION
    assert uow.commits == 2


@pytest.mark.asyncio
async def test_postgresql_repository_rejects_unaccepted_or_cross_scope_revisions() -> (
    None
):
    row_uow = _UnitOfWork(_context(), row_accepted=False)
    with pytest.raises(ValueError, match="row revision is not canonically accepted"):
        await _repository(row_uow).record_claim(_claim())

    uow = _UnitOfWork(_context())
    repository = _repository(uow)
    await repository.record_claim(_claim())
    foreign = _acceptance().model_copy(
        update={"workspace_id": UUID("f3000000-0000-4000-8000-000000000001")}
    )
    with pytest.raises(PermissionError, match="outside the trusted scope"):
        await repository.accept_claim(claim_id=CLAIM_ID, acceptance=foreign)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revision_matches", "original_matches"),
    ((False, True), (True, False)),
)
async def test_postgresql_repository_rejects_claim_content_bound_to_another_revision(
    revision_matches: bool,
    original_matches: bool,
) -> None:
    uow = _UnitOfWork(
        _context(),
        row_revision_digest_matches=revision_matches,
        row_original_digest_matches=original_matches,
    )

    with pytest.raises(ValueError, match="does not match its accepted source revision"):
        await _repository(uow).record_claim(_claim())


@pytest.mark.asyncio
async def test_postgresql_repository_rejects_wrong_target_revision_or_provider_identity() -> (
    None
):
    uow = _UnitOfWork(_context(), target_accepted=False)
    repository = _repository(uow)
    await repository.record_claim(_claim())
    with pytest.raises(ValueError, match="target revision is not canonically accepted"):
        await repository.accept_claim(claim_id=CLAIM_ID, acceptance=_acceptance())

    uow = _UnitOfWork(_context(), target_identity_matches=False)
    repository = _repository(uow)
    await repository.record_claim(_claim())
    with pytest.raises(
        ValueError, match="does not match the validated provider identity"
    ):
        await repository.accept_claim(claim_id=CLAIM_ID, acceptance=_acceptance())


@pytest.mark.asyncio
async def test_postgresql_repository_rejects_actor_mismatch() -> None:
    uow = _UnitOfWork(_context())
    repository = _repository(uow)
    await repository.record_claim(_claim())

    with pytest.raises(PermissionError, match="actor does not match"):
        await repository.accept_claim(
            claim_id=CLAIM_ID,
            acceptance=_acceptance(
                actor_id=UUID("f4000000-0000-4000-8000-000000000001")
            ),
        )


def test_provider_identity_fixture_maps_to_opaque_canonical_identity() -> None:
    key = GoogleDriveFileKey(connection_id=TARGET_CONNECTION, file_id="drive-file-123")

    identity = google_source_identity(key)

    assert identity.external_namespace == "google"
    assert identity.external_object_id.startswith("gsk1:")
