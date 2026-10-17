from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.auth import AuthorizationDeniedError, AuthorizedRequestContext, RequestedAuthorizationScope
from spine.evals.real_qa_r1 import (
    REAL_R1_CASE_SCHEMA_VERSION,
    RealCaseObservationInput,
    RealQACase,
    RealRuntimeMappingManifest,
    RealRuntimeMappingResolver,
    RealRuntimeCaseResolution,
    RealRuntimeRevisionRecord,
    RealRuntimeResolutionCategory,
    RealRuntimeResolutionError,
    RealRuntimeSourceIntent,
    RealRuntimeRevisionIntent,
    runtime_mapping_manifest_digest,
)


WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000101")
OTHER_WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000102")
ENVIRONMENT_ID = UUID("00000000-0000-0000-0000-000000000201")
PRINCIPAL_ID = UUID("00000000-0000-0000-0000-000000000301")
SOURCE_OBJECT_ID = UUID("00000000-0000-0000-0000-000000000401")
REVISION_ID = UUID("00000000-0000-0000-0000-000000000501")
ACCEPTANCE_ID = UUID("00000000-0000-0000-0000-000000000601")
SHA256 = "a" * 64
_DEFAULT_CONTEXT = object()


def _manifest() -> RealRuntimeMappingManifest:
    return RealRuntimeMappingManifest(
        dataset_version="0.3.9-user-approved",
        dataset_sha256=SHA256,
        owner_approval_ref="approval:test-fixture",
        sources=(
            RealRuntimeSourceIntent(
                dataset_source_id="source:opaque-7",
                upload_identity="approved.source.7",
                revisions=(
                    RealRuntimeRevisionIntent(
                        dataset_revision_id="revision:opaque-11",
                        expected_original_sha256=SHA256,
                        ingestion_intent_id="intent:revision-11",
                    ),
                ),
            ),
        ),
    )


def _case(**updates: object) -> RealQACase:
    fields: dict[str, object] = {
        "case_schema_version": REAL_R1_CASE_SCHEMA_VERSION,
        "case_id": "case:opaque-3",
        "question": "Which approved fact is present?",
        "language": "en",
        "expected_outcome": "answered",
        "expected_answer": "The protected gold answer.",
        "expected_claims": ({"claim_id": "claim-1", "text": "Protected claim", "evidence_ids": ["evidence-1"]},),
        "expected_evidence_ids": ("evidence-1",),
        "citations": (),
        "source_ids": ("source:opaque-7",),
        "source_revision_ids": ("revision:opaque-11",),
        "available_source_revision_ids": ("revision:opaque-11",),
        "question_type": "fact_lookup",
        "difficulty": "easy",
        "synthetic": False,
    }
    fields.update(updates)
    # This helper models a structurally validated answered RealQACase; citation
    # contents are not part of the resolver input.
    fields["citations"] = ({
        "evidence_id": "evidence-1", "source_id": "source:opaque-7",
        "source_revision_id": "revision:opaque-11", "excerpt": "approved excerpt",
        "locator": {"kind": "pdf_page", "schema_version": "locator:pdf-page-char:v1",
                    "page": 1, "char_start": 0, "char_end": 16},
    },)
    return RealQACase.model_validate(fields)


class _CanonicalMappings:
    def __init__(self, records: tuple[RealRuntimeRevisionRecord, ...]) -> None:
        self.records = records

    async def resolve_revision(self, **_: object) -> tuple[RealRuntimeRevisionRecord, ...]:
        return self.records


class _CanonicalMappingsByIntent:
    def __init__(self, records: dict[str, tuple[RealRuntimeRevisionRecord, ...]]) -> None:
        self.records = records

    async def resolve_revision(
        self, *, ingestion_intent_id: str, **_: object
    ) -> tuple[RealRuntimeRevisionRecord, ...]:
        return self.records[ingestion_intent_id]


class _TrustedContext:
    def __init__(self, context: object | None = None, *, error: Exception | None = None) -> None:
        self.context = context
        self.error = error

    async def resolve(self, **_: object) -> object | None:
        if self.error is not None:
            raise self.error
        return self.context


class _BindingStore:
    def __init__(self) -> None:
        self.bindings: dict[tuple[str, str, str], RealRuntimeRevisionBinding] = {}
        self.persist_count = 0

    async def resolve_binding(
        self,
        *,
        manifest_sha256: str,
        dataset_source_id: str,
        dataset_revision_id: str,
    ) -> tuple[RealRuntimeRevisionBinding, ...]:
        binding = self.bindings.get((manifest_sha256, dataset_source_id, dataset_revision_id))
        return () if binding is None else (binding,)

    async def persist_verified_binding(
        self, binding: RealRuntimeRevisionBinding
    ) -> RealRuntimeRevisionBinding:
        self.persist_count += 1
        key = (binding.manifest_sha256, binding.dataset_source_id, binding.dataset_revision_id)
        existing = self.bindings.setdefault(key, binding)
        return existing


def _trusted_context(*, workspace_id: UUID = WORKSPACE_ID, environment_id: UUID = ENVIRONMENT_ID) -> AuthorizedRequestContext:
    context = MagicMock(spec=AuthorizedRequestContext)
    context.acting_subject_id = PRINCIPAL_ID
    context.scope = RequestedAuthorizationScope(workspace_id=workspace_id, environment_id=environment_id)
    context.authorization_generation = 4
    return context


def _record(**updates: object) -> RealRuntimeRevisionRecord:
    fields: dict[str, object] = {
        "upload_identity": "approved.source.7",
        "ingestion_intent_id": "intent:revision-11",
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "source_object_id": SOURCE_OBJECT_ID,
        "source_revision_id": REVISION_ID,
        "canonical_acceptance_id": ACCEPTANCE_ID,
        "original_sha256": SHA256,
        "is_current": True,
    }
    fields.update(updates)
    return RealRuntimeRevisionRecord.model_validate(fields)


async def _resolve(
    *,
    records: tuple[RealRuntimeRevisionRecord, ...] | None = None,
    context: object = _DEFAULT_CONTEXT,
    case: RealQACase | None = None,
    binding_store: _BindingStore | None = None,
) -> RealRuntimeCaseResolution:
    resolver = RealRuntimeMappingResolver(
        canonical_mappings=_CanonicalMappings((_record(),) if records is None else records),
        binding_store=_BindingStore() if binding_store is None else binding_store,
        trusted_contexts=_TrustedContext(_trusted_context() if context is _DEFAULT_CONTEXT else context),
    )
    return await resolver.resolve(
        manifest=_manifest(),
        case=RealCaseObservationInput.from_case(
            _case() if case is None else case,
            dataset_version="0.3.9-user-approved",
            dataset_sha256=SHA256,
        ),
        workspace_selector=WORKSPACE_ID,
        environment_selector=ENVIRONMENT_ID,
    )


@pytest.mark.asyncio
async def test_real_case_resolves_to_gold_blind_request_with_canonical_scope() -> None:
    request = await _resolve()

    request = request.request
    assert request.case_id == "case:opaque-3"
    assert request.available_revision_ids == (REVISION_ID,)
    assert request.workspace_id == WORKSPACE_ID
    assert request.environment_id == ENVIRONMENT_ID
    assert request.principal_id == PRINCIPAL_ID
    assert "expected_answer" not in request.model_dump()
    assert "expected_claims" not in request.model_dump()
    assert "expected_evidence_ids" not in request.model_dump()
    assert request.schema_version == "r1-production-contract-case-request:v1"
    assert request.model_dump().get("golden_answer") is None
    resolution = await _resolve()
    assert len(resolution.bindings) == 1
    binding = resolution.bindings[0]
    assert binding.dataset_source_id == "source:opaque-7"
    assert binding.dataset_revision_id == "revision:opaque-11"
    assert binding.upload_identity == "approved.source.7"
    assert binding.source_object_id == SOURCE_OBJECT_ID
    assert binding.source_revision_id == REVISION_ID
    assert binding.canonical_acceptance_id == ACCEPTANCE_ID
    assert binding.currentness == "current"
    assert resolution.authorization_generation == 4
    assert len(resolution.binding_sha256) == 64


@pytest.mark.asyncio
async def test_missing_or_ambiguous_canonical_mapping_fails_closed() -> None:
    for records, category in (
        ((), RealRuntimeResolutionCategory.MAPPING_MISSING),
        ((_record(), _record(source_revision_id=UUID("00000000-0000-0000-0000-000000000502"))), RealRuntimeResolutionCategory.MAPPING_AMBIGUOUS),
    ):
        with pytest.raises(RealRuntimeResolutionError) as error:
            await _resolve(records=records)
        assert error.value.category is category
        assert "Protected gold" not in str(error.value)


@pytest.mark.asyncio
async def test_duplicate_or_conflicting_canonical_binding_fails_closed() -> None:
    records = (
        _record(),
        _record(source_revision_id=UUID("00000000-0000-0000-0000-000000000502")),
    )
    with pytest.raises(RealRuntimeResolutionError) as error:
        await _resolve(records=records)
    assert error.value.category is RealRuntimeResolutionCategory.MAPPING_AMBIGUOUS

    second_revision = UUID("00000000-0000-0000-0000-000000000502")
    second_source = UUID("00000000-0000-0000-0000-000000000402")
    mappings = _CanonicalMappingsByIntent(
        {
            "intent:revision-11": (_record(),),
            "intent:revision-12": (
                _record(
                    ingestion_intent_id="intent:revision-12",
                    source_revision_id=second_revision,
                    source_object_id=second_source,
                ),
            ),
        }
    )
    resolver = RealRuntimeMappingResolver(
        canonical_mappings=mappings,
        binding_store=_BindingStore(),
        trusted_contexts=_TrustedContext(_trusted_context()),
    )
    case = _case(
        source_revision_ids=("revision:opaque-11", "revision:opaque-12"),
        available_source_revision_ids=("revision:opaque-11", "revision:opaque-12"),
    )
    manifest = _manifest().model_copy(
        update={
            "sources": (
                RealRuntimeSourceIntent(
                    dataset_source_id="source:opaque-7",
                    upload_identity="approved.source.7",
                    revisions=(
                        RealRuntimeRevisionIntent(
                            dataset_revision_id="revision:opaque-11",
                            expected_original_sha256=SHA256,
                            ingestion_intent_id="intent:revision-11",
                        ),
                        RealRuntimeRevisionIntent(
                            dataset_revision_id="revision:opaque-12",
                            expected_original_sha256=SHA256,
                            ingestion_intent_id="intent:revision-12",
                        ),
                    ),
                ),
            )
        }
    )

    with pytest.raises(RealRuntimeResolutionError) as error:
        await resolver.resolve(
            manifest=manifest,
            case=RealCaseObservationInput.from_case(
                case, dataset_version="0.3.9-user-approved", dataset_sha256=SHA256
            ),
            workspace_selector=WORKSPACE_ID,
            environment_selector=ENVIRONMENT_ID,
        )
    assert error.value.category is RealRuntimeResolutionCategory.MAPPING_CONFLICT


@pytest.mark.asyncio
async def test_non_current_or_stale_revision_fails_closed() -> None:
    with pytest.raises(RealRuntimeResolutionError) as error:
        await _resolve(records=(_record(is_current=False),))
    assert error.value.category is RealRuntimeResolutionCategory.REVISION_NOT_CURRENT

    with pytest.raises(RealRuntimeResolutionError) as error:
        await _resolve(records=(_record(original_sha256="b" * 64),))
    assert error.value.category is RealRuntimeResolutionCategory.REVISION_STALE


@pytest.mark.asyncio
async def test_cross_workspace_mapping_and_selector_mismatch_fail_closed() -> None:
    with pytest.raises(RealRuntimeResolutionError) as error:
        await _resolve(records=(_record(workspace_id=OTHER_WORKSPACE_ID),))
    assert error.value.category is RealRuntimeResolutionCategory.SCOPE_MISMATCH

    with pytest.raises(RealRuntimeResolutionError) as error:
        await _resolve(context=_trusted_context(workspace_id=OTHER_WORKSPACE_ID))
    assert error.value.category is RealRuntimeResolutionCategory.SCOPE_MISMATCH


@pytest.mark.asyncio
async def test_unavailable_or_untrusted_authorization_context_fails_closed() -> None:
    with pytest.raises(RealRuntimeResolutionError) as error:
        await _resolve(context=None)
    assert error.value.category is RealRuntimeResolutionCategory.AUTHORIZATION_UNAVAILABLE

    with pytest.raises(RealRuntimeResolutionError) as error:
        await _resolve(context=object())
    assert error.value.category is RealRuntimeResolutionCategory.AUTHORIZATION_UNAVAILABLE


@pytest.mark.asyncio
async def test_authorization_denial_fails_without_disclosing_protected_data(caplog: pytest.LogCaptureFixture) -> None:
    resolver = RealRuntimeMappingResolver(
        canonical_mappings=_CanonicalMappings((_record(),)),
        binding_store=_BindingStore(),
        trusted_contexts=_TrustedContext(error=AuthorizationDeniedError()),
    )

    with pytest.raises(RealRuntimeResolutionError) as error:
        await resolver.resolve(
            manifest=_manifest(),
            case=RealCaseObservationInput.from_case(
                _case(), dataset_version="0.3.9-user-approved", dataset_sha256=SHA256
            ),
            workspace_selector=WORKSPACE_ID,
            environment_selector=ENVIRONMENT_ID,
        )

    assert error.value.category is RealRuntimeResolutionCategory.AUTHORIZATION_DENIED
    assert "Protected gold" not in str(error.value)
    assert "Protected gold" not in repr(error.value.args)
    assert "Protected gold" not in caplog.text


def test_mapping_manifest_rejects_duplicate_opaque_revision_keys() -> None:
    manifest = _manifest().model_dump()
    source = manifest["sources"][0]
    source["revisions"] = [source["revisions"][0], source["revisions"][0]]

    with pytest.raises(ValidationError):
        RealRuntimeMappingManifest.model_validate(manifest)


def test_gold_label_fields_are_forbidden_at_real_case_observation_boundary() -> None:
    case_input = RealCaseObservationInput.from_case(
        _case(), dataset_version="0.3.9-user-approved", dataset_sha256=SHA256
    )

    with pytest.raises(ValidationError):
        RealCaseObservationInput.model_validate(
            {**case_input.model_dump(), "expected_answer": "Protected gold"}
        )


@pytest.mark.asyncio
async def test_resolver_rejects_directly_constructed_real_case_input() -> None:
    resolver = RealRuntimeMappingResolver(
        canonical_mappings=_CanonicalMappings((_record(),)),
        binding_store=_BindingStore(),
        trusted_contexts=_TrustedContext(_trusted_context()),
    )
    unverified = RealCaseObservationInput(
        dataset_version="0.3.9-user-approved",
        dataset_sha256=SHA256,
        case_id="case:opaque-3",
        question="Question without validated real-case structure",
        language="en",
        source_ids=("source:opaque-7",),
        source_revision_ids=("revision:opaque-11",),
        available_source_revision_ids=("revision:opaque-11",),
    )

    with pytest.raises(RealRuntimeResolutionError) as error:
        await resolver.resolve(
            manifest=_manifest(), case=unverified,
            workspace_selector=WORKSPACE_ID, environment_selector=ENVIRONMENT_ID,
        )
    assert error.value.category is RealRuntimeResolutionCategory.INVALID_CASE


@pytest.mark.asyncio
async def test_resolver_rejects_case_input_changed_after_validation() -> None:
    resolver = RealRuntimeMappingResolver(
        canonical_mappings=_CanonicalMappings((_record(),)),
        binding_store=_BindingStore(),
        trusted_contexts=_TrustedContext(_trusted_context()),
    )
    validated = RealCaseObservationInput.from_case(
        _case(), dataset_version="0.3.9-user-approved", dataset_sha256=SHA256
    )
    changed = validated.model_copy(
        update={"available_source_revision_ids": ("revision:unmapped",)}
    )

    with pytest.raises(RealRuntimeResolutionError) as error:
        await resolver.resolve(
            manifest=_manifest(), case=changed,
            workspace_selector=WORKSPACE_ID, environment_selector=ENVIRONMENT_ID,
        )
    assert error.value.category is RealRuntimeResolutionCategory.INVALID_CASE


@pytest.mark.asyncio
async def test_exact_verified_binding_is_reused_on_repeated_resolution() -> None:
    store = _BindingStore()
    first = await _resolve(binding_store=store)
    second = await _resolve(binding_store=store)

    assert first.bindings == second.bindings
    assert first.binding_sha256 == second.binding_sha256
    assert store.persist_count == 1


@pytest.mark.asyncio
async def test_manifest_from_another_dataset_version_fails_closed() -> None:
    manifest = _manifest().model_copy(update={"dataset_version": "another-version"})
    resolver = RealRuntimeMappingResolver(
        canonical_mappings=_CanonicalMappings((_record(),)),
        binding_store=_BindingStore(),
        trusted_contexts=_TrustedContext(_trusted_context()),
    )

    with pytest.raises(RealRuntimeResolutionError) as error:
        await resolver.resolve(
            manifest=manifest,
            case=RealCaseObservationInput.from_case(
                _case(), dataset_version="0.3.9-user-approved", dataset_sha256=SHA256
            ),
            workspace_selector=WORKSPACE_ID,
            environment_selector=ENVIRONMENT_ID,
        )
    assert error.value.category is RealRuntimeResolutionCategory.MANIFEST_MISMATCH


def test_manifest_digest_is_stable_and_version_pinned() -> None:
    manifest = _manifest()

    assert runtime_mapping_manifest_digest(manifest) == runtime_mapping_manifest_digest(manifest)
    assert manifest.schema_version == "r1-runtime-mapping-manifest:v1"
