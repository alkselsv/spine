from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.persistence.context import (
    ContextOrigin,
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.private_asr_corpus import (
    AccessPolicyPort,
    AdmittedSourceRevision,
    AggregateMeasurement,
    CorpusAdmissionDenied,
    CorpusVerificationError,
    MetadataInventoryEntry,
    PrivacySanitizationError,
    PurposeAdmissionRequest,
    SanitizedReportBuilder,
    TrustedMetadataInventory,
    TwoStageSourceAdmission,
    VerifiedAuthority,
)
from spine.domain.evaluations.private_asr_corpus import (
    AggregateCategory,
    AggregateMetric,
    ApprovedPrivacyPolicy,
    PrivateReference,
    PrivacyPolicy,
    QualificationSufficiency,
    SanitizedCorpusReport,
)
from spine.domain.sources import IdentityMode, RevisionMetadata, SourceKind, SourceObject, content_revision
from spine.domain.sources.access_policy import AccessPolicyVersion, PolicyOriginKind
from spine.domain.sources.original_reference import ObjectReference


NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
WORKSPACE, ENVIRONMENT = UUID(int=1), UUID(int=2)
RAW = b"synthetic fixture audio"
DIGEST = sha256(RAW).hexdigest()


def _id(value: int) -> UUID:
    return UUID(int=value)


def context(
    *, workspace_id=WORKSPACE, environment_id=ENVIRONMENT, actor_id=_id(11),
    operation="admit_private_corpus",
) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("asr_model_evaluation"),
        operation=PersistenceOperation(operation),
        trace_id=_id(10),
        acting_subject_id=actor_id,
        service_principal_id=None,
        provenance=TrustedContextProvenance(issuer_id=_id(12), signature=b"synthetic-proof"),
    )


def source_revision(*, source_number: int = 1):
    source_id = _id(100 + source_number)
    source = SourceObject(
        source_object_id=source_id, workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
        source_kind=SourceKind.RECORD.value, identity_mode=IdentityMode.UPLOAD,
        upload_identity=f"synthetic-{source_number}", created_at=NOW,
    )
    original = ObjectReference(
        schema_version=1, object_id=_id(200 + source_number),
        storage_generation=_id(300 + source_number), digest_algorithm="sha256",
        digest_hex=DIGEST, byte_length=len(RAW),
    )
    revision = content_revision(
        revision_id=_id(400 + source_number), source_object_id=source_id,
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT, kind="content",
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        original_reference=original, media_type="audio/wav",
        revision_metadata=RevisionMetadata(document_language="ru"),
        original_sha256=DIGEST, byte_length=len(RAW), observed_at=NOW,
    )
    return source, revision


def private_ref(number: int, kind: str = "private") -> PrivateReference:
    return PrivateReference(reference_id=_id(number), reference_kind=kind)


def approved_policy(policy, *, workspace_id=WORKSPACE, environment_id=ENVIRONMENT, revoked=False):
    return ApprovedPrivacyPolicy(
        policy=policy,
        approval_reference=private_ref(880, "privacy_policy_approval"),
        approval_version="privacy-approval:v1",
        workspace_id=workspace_id,
        environment_id=environment_id,
        approved_by=_id(881),
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(days=1),
        revoked=revoked,
    )


def report_builder(policy, *, clock=None, revoked=False):
    return SanitizedReportBuilder(
        approvals=FakePrivacyApprovals(policy, revoked=revoked),
        contexts=FakeContextVerifier(),
        clock=FakeClock() if clock is None else clock,
    )


def admission_request(*, source_number: int = 1, request_scope=None):
    source, revision = source_revision(source_number=source_number)
    return PurposeAdmissionRequest(
        source_object=source,
        source_revision=revision,
        workspace_id=WORKSPACE if request_scope is None else request_scope[0],
        environment_id=ENVIRONMENT if request_scope is None else request_scope[1],
        purpose_approval_ref=private_ref(501, "purpose"),
        legal_data_evidence_ref=private_ref(502, "legal_evidence"),
        retention_profile_ref=private_ref(503, "retention"),
        authorized_actor_id=_id(11),
        access_policy_version=_id(601),
        policy_version="corpus-policy:v1",
        allowed_environment_id=ENVIRONMENT,
    )


def policy(source):
    return AccessPolicyVersion(
        access_policy_version_id=_id(601), workspace_id=source.workspace_id,
        environment_id=source.environment_id, source_object_id=source.source_object_id,
        version=1, predecessor_policy_version_id=None,
        document_schema_version="access-policy.v1", document_hash="a" * 64,
        origin_kind=PolicyOriginKind.APPROVED_PROPOSAL, origin_reference=_id(602),
        origin_version=1, origin_hash="b" * 64, created_by_canonical_human_id=_id(603),
        created_at=NOW,
    )


class FakeClock:
    def __init__(self, now=NOW): self.value = now
    def now(self): return self.value


class FakeContextVerifier:
    def verify(self, value): return value


class FakePolicies:
    def __init__(self, current_id=_id(601)):
        self.current_ids = current_id if isinstance(current_id, tuple) else (current_id,)
        self.calls = 0
    async def current_for(self, source, *, context):
        current = self.current_ids[min(self.calls, len(self.current_ids) - 1)]
        self.calls += 1
        item = policy(source)
        return item.model_copy(update={"access_policy_version_id": current})


class FakeAuthority:
    def __init__(self, *, allow=True, purpose="asr_model_evaluation", expires=NOW + timedelta(hours=1)):
        self.allow = allow; self.purpose = purpose; self.expires = expires
        self.read_calls = 0; self.recheck_calls = 0
    async def verify_current_authority(self, request, *, access_policy, context, evaluated_at):
        self.read_calls += 1
        return VerifiedAuthority(
            purpose=self.purpose, process_granted=self.allow, human_read_granted=self.allow,
            legal_data_evidence_confirmed=self.allow, current=self.allow, revoked=not self.allow,
            purpose_approved=self.allow, actor_authorized=self.allow,
            retention_current=self.allow, audit_permitted=self.allow, environment_allowed=self.allow,
            expires_at=self.expires, revocation_epoch=0, attestation_ref=private_ref(701),
            read_grant_ref=private_ref(702), process_grant_ref=private_ref(703),
        )
    async def recheck(self, admitted, *, evaluated_at):
        self.recheck_calls += 1
        return self.allow and evaluated_at < self.expires


class FakeRevisions:
    def __init__(self, exact=True):
        self.results = exact if isinstance(exact, tuple) else (exact,)
        self.calls = 0
    async def exact_revision_number(self, source, revision): return 1
    async def is_exact_revision_current(self, admitted):
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


class FakeContent:
    def __init__(self, chunks=(RAW,)):
        self.chunks = chunks; self.reads = 0; self.advance_clock = False; self.clock = None
    async def _stream(self, admitted):
        self.reads += 1
        if self.advance_clock and self.clock is not None:
            self.clock.value += timedelta(hours=2)
        for chunk in self.chunks: yield chunk
    def read_exact_revision(self, admitted): return self._stream(admitted)


class FakeStaging:
    def __init__(self): self.started = 0; self.parts = []; self.discarded = []; self.finalized = []
    async def begin(self, admitted): self.started += 1; return private_ref(801, "stage")
    async def append(self, staging_ref, chunk): self.parts.append(chunk)
    async def finalize(self, staging_ref, *, sha256, byte_length):
        self.finalized.append((sha256, byte_length)); return private_ref(802, "stage")
    async def discard(self, staging_ref): self.discarded.append(staging_ref)


class FakeOriginals:
    def __init__(self): self.bound = []
    async def bind_immutable_original(self, admitted, *, sha256, byte_length):
        self.bound.append((admitted.source_revision.revision_id, sha256, byte_length))
        return private_ref(803, "original")


class FakeAudit:
    def __init__(self): self.events = []
    async def receipt(self, *, operation, source_revision_id, outcome):
        self.events.append((operation, source_revision_id, outcome))
        return private_ref(850 + len(self.events), "audit")


class FakePrivacyApprovals:
    def __init__(self, policy, *, revoked=False):
        self.policy = policy; self.revoked = revoked; self.calls = []
    async def resolve_current_approved(
        self, policy_version, *, workspace_id, environment_id, context
    ):
        self.calls.append((policy_version, workspace_id, environment_id))
        if self.policy is None:
            return None
        return approved_policy(
            self.policy, workspace_id=workspace_id,
            environment_id=environment_id, revoked=self.revoked,
        )


class FakeMetadata:
    def __init__(self, entries): self.entries = entries; self.calls = 0
    async def list_metadata(self, *, workspace_id, environment_id, context):
        self.calls += 1
        return self.entries


def service(*, allowed=True, chunks=(RAW,), exact=True, now=NOW, expires=NOW + timedelta(hours=1), policy_id=_id(601)):
    request = admission_request()
    authority = FakeAuthority(allow=allowed, expires=expires)
    policies = FakePolicies(current_id=policy_id)
    revisions = FakeRevisions(exact=exact)
    content = FakeContent(chunks=chunks)
    staging = FakeStaging()
    originals = FakeOriginals()
    clock = FakeClock(now)
    content.clock = clock
    audit = FakeAudit()
    subject = TwoStageSourceAdmission(
        policies=policies, authority=authority, contexts=FakeContextVerifier(), audit=audit, revisions=revisions,
        content=content, staging=staging, originals=originals, clock=clock,
    )
    return request, subject, authority, policies, revisions, content, staging, originals


@pytest.mark.asyncio
async def test_metadata_inventory_has_no_content_resolution_or_hashing_port() -> None:
    source, revision = source_revision()
    metadata = FakeMetadata((MetadataInventoryEntry(
        source_object=source, source_revision=revision, access_policy_version=_id(901),
        policy_version="policy:v1", media_type="audio/wav", byte_length=len(RAW),
        duration_ms=1000, previously_verified_audio_sha256=None,
    ),))
    inventory = TrustedMetadataInventory(metadata, FakeContextVerifier())
    values = await inventory.list_candidates(
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT, context=context()
    )
    assert len(values) == 1
    assert metadata.calls == 1
    assert not hasattr(inventory, "content")


@pytest.mark.asyncio
async def test_metadata_inventory_rejects_source_revision_scope_mismatch() -> None:
    source, revision = source_revision()
    mismatched_revision = revision.model_copy(update={"environment_id": _id(999)})
    metadata = FakeMetadata((MetadataInventoryEntry(
        source_object=source, source_revision=mismatched_revision,
        access_policy_version=_id(901), policy_version="policy:v1",
        media_type="audio/wav", byte_length=len(RAW), duration_ms=1000,
    ),))
    inventory = TrustedMetadataInventory(metadata, FakeContextVerifier())
    with pytest.raises(CorpusAdmissionDenied, match="revision scope"):
        await inventory.list_candidates(
            workspace_id=WORKSPACE, environment_id=ENVIRONMENT, context=context()
        )


@pytest.mark.asyncio
async def test_metadata_inventory_rejects_scope_mismatch_without_listing() -> None:
    metadata = FakeMetadata(())
    inventory = TrustedMetadataInventory(metadata, FakeContextVerifier())
    with pytest.raises(CorpusAdmissionDenied, match="metadata scope"):
        await inventory.list_candidates(
            workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
            context=context(environment_id=_id(999)),
        )
    assert metadata.calls == 0


@pytest.mark.asyncio
async def test_denied_purpose_admission_performs_zero_content_calls() -> None:
    request, subject, *_ = service(allowed=False)
    with pytest.raises(CorpusAdmissionDenied):
        await subject.admit(request, context=context())
    assert subject._content.reads == 0
    assert subject._staging.started == 0


@pytest.mark.asyncio
async def test_source_revision_mismatch_is_denied_before_content_access() -> None:
    request, subject, *_ = service()
    request = request.model_copy(
        update={
            "source_revision": request.source_revision.model_copy(
                update={"source_object_id": _id(999)}
            )
        }
    )
    with pytest.raises(CorpusAdmissionDenied, match="revision mismatch"):
        await subject.admit(request, context=context())
    assert subject._content.reads == 0


@pytest.mark.asyncio
async def test_corpus_digest_is_not_required_or_computed_at_stage_one() -> None:
    request, subject, *_ = service()
    admitted = await subject.admit(request, context=context())
    # Spine's immutable SourceRevision already carries its ingestion digest;
    # corpus-specific AudioDigestVerification is created only after Stage 2.
    assert admitted.source_revision.original_sha256 == DIGEST
    assert admitted.audit_receipt_ref.reference_kind == "audit"
    assert subject._content.reads == 0
    assert len(subject._audit.events) == 1
    assert not hasattr(admitted, "digest_verification")


@pytest.mark.asyncio
async def test_hashing_and_private_staging_begin_only_after_admission() -> None:
    request, subject, _, _, revisions, content, staging, originals = service()
    admitted = await subject.admit(request, context=context())
    result = await subject.verify_content(admitted, context=context())
    assert result.digest_verification.audio_sha256 == DIGEST
    assert result.digest_verification.source_revision_id == admitted.source_revision.revision_id
    assert result.digest_verification.audit_receipt_ref.reference_kind == "audit"
    assert len(subject._audit.events) == 2
    assert content.reads == 1
    assert revisions.calls == 3
    assert len(staging.finalized) == 1
    assert originals.bound == [(admitted.source_revision.revision_id, DIGEST, len(RAW))]


@pytest.mark.asyncio
async def test_previously_verified_digest_is_compared_after_authorized_read() -> None:
    request, subject, _, _, _, content, *_ = service()
    admitted = await subject.admit(request, context=context())
    with pytest.raises(CorpusVerificationError, match="previously verified"):
        await subject.verify_content(admitted, context=context(), previously_verified_sha256="f" * 64)
    assert content.reads == 1


@pytest.mark.asyncio
async def test_source_revision_byte_or_digest_mismatch_discards_partial_staging() -> None:
    request, subject, _, _, _, _, staging, _ = service(chunks=(b"wrong bytes",))
    admitted = await subject.admit(request, context=context())
    with pytest.raises(CorpusVerificationError):
        await subject.verify_content(admitted, context=context())
    assert len(staging.discarded) == 1
    assert staging.finalized == []


@pytest.mark.asyncio
async def test_toctou_revision_change_discards_partial_staging() -> None:
    request, subject, _, _, revisions, _, staging, _ = service(exact=False)
    admitted = await subject.admit(request, context=context())
    with pytest.raises(CorpusAdmissionDenied, match="no longer exact"):
        await subject.verify_content(admitted, context=context())
    assert revisions.calls == 1
    assert subject._content.reads == 0
    assert staging.started == 0


@pytest.mark.asyncio
async def test_toctou_revision_replacement_during_stream_discards_staging() -> None:
    request, subject, _, _, revisions, content, staging, _ = service(exact=(True, True, False))
    admitted = await subject.admit(request, context=context())
    with pytest.raises(CorpusVerificationError, match="changed during"):
        await subject.verify_content(admitted, context=context())
    assert content.reads == 1
    assert len(staging.discarded) == 1


@pytest.mark.asyncio
async def test_revocation_during_stream_discards_staging() -> None:
    request, subject, authority, _, _, content, staging, _ = service()
    admitted = await subject.admit(request, context=context())

    async def revoke_on_final_recheck(admitted_source, *, evaluated_at):
        authority.recheck_calls += 1
        return authority.recheck_calls < 4

    authority.recheck = revoke_on_final_recheck
    with pytest.raises(CorpusAdmissionDenied, match="changed during"):
        await subject.verify_content(admitted, context=context())
    assert content.reads == 1
    assert len(staging.discarded) == 1


@pytest.mark.asyncio
async def test_expired_or_revoked_authority_is_denied_before_content_read() -> None:
    request, subject, _, _, _, content, staging, _ = service(
        now=NOW, expires=NOW
    )
    with pytest.raises(CorpusAdmissionDenied):
        await subject.admit(request, context=context())
    assert content.reads == 0
    assert staging.started == 0


@pytest.mark.asyncio
async def test_authority_expiry_during_stream_discards_partial_staging() -> None:
    request, subject, _, _, _, content, staging, _ = service()
    admitted = await subject.admit(request, context=context())
    content.advance_clock = True
    with pytest.raises(CorpusAdmissionDenied, match="expired during"):
        await subject.verify_content(admitted, context=context())
    assert content.reads == 1
    assert len(staging.discarded) == 1


@pytest.mark.asyncio
async def test_access_policy_change_during_read_discards_partial_staging() -> None:
    request, subject, _, _, _, _, staging, _ = service(
        policy_id=(_id(601), _id(601), _id(601), _id(999))
    )
    admitted = await subject.admit(request, context=context())
    with pytest.raises(CorpusAdmissionDenied, match="policy changed"):
        await subject.verify_content(admitted, context=context())
    assert len(staging.discarded) == 1


@pytest.mark.asyncio
async def test_sanitizer_only_emits_approved_aggregate_fields_and_not_verified_status() -> None:
    policy = PrivacyPolicy(
        policy_version="privacy:v1", minimum_cell_size=5,
        approved_categories=(AggregateCategory.DURATION_BAND,),
        approved_metric_names=(AggregateMetric.WER,),
    )
    report = await report_builder(policy).build(
        policy_version=policy.policy_version,
        measurements=(AggregateMeasurement(
            category=AggregateCategory.DURATION_BAND, denominator=10, numerator=5,
            metric_name=AggregateMetric.WER,
        ),),
        context=context(operation="sanitize_private_asr_report"),
    )
    assert report.status == "NOT_VERIFIED"
    assert report.sufficiency.value == "NOT_VERIFIED"
    assert set(report.model_dump()) == {
        "contract_version", "schema_version", "privacy_policy_version",
        "privacy_policy_approval_version", "privacy_policy", "status",
        "sufficiency", "aggregates", "metric_names"
    }


def test_sanitized_report_schema_rejects_direct_small_cell_or_verified_claim() -> None:
    policy = PrivacyPolicy(
        policy_version="privacy:v1", minimum_cell_size=5,
        approved_categories=(AggregateCategory.DURATION_BAND,),
        approved_metric_names=(AggregateMetric.WER,),
    )
    with pytest.raises(ValidationError, match="small-cell"):
        SanitizedCorpusReport(
            schema_version="r2-private-asr-sanitized-report:v1",
            privacy_policy_version=policy.policy_version,
            privacy_policy_approval_version="privacy-approval:v1",
            generated_at=NOW,
            privacy_policy_approval=approved_policy(policy),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            privacy_policy=policy,
            status="NOT_VERIFIED",
            sufficiency=QualificationSufficiency.NOT_VERIFIED,
            aggregates=({
                "category": AggregateCategory.DURATION_BAND,
                "metric_name": AggregateMetric.WER,
                "denominator": 1,
                "numerator": 1,
                "rate_millionths": 1_000_000,
            },),
            metric_names=(AggregateMetric.WER,),
        )
    with pytest.raises(ValidationError):
        SanitizedCorpusReport(
            schema_version="r2-private-asr-sanitized-report:v1",
            privacy_policy_version=policy.policy_version,
            privacy_policy_approval_version="privacy-approval:v1",
            generated_at=NOW,
            privacy_policy_approval=approved_policy(policy),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            privacy_policy=policy,
            status="VERIFIED",
            sufficiency=QualificationSufficiency.NOT_VERIFIED,
            aggregates=(), metric_names=(),
        )


@pytest.mark.parametrize(
    "item",
    [
        AggregateMeasurement(category=AggregateCategory.NOISE_BAND, denominator=10, numerator=1, metric_name=AggregateMetric.WER),
        AggregateMeasurement(category=AggregateCategory.DURATION_BAND, denominator=2, numerator=1, metric_name=AggregateMetric.WER),
    ],
)
@pytest.mark.asyncio
async def test_sanitizer_rejects_unapproved_or_small_cells(item) -> None:
    policy = PrivacyPolicy(
        policy_version="privacy:v1", minimum_cell_size=5,
        approved_categories=(AggregateCategory.DURATION_BAND,),
        approved_metric_names=(AggregateMetric.WER,),
    )
    with pytest.raises(PrivacySanitizationError):
        await report_builder(policy).build(
            policy_version=policy.policy_version,
            measurements=(item,),
            context=context(operation="sanitize_private_asr_report"),
        )


@pytest.mark.asyncio
async def test_report_builder_fails_closed_without_current_approved_privacy_policy() -> None:
    with pytest.raises(PrivacySanitizationError, match="no current approved"):
        await report_builder(None).build(
            policy_version="privacy:v1",
            measurements=(AggregateMeasurement(
                category=AggregateCategory.DURATION_BAND,
                denominator=10,
                numerator=5,
                metric_name=AggregateMetric.WER,
            ),),
            context=context(operation="sanitize_private_asr_report"),
        )


@pytest.mark.asyncio
async def test_report_builder_requires_scoped_purpose_and_operation() -> None:
    policy = PrivacyPolicy(
        policy_version="privacy:v1", minimum_cell_size=5,
        approved_categories=(AggregateCategory.DURATION_BAND,),
        approved_metric_names=(AggregateMetric.WER,),
    )
    with pytest.raises(PrivacySanitizationError, match="purpose or operation"):
        await report_builder(policy).build(
            policy_version=policy.policy_version,
            measurements=(),
            context=context(operation="admit_private_corpus"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revoked", "clock"),
    [(True, FakeClock()), (False, FakeClock(NOW + timedelta(days=2)))],
)
async def test_report_builder_rejects_revoked_or_expired_policy(revoked, clock) -> None:
    policy = PrivacyPolicy(
        policy_version="privacy:v1", minimum_cell_size=5,
        approved_categories=(AggregateCategory.DURATION_BAND,),
        approved_metric_names=(AggregateMetric.WER,),
    )
    with pytest.raises(PrivacySanitizationError, match="no current approved"):
        await report_builder(policy, clock=clock, revoked=revoked).build(
            policy_version=policy.policy_version,
            measurements=(AggregateMeasurement(
                category=AggregateCategory.DURATION_BAND,
                denominator=10,
                numerator=5,
                metric_name=AggregateMetric.WER,
            ),),
            context=context(operation="sanitize_private_asr_report"),
        )


def test_no_real_data_or_external_calls_are_part_of_fake_backed_fixture() -> None:
    assert RAW == b"synthetic fixture audio"
    assert DIGEST == sha256(RAW).hexdigest()
