from __future__ import annotations

import copy
import pickle
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from spine.infrastructure.object_storage.contracts import (
    AuthorizedOriginalReadGrant,
    AuthorizedReadGrantRegistry,
    ConsumedOriginalReadLease,
    DeletionApproval,
    IntegrityResult,
    IntegrityStatus,
    ObjectReference,
    ObservedContentIdentity,
    OriginalUploadCommandInput,
    OriginalUploadCommandPayload,
    OriginalUploadCommandResult,
    PreWriteRequestDigest,
    UploadCommandState,
    UploadCommandStateName,
    WriteReceipt,
)
from spine.domain.sources.original_reference import ObjectReference as DomainObjectReference, StorageModel as DomainStorageModel
from spine.infrastructure.object_storage.errors import InvalidReadGrant


ZERO = UUID(int=0)
DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def reference() -> ObjectReference:
    return ObjectReference(
        schema_version=1,
        object_id=uuid4(),
        storage_generation=uuid4(),
        digest_algorithm="sha256",
        digest_hex=DIGEST,
        byte_length=0,
    )


def test_original_reference_type_is_shared_with_source_domain() -> None:
    assert ObjectReference is DomainObjectReference
    assert issubclass(ObjectReference, DomainStorageModel)


def test_models_are_immutable_and_reject_unknown_fields() -> None:
    value = reference()
    with pytest.raises(ValidationError):
        value.object_id = uuid4()  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ObjectReference(**{**value.model_dump(), "provider": "s3"})


def test_digest_and_types_are_strictly_validated() -> None:
    with pytest.raises(ValidationError):
        ObjectReference(
            schema_version=1,
            object_id=uuid4(),
            storage_generation=uuid4(),
            digest_algorithm="sha256",
            digest_hex="A" * 64,
            byte_length=0,
        )
    with pytest.raises(ValidationError):
        PreWriteRequestDigest(
            algorithm="sha256",
            algorithm_version="spine.command-digest.v1",
            digest_hex="not-a-digest",
        )
    with pytest.raises(ValidationError):
        OriginalUploadCommandPayload(
            workspace_id=str(uuid4()),  # strict UUID is intentional
            purpose="document.upload.v1",
        )


def test_zero_length_objects_are_valid() -> None:
    value = reference()
    assert value.byte_length == 0
    observed = ObservedContentIdentity(observed_sha256=DIGEST, observed_byte_length=0)
    result = IntegrityResult(
        status=IntegrityStatus.VERIFIED,
        algorithm="sha256",
        observed_digest=DIGEST,
        observed_length=0,
    )
    receipt = WriteReceipt(
        schema_version=1,
        upload_id=uuid4(),
        object_reference=value,
        observed_content=observed,
        verification=result,
        storage_status="finalized_verified",
        finalized_at=datetime.now(timezone.utc),
    )
    assert receipt.observed_content.observed_byte_length == 0


def test_generation_identity_is_opaque_stable_and_not_authorization() -> None:
    value = reference()
    same = ObjectReference.model_validate(value.model_dump())
    assert same == value
    assert value.storage_generation != value.object_id
    assert "authorization" not in value.model_dump()


def test_upload_result_keeps_server_identities_out_of_request_payload() -> None:
    result = OriginalUploadCommandResult(
        request_digest=PreWriteRequestDigest(
            algorithm="sha256",
            algorithm_version="spine.command-digest.v1",
            digest_hex=DIGEST,
        ),
        upload_id=uuid4(),
        object_id=uuid4(),
        issue8_result_reference={"receipt_id": uuid4()},
    )
    payload = OriginalUploadCommandPayload(
        workspace_id=uuid4(), purpose="document.upload.v1"
    )
    assert result.object_id not in payload.model_dump().values()


def test_verified_receipt_contains_only_consistent_physical_evidence() -> None:
    with pytest.raises(ValidationError):
        IntegrityResult(
            status=IntegrityStatus.VERIFIED,
            algorithm="sha256",
            expected_digest=DIGEST,
            observed_digest=OTHER_DIGEST,
            observed_length=2,
        )
    observed = ObservedContentIdentity(observed_sha256=DIGEST, observed_byte_length=0)
    with pytest.raises(ValidationError):
        WriteReceipt(
            schema_version=1,
            upload_id=uuid4(),
            object_reference=reference(),
            observed_content=observed,
            verification=IntegrityResult(
                status=IntegrityStatus.VERIFIED,
                algorithm="sha256",
                observed_digest=OTHER_DIGEST,
                observed_length=0,
            ),
            storage_status="finalized_verified",
            finalized_at=datetime.now(timezone.utc),
        )

    for update in ({"digest_hex": OTHER_DIGEST}, {"byte_length": 1}):
        mismatched_reference = reference().model_copy(update=update)
        with pytest.raises(ValidationError):
            WriteReceipt(
                schema_version=1,
                upload_id=uuid4(),
                object_reference=mismatched_reference,
                observed_content=observed,
                verification=IntegrityResult(
                    status=IntegrityStatus.VERIFIED,
                    algorithm="sha256",
                    observed_digest=DIGEST,
                    observed_length=0,
                ),
                storage_status="finalized_verified",
                finalized_at=datetime.now(timezone.utc),
            )


def test_upload_state_allows_only_documented_transitions() -> None:
    state = UploadCommandState(
        issue8_receipt_id=uuid4(),
        request_digest=PreWriteRequestDigest(
            algorithm="sha256",
            algorithm_version="spine.command-digest.v1",
            digest_hex=DIGEST,
        ),
        upload_id=uuid4(),
        object_id=uuid4(),
        state=UploadCommandStateName.STAGING,
    )
    assert state.transitioned(UploadCommandStateName.INTERRUPTED).state is UploadCommandStateName.INTERRUPTED
    with pytest.raises(ValueError, match="finalized state requires"):
        state.transitioned(UploadCommandStateName.FINALIZED)


def test_grant_requires_explicit_scope_and_aware_expiry() -> None:
    with pytest.raises(TypeError):
        AuthorizedOriginalReadGrant()  # type: ignore[call-arg]

    workspace_id = uuid4()
    source_revision_id = uuid4()
    object_reference = reference()
    registry = AuthorizedReadGrantRegistry()
    grant = registry.issue(
        workspace_id=workspace_id,
        environment_id=None,
        source_revision_id=source_revision_id,
        object_reference=object_reference,
        purpose="read.original.v1",
        authorization_decision_version="policy-v1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert not hasattr(grant, "model_dump")
    with pytest.raises(TypeError):
        copy.copy(grant)
    with pytest.raises((TypeError, pickle.PicklingError)):
        pickle.dumps(grant)
    with pytest.raises(InvalidReadGrant):
        registry.consume(
            grant,
            workspace_id=workspace_id,
            environment_id=None,
            source_revision_id=source_revision_id,
            object_reference=object_reference.model_copy(update={"storage_generation": uuid4()}),
        )


def test_grant_is_one_shot_and_restart_invalidates_old_registry() -> None:
    workspace_id = uuid4()
    source_revision_id = uuid4()
    object_reference = reference()
    registry = AuthorizedReadGrantRegistry()
    grant = registry.issue(
        workspace_id=workspace_id,
        environment_id=None,
        source_revision_id=source_revision_id,
        object_reference=object_reference,
        purpose="read.original.v1",
        authorization_decision_version="policy-v1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    replacement_registry = AuthorizedReadGrantRegistry()
    with pytest.raises(InvalidReadGrant):
        replacement_registry.consume(
            grant,
            workspace_id=workspace_id,
            environment_id=None,
            source_revision_id=source_revision_id,
            object_reference=object_reference,
        )

    lease = registry.consume(
        grant,
        workspace_id=workspace_id,
        environment_id=None,
        source_revision_id=source_revision_id,
        object_reference=object_reference,
    )
    with pytest.raises(InvalidReadGrant):
        registry.consume(
            grant,
            workspace_id=workspace_id,
            environment_id=None,
            source_revision_id=source_revision_id,
            object_reference=object_reference,
        )
    assert registry.consume_read_lease(lease).object_reference == object_reference


def test_raw_read_requires_consumed_lease_and_consumes_it_once() -> None:
    workspace_id = uuid4()
    source_revision_id = uuid4()
    object_reference = reference()
    registry = AuthorizedReadGrantRegistry()
    grant = registry.issue(
        workspace_id=workspace_id,
        environment_id=None,
        source_revision_id=source_revision_id,
        object_reference=object_reference,
        purpose="read.original.v1",
        authorization_decision_version="policy-v1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    with pytest.raises(InvalidReadGrant):
        registry.consume_read_lease(grant)  # type: ignore[arg-type]

    lease = registry.consume(
        grant,
        workspace_id=workspace_id,
        environment_id=None,
        source_revision_id=source_revision_id,
        object_reference=object_reference,
    )
    assert isinstance(lease, ConsumedOriginalReadLease)
    with pytest.raises(TypeError):
        ConsumedOriginalReadLease()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        copy.copy(lease)
    with pytest.raises((TypeError, pickle.PicklingError)):
        pickle.dumps(lease)

    class RawReadFake:
        def open_bounded_read(
            self, consumed_lease: ConsumedOriginalReadLease, *, max_bytes: int
        ) -> list[bytes]:
            assert max_bytes > 0
            registry.consume_read_lease(consumed_lease)
            return [b""]

    assert RawReadFake().open_bounded_read(lease, max_bytes=1) == [b""]
    with pytest.raises(InvalidReadGrant):
        registry.consume_read_lease(lease)


def test_consumed_read_lease_rejects_wrong_registry_and_expiry() -> None:
    workspace_id = uuid4()
    source_revision_id = uuid4()
    object_reference = reference()
    registry = AuthorizedReadGrantRegistry()
    grant = registry.issue(
        workspace_id=workspace_id,
        environment_id=None,
        source_revision_id=source_revision_id,
        object_reference=object_reference,
        purpose="read.original.v1",
        authorization_decision_version="policy-v1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    lease = registry.consume(
        grant,
        workspace_id=workspace_id,
        environment_id=None,
        source_revision_id=source_revision_id,
        object_reference=object_reference,
    )
    with pytest.raises(InvalidReadGrant):
        AuthorizedReadGrantRegistry().consume_read_lease(lease)

    expired_registry = AuthorizedReadGrantRegistry()
    expired_grant = expired_registry.issue(
        workspace_id=workspace_id,
        environment_id=None,
        source_revision_id=source_revision_id,
        object_reference=object_reference,
        purpose="read.original.v1",
        authorization_decision_version="policy-v1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    expired_lease = expired_registry.consume(
        expired_grant,
        workspace_id=workspace_id,
        environment_id=None,
        source_revision_id=source_revision_id,
        object_reference=object_reference,
    )
    with pytest.raises(InvalidReadGrant):
        expired_registry.consume_read_lease(
            expired_lease, now=datetime.now(timezone.utc) + timedelta(minutes=6)
        )
    with pytest.raises(InvalidReadGrant):
        expired_registry.consume_read_lease(expired_lease)


def test_grant_rejects_expiry_and_scope_or_generation_substitution() -> None:
    workspace_id = uuid4()
    source_revision_id = uuid4()
    object_reference = reference()
    registry = AuthorizedReadGrantRegistry()
    expired = registry.issue(
        workspace_id=workspace_id,
        environment_id=None,
        source_revision_id=source_revision_id,
        object_reference=object_reference,
        purpose="read.original.v1",
        authorization_decision_version="policy-v1",
        expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(InvalidReadGrant):
        registry.consume(
            expired,
            workspace_id=workspace_id,
            environment_id=None,
            source_revision_id=source_revision_id,
            object_reference=object_reference,
        )

    substituted = registry.issue(
        workspace_id=workspace_id,
        environment_id=None,
        source_revision_id=source_revision_id,
        object_reference=object_reference,
        purpose="read.original.v1",
        authorization_decision_version="policy-v1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    with pytest.raises(InvalidReadGrant):
        registry.consume(
            substituted,
            workspace_id=uuid4(),
            environment_id=None,
            source_revision_id=source_revision_id,
            object_reference=object_reference.model_copy(
                update={"storage_generation": uuid4()}
            ),
        )


def test_digest_rejects_unknown_issue8_algorithm_version() -> None:
    with pytest.raises(ValidationError):
        PreWriteRequestDigest(
            algorithm="sha256",
            algorithm_version="spine.command-digest.v999",
            digest_hex=DIGEST,
        )


def test_deletion_approval_accepts_only_typed_safe_identifier() -> None:
    approval = DeletionApproval(command_id=uuid4(), approval_reference=uuid4())
    assert isinstance(approval.approval_reference, UUID)
    for unsafe in (
        "https://storage.example/object?token=secret",
        "/var/lib/spine/object",
        "Bearer-secret-token",
    ):
        with pytest.raises(ValidationError):
            DeletionApproval(command_id=uuid4(), approval_reference=unsafe)
