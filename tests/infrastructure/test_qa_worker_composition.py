from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.infrastructure.qa_worker_composition import (
    _ParsedProjectionRetrieval,
    _projection_reference_matches_evidence,
)
from spine.memory.contracts import ProjectionOutcome


class _Rows:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def mappings(self) -> "_Rows":
        return self

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class _Session:
    def __init__(self, row: SimpleNamespace) -> None:
        self._row = row

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement: object, parameters: object) -> _Rows:
        del parameters
        sql = str(statement)
        if "set_config" in sql:
            return _Rows([])
        rows = (
            [self._row]
            if "r.segment_id = CONCAT('segment-'" in sql
            and "r.projected_object_id = CAST(s.segment_id AS text)" not in sql
            and "JOIN spine.source_revisions revision" in sql
            and "JOIN spine.canonical_boundary_members boundary_member" in sql
            else []
        )
        return _Rows(rows)


@pytest.mark.asyncio
async def test_parsed_retrieval_joins_canonical_segment_with_physical_object_id() -> None:
    workspace_id = UUID(int=1)
    environment_id = UUID(int=2)
    source_object_id = UUID(int=3)
    source_revision_id = UUID(int=4)
    segment_id = UUID(int=5)
    physical_data_id = UUID(int=6)
    source = SimpleNamespace(
        source_object_id=source_object_id,
        source_revision_id=source_revision_id,
        revision_digest=None,
        revision_number=None,
        processing_receipt_id=UUID(int=7),
        locator_contract_version="physical-line-codepoint:v1",
    )
    row = SimpleNamespace(
        source_revision_id=source_revision_id,
        source_object_id=source_object_id,
        segment_id=segment_id,
        segment_digest="b" * 64,
        text="Synthetic qualification evidence.",
        locator_reference="character_range:line:1:0-33",
        locator_digest="c" * 64,
        projected_kind="chunk",
        projected_object_id=str(physical_data_id),
        evidence_id=f"segment-{segment_id.hex}",
        revision_digest="a" * 64,
        revision_number=3,
    )
    command = SimpleNamespace(
        workspace_id=workspace_id,
        environment_id=environment_id,
        retrieval_sources=(source,),
        projection_run_id=UUID(int=8),
        projection_candidate=SimpleNamespace(snapshot_id=UUID(int=9)),
        limit=10,
        command_identity="retrieve-1",
        command_digest="d" * 64,
    )
    retrieval = _ParsedProjectionRetrieval(lambda: _Session(row))

    result = await retrieval.retrieve(command)

    assert result.outcome is ProjectionOutcome.SUCCEEDED
    assert result.chunks[0].chunk_id == str(physical_data_id)
    assert result.evidence[0].evidence_id == f"segment-{segment_id.hex}"
    assert result.evidence[0].revision_digest == "a" * 64
    assert result.evidence[0].revision_number == 3


def test_citation_lineage_matches_canonical_segment_not_physical_object_id() -> None:
    common = {
        "source_object_id": UUID(int=1),
        "source_revision_id": UUID(int=2),
        "processing_receipt_id": UUID(int=3),
        "projected_kind": "chunk",
        "locator_digest": "a" * 64,
    }
    reference = SimpleNamespace(
        **common,
        segment_id="segment-canonical",
        projected_object_id="chunk-physical",
    )
    evidence = SimpleNamespace(**common, evidence_id="segment-canonical")

    assert _projection_reference_matches_evidence(reference, evidence)
