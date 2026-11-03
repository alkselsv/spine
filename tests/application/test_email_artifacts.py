from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from uuid import UUID

import pytest

from spine.application.email_artifacts import (
    AcceptedEmailParent,
    EmailArtifactProcessor,
    EmailArtifactUnavailable,
)
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.processing_disposition import (
    OrdinaryProcessingEligibility,
    OrdinaryProcessingEligibilityReason,
)
from spine.application.storage.authorized_read import OriginalReadDenied
from spine.domain.artifacts.email_html import EmailHtmlSanitizerProfile
from spine.domain.artifacts.email_mime import MimeParseDisposition
from spine.infrastructure.persistence.email_artifacts import InMemoryEmailArtifactRepository
from spine.ingestion.email_html import EmailHtmlSanitizer
from spine.ingestion.email_mime import MimeParser, MimeParserProfile

WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")
SOURCE = UUID("30000000-0000-4000-8000-000000000001")
REVISION = UUID("40000000-0000-4000-8000-000000000001")
ACCEPTANCE = UUID("50000000-0000-4000-8000-000000000001")
TRACE = UUID("60000000-0000-4000-8000-000000000001")
RAW = (
    b"From: synthetic@example.test\r\n"
    b"Subject: synthetic subject\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: multipart/alternative; boundary=part\r\n\r\n"
    b"--part\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nPlain body\r\n"
    b"--part\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
    b"<p>HTML body</p><script>unsafe()</script>\r\n"
    b"--part--\r\n"
)


class FakeEligibility:
    def __init__(self, values=None):
        self.values = list(values or [OrdinaryProcessingEligibility(
            eligible=True,
            reason=OrdinaryProcessingEligibilityReason.ELIGIBLE,
            canonical_acceptance_id=ACCEPTANCE,
        )])
        self.calls = 0

    async def resolve(self, **_values):
        self.calls += 1
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class FakeParentReader:
    def __init__(self, *, workspace_id=WORKSPACE):
        self.calls = 0
        self.workspace_id = workspace_id

    async def resolve_accepted_email_parent(self, **_values):
        self.calls += 1
        return AcceptedEmailParent(
            workspace_id=self.workspace_id,
            environment_id=ENVIRONMENT,
            source_object_id=SOURCE,
            source_revision_id=REVISION,
            source_revision_digest="a" * 64,
            original_sha256=hashlib.sha256(RAW).hexdigest(),
            raw_byte_length=len(RAW),
        )


class FakeGateway:
    def __init__(self, raw=RAW, *, denied=False):
        self.raw = raw
        self.denied = denied
        self.calls = []

    def read_original(self, **kwargs) -> AsyncIterator[bytes]:
        self.calls.append(kwargs)
        async def stream():
            if self.denied:
                raise OriginalReadDenied()
            yield self.raw
        return stream()


def _processor(*, parser_profile=None, eligibility=None, gateway=None, parent=None, repository=None):
    parser = MimeParser(profile=parser_profile or MimeParserProfile(
        max_total_bytes=10_000,
        max_header_bytes=5_000,
        max_parts=30,
        max_depth=10,
        max_decoded_bytes=10_000,
        max_parse_seconds=2.0,
    ))
    sanitizer = EmailHtmlSanitizer(profile=EmailHtmlSanitizerProfile(
        max_total_input_bytes=10_000,
        max_total_output_bytes=10_000,
        max_parts=30,
        max_depth=10,
    ))
    repo = repository or InMemoryEmailArtifactRepository()
    return EmailArtifactProcessor(
        gateway=gateway or FakeGateway(),
        eligibility=eligibility or FakeEligibility(),
        parents=parent or FakeParentReader(),
        repository=repo,
        parser=parser,
        sanitizer=sanitizer,
        outbox_events=OutboxEventRegistry(),
    ), repo


async def _process(processor):
    return await processor.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        trace_id=TRACE,
        max_bytes=10_000,
    )


@pytest.mark.asyncio
async def test_accepted_email_revision_persists_safe_body_artifact_and_replays() -> None:
    processor, repository = _processor()

    first = await _process(processor)
    replay = await _process(processor)

    assert first.replay is False
    assert replay.replay is True
    assert first.artifact == replay.artifact
    assert first.artifact.source_revision_id == REVISION
    assert first.artifact.source_revision_digest == "a" * 64
    assert first.artifact.canonical_acceptance_id == ACCEPTANCE
    assert first.artifact.raw_sha256 == hashlib.sha256(RAW).hexdigest()
    assert first.artifact.selected_representation == "plain_text"
    assert first.artifact.selected_text == "Plain body"
    assert "unsafe()" not in str(first.artifact.sanitized_html)
    assert await repository.get(
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT, artifact_id=first.artifact.artifact_id
    ) == first.artifact
    assert await repository.get(
        workspace_id=UUID("10000000-0000-4000-8000-000000000099"),
        environment_id=ENVIRONMENT, artifact_id=first.artifact.artifact_id
    ) is None
    assert len(await repository.list_for_revision(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_revision_id=REVISION,
    )) == 1


@pytest.mark.asyncio
async def test_parser_profile_change_creates_a_successor_artifact() -> None:
    repo = InMemoryEmailArtifactRepository()
    first_processor, _ = _processor(repository=repo)
    successor_profile = MimeParserProfile(
        max_total_bytes=20_000,
        max_header_bytes=5_000,
        max_parts=30,
        max_depth=10,
        max_decoded_bytes=10_000,
        max_parse_seconds=2.0,
    )
    successor_processor, _ = _processor(parser_profile=successor_profile, repository=repo)

    first = await _process(first_processor)
    successor = await _process(successor_processor)

    assert successor.replay is False
    assert successor.artifact.artifact_id != first.artifact.artifact_id
    assert successor.artifact.parser_profile != first.artifact.parser_profile
    assert len(await repo.list_for_revision(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_revision_id=REVISION,
    )) == 2
    assert await repo.projection_intent_count() == 2


@pytest.mark.asyncio
async def test_unaccepted_or_ineligible_revision_is_rejected_before_original_read() -> None:
    eligibility = FakeEligibility([OrdinaryProcessingEligibility(
        eligible=False,
        reason=OrdinaryProcessingEligibilityReason.OPAQUE_ACCEPTANCE,
    )])
    gateway = FakeGateway()
    parent = FakeParentReader()
    processor, repository = _processor(eligibility=eligibility, gateway=gateway, parent=parent)

    with pytest.raises(EmailArtifactUnavailable):
        await _process(processor)

    assert gateway.calls == []
    assert parent.calls == 0
    assert await repository.list_for_revision(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_revision_id=REVISION,
    ) == ()


@pytest.mark.asyncio
async def test_lifecycle_change_during_original_read_stops_before_artifact_creation() -> None:
    eligible = OrdinaryProcessingEligibility(
        eligible=True,
        reason=OrdinaryProcessingEligibilityReason.ELIGIBLE,
        canonical_acceptance_id=ACCEPTANCE,
    )
    revoked = OrdinaryProcessingEligibility(
        eligible=False,
        reason=OrdinaryProcessingEligibilityReason.CURRENT_REVISION_MISMATCH,
    )
    eligibility = FakeEligibility([eligible, revoked])
    processor, repository = _processor(eligibility=eligibility)

    with pytest.raises(EmailArtifactUnavailable):
        await _process(processor)

    assert eligibility.calls == 2
    assert await repository.list_for_revision(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_revision_id=REVISION,
    ) == ()
    assert await repository.projection_intent_count() == 0


@pytest.mark.asyncio
async def test_wrong_tenant_parent_is_rejected_before_original_read() -> None:
    gateway = FakeGateway()
    parent = FakeParentReader(workspace_id=UUID("10000000-0000-4000-8000-000000000099"))
    processor, repository = _processor(gateway=gateway, parent=parent)

    with pytest.raises(EmailArtifactUnavailable):
        await _process(processor)

    assert gateway.calls == []
    assert await repository.projection_intent_count() == 0


@pytest.mark.asyncio
async def test_process_content_denial_stops_without_persisting_metadata() -> None:
    gateway = FakeGateway(denied=True)
    processor, repository = _processor(gateway=gateway)

    with pytest.raises(EmailArtifactUnavailable) as error:
        await _process(processor)

    assert str(error.value) == "email content is unavailable"
    assert "synthetic" not in str(error.value)
    assert gateway.calls[0]["purpose"] == "process_content"
    assert await repository.list_for_revision(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_revision_id=REVISION,
    ) == ()
    assert await repository.projection_intent_count() == 0


@pytest.mark.asyncio
async def test_original_integrity_mismatch_does_not_create_an_artifact() -> None:
    gateway = FakeGateway(RAW.replace(b"Plain body", b"Other body"))
    processor, repository = _processor(gateway=gateway)

    with pytest.raises(EmailArtifactUnavailable):
        await _process(processor)

    assert await repository.list_for_revision(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_revision_id=REVISION,
    ) == ()


@pytest.mark.asyncio
async def test_malformed_mime_is_recorded_as_a_bounded_defective_artifact() -> None:
    malformed = b"Content-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\n"
    gateway = FakeGateway(malformed)
    parent = FakeParentReader()
    parent.resolve_accepted_email_parent = lambda **_values: _resolved_parent(malformed)
    processor, _repository = _processor(gateway=gateway, parent=parent)

    result = await _process(processor)

    assert result.artifact.mime.disposition is MimeParseDisposition.MALFORMED
    assert result.artifact.mime.defects
    assert result.artifact.selected_representation == "none"
    assert result.artifact.selected_text is None


async def _resolved_parent(raw: bytes):
    return AcceptedEmailParent(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        source_revision_digest="a" * 64,
        original_sha256=hashlib.sha256(raw).hexdigest(),
        raw_byte_length=len(raw),
    )
