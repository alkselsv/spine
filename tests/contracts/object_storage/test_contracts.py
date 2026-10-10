from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from spine.infrastructure.object_storage.contracts import (
    AuthorizedOriginalReadGrant,
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
    with pytest.raises(ValidationError):
        AuthorizedOriginalReadGrant(
            grant_id=ZERO,
            workspace_id=uuid4(),
            environment_id=None,
            source_revision_id=uuid4(),
            object_reference=reference(),
            purpose="read.original.v1",
            authorization_decision_version="policy-v1",
            expires_at=datetime.now(timezone.utc),
        )
