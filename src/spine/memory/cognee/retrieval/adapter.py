"""Read-side Cognee 1.5.4 adapter for structured ContextProjection results.

The adapter deliberately consumes only the public ``cognee.search`` surface.
Cognee result objects are treated as untrusted physical observations: a result
is returned only after its workspace, environment, source revision and stable
locator can be reconstructed from the resolved command context.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError

from spine.memory.contracts import (
    EvidenceReference,
    ProjectionChunk,
    ProjectionDiagnostic,
    ProjectionEntity,
    ProjectionFailure,
    ProjectionFailureCode,
    ProjectionNamespaceState,
    ProjectionOutcome,
    ProjectionRelation,
    ProjectionRetrievalResult,
    ProjectionSourceRevision,
    RetrieveCommand,
    StableLocator,
)
from spine.memory.cognee.indexing import (
    CogneeVersionError,
    SUPPORTED_COGNEE_VERSION,
)
from spine.memory.namespace import NamespaceRoutingError, ProjectionNamespaceContext, ProjectionNamespaceRouter


class CogneeStructuredRetrievalSdk(Protocol):
    """Small public-SDK-shaped seam for deterministic retrieval tests."""

    version: str

    async def search(self, query: str, **kwargs: object) -> object: ...


class CogneePublicStructuredRetrievalSdk:
    """Bridge to public Cognee 1.5.4 ``search`` and ``SearchType`` only."""

    def __init__(self, sdk: Any | None = None) -> None:
        if sdk is None:
            from spine.memory.cognee_memory import configure_environment

            configure_environment()
            import cognee

            sdk = cognee
        self._sdk = sdk
        self.version = str(getattr(sdk, "__version__", ""))
        if self.version != SUPPORTED_COGNEE_VERSION:
            raise CogneeVersionError(
                f"Cognee {SUPPORTED_COGNEE_VERSION} is required, got {self.version or 'unknown'}"
            )

    async def search(self, query: str, **kwargs: object) -> object:
        query_type_name = kwargs.pop("query_type")
        search_type = getattr(self._sdk, "SearchType", None)
        query_type = getattr(search_type, str(query_type_name).upper(), None)
        if query_type is None:
            raise ValueError("public Cognee SearchType is unavailable")
        return await self._sdk.search(query, query_type=query_type, **kwargs)


class _RetrievalIssue(Exception):
    def __init__(self, code: ProjectionFailureCode) -> None:
        super().__init__(code.value)
        self.code = code


class CogneeStructuredRetrieval:
    """Implement the provider-neutral read side of ``ContextProjection``."""

    def __init__(
        self,
        *,
        sdk: CogneeStructuredRetrievalSdk | None = None,
        router: ProjectionNamespaceRouter | None = None,
    ) -> None:
        self._sdk = sdk or CogneePublicStructuredRetrievalSdk()
        if self._sdk.version != SUPPORTED_COGNEE_VERSION:
            raise CogneeVersionError(
                f"Cognee {SUPPORTED_COGNEE_VERSION} is required, got {self._sdk.version or 'unknown'}"
            )
        self._router = router or ProjectionNamespaceRouter()
        self._lock = asyncio.Lock()
        self._results: dict[tuple[UUID, UUID, str, str], ProjectionRetrievalResult] = {}
        self._digests: dict[tuple[UUID, UUID, str, str], str] = {}

    async def retrieve(self, command: RetrieveCommand) -> ProjectionRetrievalResult:
        """Search one already-resolved active namespace and map structured evidence."""

        async with self._lock:
            key = self._key(command)
            previous = self._results.get(key)
            if previous is not None:
                if self._digests[key] != command.command_digest:
                    return self._failure(command, ProjectionFailureCode.INTEGRITY_CONFLICT)
                return previous.model_copy(update={"outcome": ProjectionOutcome.REPLAYED})

            preflight = self._preflight(command)
            if preflight is not None:
                result = self._failure(command, preflight)
                self._remember(key, command.command_digest, result)
                return result

            try:
                chunks_response = await self._sdk.search(
                    command.query,
                    query_type="chunks",
                    datasets=[self.dataset_name(command)],
                    top_k=command.limit,
                    only_context=True,
                    verbose=True,
                    include_references=True,
                )
                hybrid_response = await self._sdk.search(
                    command.query,
                    query_type="hybrid_completion",
                    datasets=[self.dataset_name(command)],
                    top_k=command.limit,
                    only_context=True,
                    verbose=True,
                    include_references=True,
                )
                chunks, entities, relations, evidence = self._map_responses(
                    command, chunks_response, hybrid_response
                )
                if not chunks and not entities and not relations:
                    raise _RetrievalIssue(ProjectionFailureCode.MISSING_EVIDENCE)
                result = ProjectionRetrievalResult(
                    outcome=ProjectionOutcome.SUCCEEDED,
                    command_identity=command.command_identity,
                    command_digest=command.command_digest,
                    chunks=tuple(chunks),
                    entities=tuple(entities),
                    relations=tuple(relations),
                    evidence=tuple(evidence),
                )
            except _RetrievalIssue as error:
                result = self._failure(command, error.code)
            except ValidationError:
                result = self._failure(command, ProjectionFailureCode.INCOMPLETE_EVIDENCE)
            except Exception:
                # Provider exceptions can contain source text, credentials or
                # backend details. Never copy them into the contract result.
                result = self._failure(command, ProjectionFailureCode.PHYSICAL_FAILURE)
            self._remember(key, command.command_digest, result)
            return result

    @staticmethod
    def dataset_name(command: RetrieveCommand) -> str:
        """Derive the provider dataset only from the trusted routed namespace."""

        return (
            f"spine_{command.physical_namespace.projection_kind}_"
            f"{command.physical_namespace.namespace_digest}"
        )

    def _preflight(self, command: RetrieveCommand) -> ProjectionFailureCode | None:
        try:
            self._router.validate(
                command.physical_namespace,
                ProjectionNamespaceContext.from_command(command),
            )
        except (NamespaceRoutingError, ValueError):
            return ProjectionFailureCode.NAMESPACE_UNAVAILABLE
        if command.physical_namespace.state is not ProjectionNamespaceState.ACTIVE:
            return ProjectionFailureCode.NAMESPACE_UNAVAILABLE
        for source in command.source_revisions:
            if source.status != "current":
                return self._code_for_source_status(source.status)
        return None

    def _map_responses(
        self,
        command: RetrieveCommand,
        chunks_response: object,
        hybrid_response: object,
    ) -> tuple[list[ProjectionChunk], list[ProjectionEntity], list[ProjectionRelation], list[Any]]:
        sources = {source.source_revision_id: source for source in command.source_revisions}
        chunks: dict[str, ProjectionChunk] = {}
        entities: dict[str, ProjectionEntity] = {}
        relations: dict[str, ProjectionRelation] = {}
        evidence: dict[str, Any] = {}

        for item, wrapper in self._lane_items(chunks_response, "chunks"):
            chunk = self._map_chunk(command, sources, item, wrapper)
            self._merge(chunks, chunk.chunk_id, chunk)
            for reference in chunk.evidence:
                self._merge(evidence, reference.evidence_id, reference)
        for item, wrapper in self._lane_items(hybrid_response, "entities"):
            entity = self._map_entity(command, sources, item, wrapper)
            self._merge(entities, entity.entity_id, entity)
            for reference in entity.evidence:
                self._merge(evidence, reference.evidence_id, reference)
        for item, wrapper in self._lane_items(hybrid_response, "relations"):
            relation = self._map_relation(command, sources, item, wrapper)
            self._merge(relations, relation.relation_id, relation)
            for reference in relation.evidence:
                self._merge(evidence, reference.evidence_id, reference)

        return (
            sorted(chunks.values(), key=lambda item: item.chunk_id),
            sorted(entities.values(), key=lambda item: item.entity_id),
            sorted(relations.values(), key=lambda item: item.relation_id),
            sorted(evidence.values(), key=lambda item: item.evidence_id),
        )

    def _map_chunk(
        self,
        command: RetrieveCommand,
        sources: dict[UUID, ProjectionSourceRevision],
        item: object,
        wrapper: object,
    ) -> ProjectionChunk:
        value = self._unwrap(item, "chunk")
        text = self._text(value)
        identifier = self._identifier(value, ("chunk_id", "segment_id", "id")) or self._identifier(
            self._metadata(value), ("chunk_id", "segment_id", "id")
        )
        if not text or not identifier:
            raise _RetrievalIssue(ProjectionFailureCode.INCOMPLETE_EVIDENCE)
        references = self._references(command, sources, value, wrapper)
        return ProjectionChunk(chunk_id=identifier, text=text, evidence=references)

    def _map_entity(
        self,
        command: RetrieveCommand,
        sources: dict[UUID, ProjectionSourceRevision],
        item: object,
        wrapper: object,
    ) -> ProjectionEntity:
        value = self._unwrap(item, "entity")
        metadata = self._metadata(value)
        identifier = self._identifier(value, ("entity_id", "node_id", "artifact_id", "id")) or self._identifier(
            metadata, ("entity_id", "node_id", "artifact_id", "id")
        )
        name = self._text_value(value, ("name", "canonical_name", "node_name", "entity"))
        kind = self._text_value(value, ("kind", "entity_type", "node_type", "type"))
        if not identifier or not name or not kind:
            raise _RetrievalIssue(ProjectionFailureCode.INCOMPLETE_EVIDENCE)
        return ProjectionEntity(
            entity_id=identifier,
            kind=kind,
            name=name,
            evidence=self._references(command, sources, value, wrapper),
        )

    def _map_relation(
        self,
        command: RetrieveCommand,
        sources: dict[UUID, ProjectionSourceRevision],
        item: object,
        wrapper: object,
    ) -> ProjectionRelation:
        value = self._unwrap(item, "relation")
        metadata = self._metadata(value)
        identifier = self._identifier(value, ("relation_id", "edge_id", "artifact_id", "id")) or self._identifier(
            metadata, ("relation_id", "edge_id", "artifact_id", "id")
        )
        kind = self._text_value(value, ("kind", "relation_type", "predicate", "type"))
        subject = self._endpoint(value, ("subject_entity_id", "subject", "source", "from"))
        target = self._endpoint(value, ("object_entity_id", "object", "target", "to"))
        if not identifier or not kind or not subject or not target:
            raise _RetrievalIssue(ProjectionFailureCode.INCOMPLETE_EVIDENCE)
        return ProjectionRelation(
            relation_id=identifier,
            kind=kind,
            subject_entity_id=subject,
            object_entity_id=target,
            evidence=self._references(command, sources, value, wrapper),
        )

    def _references(
        self,
        command: RetrieveCommand,
        sources: dict[UUID, ProjectionSourceRevision],
        item: object,
        wrapper: object,
    ) -> tuple[EvidenceReference, ...]:
        metadata = self._metadata(item)
        raw_references = self._first_sequence(item, ("evidence", "references", "source_references"))
        if not raw_references:
            raw_references = self._first_sequence(metadata, ("evidence", "references"))
        if not raw_references:
            raw_references = self._first_sequence(wrapper, ("evidence", "references"))
        if not raw_references and self._value(metadata, "source_revision_id") is not None:
            raw_references = (metadata,)
        if not raw_references:
            raise _RetrievalIssue(ProjectionFailureCode.MISSING_EVIDENCE)

        result = []
        for raw in raw_references:
            reference = self._mapping(raw)
            merged = dict(metadata)
            merged.update(reference)
            source_revision_id = self._uuid_value(merged, "source_revision_id")
            if source_revision_id is None or source_revision_id not in sources:
                raise _RetrievalIssue(ProjectionFailureCode.MISSING_EVIDENCE)
            source = sources[source_revision_id]
            self._validate_scope(command, source, merged)
            locator = self._locator(merged.get("locator"))
            if locator is None:
                raise _RetrievalIssue(ProjectionFailureCode.INCOMPLETE_EVIDENCE)
            status = str(merged.get("status", merged.get("quality", "valid")))
            if status in {"accepted", "ok"}:
                status = "valid"
            if status != "valid":
                raise _RetrievalIssue(self._code_for_evidence_status(status))
            evidence_id = self._text_value(merged, ("evidence_id", "id"))
            if not evidence_id:
                raise _RetrievalIssue(ProjectionFailureCode.INCOMPLETE_EVIDENCE)
            digest = self._text_value(merged, ("evidence_digest", "digest"))
            if not digest:
                raise _RetrievalIssue(ProjectionFailureCode.INCOMPLETE_EVIDENCE)
            result.append(
                EvidenceReference(
                    evidence_id=evidence_id,
                    workspace_id=command.workspace_id,
                    environment_id=command.environment_id,
                    source_object_id=source.source_object_id,
                    source_revision_id=source.source_revision_id,
                    revision_digest=source.revision_digest,
                    revision_number=source.revision_number,
                    processing_receipt_id=source.processing_receipt_id,
                    locator_contract_version=source.locator_contract_version,
                    locator=locator,
                    evidence_digest=digest,
                )
            )
        if not result:
            raise _RetrievalIssue(ProjectionFailureCode.MISSING_EVIDENCE)
        return tuple(result)

    @staticmethod
    def _validate_scope(
        command: RetrieveCommand,
        source: ProjectionSourceRevision,
        metadata: Mapping[str, object],
    ) -> None:
        required = (
            "workspace_id",
            "environment_id",
            "source_object_id",
            "source_revision_id",
            "revision_digest",
            "revision_number",
        )
        if any(metadata.get(field) is None for field in required):
            raise _RetrievalIssue(ProjectionFailureCode.INCOMPLETE_EVIDENCE)
        for field, expected in (
            ("workspace_id", command.workspace_id),
            ("environment_id", command.environment_id),
            ("source_object_id", source.source_object_id),
            ("source_revision_id", source.source_revision_id),
        ):
            actual = metadata.get(field)
            if actual is not None:
                try:
                    if UUID(str(actual)) != expected:
                        raise _RetrievalIssue(ProjectionFailureCode.MISSING_EVIDENCE)
                except ValueError as error:
                    raise _RetrievalIssue(ProjectionFailureCode.MISSING_EVIDENCE) from error
        if metadata.get("revision_digest") not in (None, source.revision_digest):
            raise _RetrievalIssue(ProjectionFailureCode.STALE_EVIDENCE)
        if metadata.get("revision_number") not in (None, source.revision_number):
            raise _RetrievalIssue(ProjectionFailureCode.STALE_EVIDENCE)
        if source.processing_receipt_id is not None:
            receipt = metadata.get("processing_receipt_id")
            if receipt is None:
                raise _RetrievalIssue(ProjectionFailureCode.INCOMPLETE_EVIDENCE)
            try:
                if UUID(str(receipt)) != source.processing_receipt_id:
                    raise _RetrievalIssue(ProjectionFailureCode.MISSING_EVIDENCE)
            except ValueError as error:
                raise _RetrievalIssue(ProjectionFailureCode.INCOMPLETE_EVIDENCE) from error

    @staticmethod
    def _locator(value: object) -> StableLocator | None:
        mapping = CogneeStructuredRetrieval._mapping(value)
        if not mapping:
            return None
        kind = str(mapping.get("kind", "custom"))
        if "value" in mapping:
            return StableLocator(kind=CogneeStructuredRetrieval._locator_kind(kind), value=str(mapping["value"]))
        if kind in {"pdf_page", "page"} and mapping.get("page") is not None:
            return StableLocator(kind="page", value=str(mapping["page"]))
        if kind in {"text_range", "character_range"} and mapping.get("start") is not None and mapping.get("end") is not None:
            return StableLocator(kind="character_range", value=f"{mapping['start']}-{mapping['end']}")
        if kind in {"xlsx_row", "sheet_row"} and mapping.get("sheet") and mapping.get("row") is not None:
            return StableLocator(kind="sheet_row", value=f"{mapping['sheet']}:{mapping['row']}")
        if kind == "message" and mapping.get("message_id"):
            return StableLocator(kind="message", value=str(mapping["message_id"]))
        if kind == "physical_line" and mapping.get("line_number") is not None:
            return StableLocator(
                kind="character_range",
                value=f"line:{mapping['line_number']}:{mapping.get('start_codepoint', 0)}-{mapping.get('end_codepoint', 0)}",
            )
        return StableLocator(
            kind="custom",
            value=json.dumps(mapping, sort_keys=True, separators=(",", ":")),
        )

    @staticmethod
    def _locator_kind(kind: str) -> str:
        return {
            "pdf_page": "page",
            "text_range": "character_range",
            "xlsx_row": "sheet_row",
            "physical_line": "character_range",
        }.get(kind, kind if kind in {"page", "sheet_row", "message", "character_range", "record_field", "custom"} else "custom")

    @staticmethod
    def _mapping(value: object) -> dict[str, object]:
        if isinstance(value, Mapping):
            return dict(value)
        if hasattr(value, "model_dump"):
            dumped = value.model_dump(mode="json")  # type: ignore[attr-defined]
            return dict(dumped) if isinstance(dumped, Mapping) else {}
        return {}

    @staticmethod
    def _value(value: object, key: str) -> object | None:
        mapping = CogneeStructuredRetrieval._mapping(value)
        if key in mapping:
            return mapping[key]
        return getattr(value, key, None)

    @staticmethod
    def _metadata(value: object) -> dict[str, object]:
        for key in ("metadata", "external_metadata", "source_metadata"):
            candidate = CogneeStructuredRetrieval._value(value, key)
            mapping = CogneeStructuredRetrieval._mapping(candidate)
            if mapping:
                return mapping
        return {}

    @staticmethod
    def _first_sequence(value: object, keys: tuple[str, ...]) -> tuple[object, ...]:
        for key in keys:
            candidate = CogneeStructuredRetrieval._value(value, key)
            if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
                return tuple(candidate)
        return ()

    @staticmethod
    def _lane_items(response: object, lane: str) -> tuple[tuple[object, object], ...]:
        wrappers = response if isinstance(response, Sequence) and not isinstance(response, (str, bytes, bytearray)) else (response,)
        output: list[tuple[object, object]] = []
        lane_keys = {
            "chunks": ("chunks",),
            "entities": ("entities", "nodes"),
            "relations": ("relations", "facts", "triplets", "edges"),
        }[lane]
        for wrapper in wrappers:
            objects = CogneeStructuredRetrieval._value(wrapper, "objects_result")
            if objects is None:
                objects = CogneeStructuredRetrieval._value(wrapper, "objectsResult")
            if objects is None:
                objects = CogneeStructuredRetrieval._value(wrapper, "objects")
            selected = CogneeStructuredRetrieval._first_sequence(objects, lane_keys)
            if selected:
                output.extend((item, wrapper) for item in selected)
            elif lane == "chunks" and isinstance(objects, Sequence) and not isinstance(objects, (str, bytes, bytearray)):
                output.extend((item, wrapper) for item in objects)
            elif lane != "chunks" and isinstance(objects, Mapping) and any(
                CogneeStructuredRetrieval._value(objects, key) is not None for key in lane_keys
            ):
                continue
        return tuple(output)

    @staticmethod
    def _unwrap(value: object, key: str) -> object:
        nested = CogneeStructuredRetrieval._value(value, key)
        return nested if nested is not None else value

    @staticmethod
    def _text(value: object) -> str | None:
        return CogneeStructuredRetrieval._text_value(value, ("text", "content", "chunk_text", "data"))

    @staticmethod
    def _text_value(value: object, keys: tuple[str, ...]) -> str | None:
        for key in keys:
            candidate = CogneeStructuredRetrieval._value(value, key)
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    @staticmethod
    def _identifier(value: object, keys: tuple[str, ...]) -> str | None:
        candidate = CogneeStructuredRetrieval._text_value(value, keys)
        if candidate:
            return candidate
        return None

    @staticmethod
    def _endpoint(value: object, keys: tuple[str, ...]) -> str | None:
        for key in keys:
            candidate = CogneeStructuredRetrieval._value(value, key)
            if isinstance(candidate, str) and candidate:
                return candidate
            identifier = CogneeStructuredRetrieval._identifier(candidate, ("id", "entity_id", "node_id", "name"))
            if identifier:
                return identifier
        return None

    @staticmethod
    def _uuid_value(value: object, key: str) -> UUID | None:
        candidate = CogneeStructuredRetrieval._value(value, key)
        if candidate is None:
            return None
        try:
            return UUID(str(candidate))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _merge(target: dict[str, object], key: str, value: object) -> None:
        existing = target.get(key)
        if existing is not None and existing != value:
            raise _RetrievalIssue(ProjectionFailureCode.INCOMPLETE_EVIDENCE)
        target[key] = value

    @staticmethod
    def _key(command: RetrieveCommand) -> tuple[UUID, UUID, str, str]:
        return (
            command.workspace_id,
            command.environment_id,
            command.physical_namespace.namespace_digest,
            command.command_identity,
        )

    def _remember(self, key, digest: str, result: ProjectionRetrievalResult) -> None:
        self._digests[key] = digest
        self._results[key] = result

    @staticmethod
    def _code_for_source_status(status: str) -> ProjectionFailureCode:
        return _EVIDENCE_STATUS_CODES[status]

    @staticmethod
    def _code_for_evidence_status(status: str) -> ProjectionFailureCode:
        return _EVIDENCE_STATUS_CODES.get(status, ProjectionFailureCode.INCOMPLETE_EVIDENCE)

    @staticmethod
    def _failure(command: RetrieveCommand, code: ProjectionFailureCode) -> ProjectionRetrievalResult:
        outcome = {
            ProjectionFailureCode.MISSING_EVIDENCE: ProjectionOutcome.MISSING,
            ProjectionFailureCode.STALE_EVIDENCE: ProjectionOutcome.STALE,
            ProjectionFailureCode.TOMBSTONED_SOURCE: ProjectionOutcome.TOMBSTONED,
            ProjectionFailureCode.AMBIGUOUS_EVIDENCE: ProjectionOutcome.AMBIGUOUS,
            ProjectionFailureCode.INCOMPLETE_EVIDENCE: ProjectionOutcome.INCOMPLETE,
        }.get(code, ProjectionOutcome.FAILED)
        return ProjectionRetrievalResult(
            outcome=outcome,
            command_identity=command.command_identity,
            command_digest=command.command_digest,
            failure=ProjectionFailure(
                code=code,
                retryable=code is ProjectionFailureCode.PHYSICAL_FAILURE,
                safe_detail=ProjectionDiagnostic(
                    namespace_digest=(
                        command.physical_namespace.namespace_digest
                        if code is ProjectionFailureCode.NAMESPACE_UNAVAILABLE
                        else None
                    )
                ),
            ),
        )


__all__ = [
    "CogneePublicStructuredRetrievalSdk",
    "CogneeStructuredRetrieval",
    "CogneeStructuredRetrievalSdk",
]


_EVIDENCE_STATUS_CODES = {
    "missing": ProjectionFailureCode.MISSING_EVIDENCE,
    "stale": ProjectionFailureCode.STALE_EVIDENCE,
    "tombstoned": ProjectionFailureCode.TOMBSTONED_SOURCE,
    "ambiguous": ProjectionFailureCode.AMBIGUOUS_EVIDENCE,
    "incomplete": ProjectionFailureCode.INCOMPLETE_EVIDENCE,
}
