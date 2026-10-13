from __future__ import annotations

import pytest

from spine.application.persistence.errors import ParsedArtifactConflictError
from spine.application.persistence.parsed_artifacts import ParsedArtifactCommand
from spine.infrastructure.persistence.parsed_artifacts import InMemoryParsedArtifactRepository
from tests.domain.artifacts.test_parsed_documents import revision


@pytest.mark.asyncio
async def test_parsed_artifact_replay_has_one_immutable_effect() -> None:
    from spine.ingestion.parsers import parse_document

    content = b"one\ntwo\n"
    document = parse_document(revision(content), content)
    command = ParsedArtifactCommand.create(document, command_identity="parse-1")
    repository = InMemoryParsedArtifactRepository()

    first = await repository.persist(command)
    replay = await repository.persist(command)

    assert first.replay is False
    assert replay.replay is True
    assert first.document.artifact_id == replay.document.artifact_id
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_changed_command_digest_conflicts_without_mutation() -> None:
    from spine.ingestion.parsers import parse_document

    first_content = b"one\n"
    first = parse_document(revision(first_content), first_content)
    second_content = b"two\n"
    second = parse_document(revision(second_content), second_content)
    repository = InMemoryParsedArtifactRepository()

    await repository.persist(ParsedArtifactCommand.create(first, command_identity="parse-1"))
    with pytest.raises(ParsedArtifactConflictError):
        await repository.persist(ParsedArtifactCommand.create(second, command_identity="parse-1"))
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_resolver_requires_exact_revision_and_parser_identity() -> None:
    from spine.ingestion.parsers import parse_document

    content = b"one\n"
    document = parse_document(revision(content), content)
    repository = InMemoryParsedArtifactRepository()
    await repository.persist(ParsedArtifactCommand.create(document, command_identity="parse-1"))
    locator = document.segments[0].locator

    assert await repository.resolve_segment(
        workspace_id=document.workspace_id,
        environment_id=document.environment_id,
        source_revision_id=document.source_revision_id,
        source_revision_digest=document.source_revision_digest,
        original_sha256=document.original_sha256,
        parser_version=document.parser_version,
        locator_contract_version=document.locator_contract_version,
        locator=locator,
    ) == document.segments[0]
    assert await repository.resolve_segment(
        workspace_id=document.workspace_id,
        environment_id=document.environment_id,
        source_revision_id=document.source_revision_id,
        source_revision_digest="f" * 64,
        original_sha256=document.original_sha256,
        parser_version=document.parser_version,
        locator_contract_version=document.locator_contract_version,
        locator=locator,
    ) is None
    assert await repository.resolve_segment(
        workspace_id=document.workspace_id,
        environment_id=document.environment_id,
        source_revision_id=document.source_revision_id,
        source_revision_digest=document.source_revision_digest,
        original_sha256=document.original_sha256,
        parser_version="other-parser:v1",
        locator_contract_version=document.locator_contract_version,
        locator=locator,
    ) is None
    assert await repository.resolve_segment(
        workspace_id=document.workspace_id,
        environment_id=document.environment_id,
        source_revision_id=document.source_revision_id,
        source_revision_digest=document.source_revision_digest,
        original_sha256=document.original_sha256,
        parser_version=document.parser_version,
        locator_contract_version="other-locator:v1",
        locator=locator,
    ) is None


@pytest.mark.asyncio
async def test_same_command_identity_is_isolated_by_workspace_and_environment() -> None:
    from spine.ingestion.parsers import parse_document

    content = b"one\n"
    first = parse_document(revision(content), content)
    second = parse_document(
        revision(content, workspace_id=first.workspace_id.__class__(int=7), environment_id=first.environment_id.__class__(int=8)),
        content,
    )
    repository = InMemoryParsedArtifactRepository()

    await repository.persist(ParsedArtifactCommand.create(first, command_identity="parse-1"))
    await repository.persist(ParsedArtifactCommand.create(second, command_identity="parse-1"))

    assert await repository.count() == 2
