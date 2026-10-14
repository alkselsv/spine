"""Opt-in qualification helpers for the audited public Cognee SDK.

This module deliberately treats Cognee as an opaque SDK object.  The offline
checks validate the public callable surface and the JSON result shapes used by
the smoke matrix; the live runner is separate because it needs configured
storage and an LLM provider.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import inspect
import json
from typing import Any


AUDITED_COGNEE_VERSION = "1.5.4"


class SmokeStatus(str, Enum):
    SUPPORTED = "supported"
    NO_GO = "no-go"


class SearchLane(str, Enum):
    CHUNKS = "chunks"
    GRAPH = "graph"
    HYBRID = "hybrid"


class BackendApproval(str, Enum):
    CAPABILITY_ONLY = "capability-only"
    NOT_APPROVED = "not-approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ApiCheck:
    """One public SDK callable and the parameters Spine relies on."""

    name: str
    public_path: str
    status: SmokeStatus
    required_parameters: tuple[str, ...]
    missing_parameters: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApiMatrix:
    """Static public-API qualification output."""

    version: str
    status: SmokeStatus
    checks: tuple[ApiCheck, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status.value,
            "checks": [
                {
                    "name": check.name,
                    "public_path": check.public_path,
                    "status": check.status.value,
                    "required_parameters": list(check.required_parameters),
                    "missing_parameters": list(check.missing_parameters),
                    "notes": list(check.notes),
                }
                for check in self.checks
            ],
        }


@dataclass(frozen=True)
class SearchObservation:
    """The structured fields observed from one verbose public search call."""

    lane: SearchLane
    status: SmokeStatus
    item_count: int
    observed_shapes: tuple[str, ...]
    missing_shapes: tuple[str, ...]
    evidence_kinds: tuple[str, ...]
    missing_evidence_kinds: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane.value,
            "status": self.status.value,
            "item_count": self.item_count,
            "observed_shapes": list(self.observed_shapes),
            "missing_shapes": list(self.missing_shapes),
            "evidence_kinds": list(self.evidence_kinds),
            "missing_evidence_kinds": list(self.missing_evidence_kinds),
        }


@dataclass(frozen=True)
class BackendProfile:
    """A physical backend profile and its qualification decision."""

    name: str
    relational: str
    graph: str
    vector: str
    approval: BackendApproval
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "relational": self.relational,
            "graph": self.graph,
            "vector": self.vector,
            "approval": self.approval.value,
            "reason": self.reason,
        }


_API_REQUIREMENTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("add", "add", ("data", "dataset_name", "node_set")),
    ("cognify", "cognify", ("datasets", "dry_run", "raise_on_error")),
    ("update", "update", ("data_id", "data", "dataset_id", "chunk_level_diff")),
    ("delete", "delete", ("data_id", "dataset_id", "mode")),
    (
        "datasets.delete_data",
        "datasets.delete_data",
        ("dataset_id", "data_id", "mode"),
    ),
    ("datasets.list_datasets", "datasets.list_datasets", ()),
    ("datasets.list_data", "datasets.list_data", ("dataset_id",)),
    (
        "forget",
        "forget",
        ("data_id", "dataset", "dataset_id", "everything", "memory_only"),
    ),
    (
        "search",
        "search",
        (
            "query_text",
            "query_type",
            "datasets",
            "dataset_ids",
            "top_k",
            "only_context",
            "verbose",
            "include_references",
        ),
    ),
)


def _resolve_public_path(sdk: object, public_path: str) -> object | None:
    current: object = sdk
    for part in public_path.split("."):
        current = getattr(current, part, None)
        if current is None:
            return None
    return current


def build_api_matrix(sdk: object) -> ApiMatrix:
    """Inspect only public SDK attributes used by the ContextProjection seam."""

    version = str(getattr(sdk, "__version__", "unknown"))
    checks: list[ApiCheck] = []
    for name, public_path, required_parameters in _API_REQUIREMENTS:
        callable_object = _resolve_public_path(sdk, public_path)
        if not callable(callable_object):
            checks.append(
                ApiCheck(
                    name=name,
                    public_path=public_path,
                    status=SmokeStatus.NO_GO,
                    required_parameters=required_parameters,
                    missing_parameters=required_parameters,
                    notes=("public callable is missing",),
                )
            )
            continue

        try:
            parameters = set(inspect.signature(callable_object).parameters)
        except (TypeError, ValueError):
            checks.append(
                ApiCheck(
                    name=name,
                    public_path=public_path,
                    status=SmokeStatus.NO_GO,
                    required_parameters=required_parameters,
                    missing_parameters=required_parameters,
                    notes=("public callable has no inspectable signature",),
                )
            )
            continue

        missing = tuple(parameter for parameter in required_parameters if parameter not in parameters)
        notes: tuple[str, ...] = ()
        if name == "delete":
            notes = ("deprecated public compatibility API; prefer datasets.delete_data",)
        checks.append(
            ApiCheck(
                name=name,
                public_path=public_path,
                status=SmokeStatus.SUPPORTED if not missing else SmokeStatus.NO_GO,
                required_parameters=required_parameters,
                missing_parameters=missing,
                notes=notes,
            )
        )

    status = (
        SmokeStatus.SUPPORTED
        if version == AUDITED_COGNEE_VERSION
        and all(check.status is SmokeStatus.SUPPORTED for check in checks)
        else SmokeStatus.NO_GO
    )
    return ApiMatrix(version=version, status=status, checks=tuple(checks))


def _as_mapping(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")  # type: ignore[attr-defined]
        return dumped if isinstance(dumped, Mapping) else None
    return None


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _search_objects(response: object) -> object:
    mapping = _as_mapping(response)
    if mapping is None:
        return None
    return mapping.get("objects_result", mapping.get("objectsResult"))


def _evidence_kinds(responses: Sequence[object]) -> tuple[str, ...]:
    kinds: set[str] = set()
    for response in responses:
        mapping = _as_mapping(response)
        if mapping is None:
            continue
        for reference in _sequence(mapping.get("evidence", ())):
            evidence = _as_mapping(reference)
            if evidence is not None and isinstance(evidence.get("kind"), str):
                kinds.add(evidence["kind"])
    return tuple(sorted(kinds))


def _chunk_document_ids(responses: Sequence[object]) -> tuple[str, ...]:
    identifiers: set[str] = set()
    for response in responses:
        for item in _sequence(_search_objects(response)):
            mapping = _as_mapping(item)
            if mapping is None:
                continue
            payload = _as_mapping(mapping.get("payload")) or mapping
            value = payload.get("document_id", payload.get("data_id"))
            if value is not None:
                identifiers.add(str(value))
    return tuple(sorted(identifiers))


def inspect_search_payload(
    lane: SearchLane,
    responses: object,
    *,
    dataset_data_ids: Sequence[str] = (),
) -> SearchObservation:
    """Inspect verbose JSON returned by public ``cognee.search``.

    The function does not depend on Cognee result classes.  It accepts the
    serialized shape returned by ``verbose=True`` so contract tests can run
    without databases, models, or private Cognee imports.
    """

    response_items = tuple(_sequence(responses))
    observed: set[str] = set()
    if lane is SearchLane.CHUNKS:
        if any(_sequence(_search_objects(item)) for item in response_items):
            observed.add("chunk")
        required = ("chunk",)
        required_evidence = ("segment",)
    elif lane is SearchLane.GRAPH:
        if any(_sequence(_search_objects(item)) for item in response_items):
            observed.add("relation")
        required = ("relation",)
        required_evidence = ("graph_edge",)
    else:
        for item in response_items:
            objects = _as_mapping(_search_objects(item))
            if objects is None:
                continue
            if _sequence(objects.get("chunks")):
                observed.add("chunk")
            if _sequence(objects.get("entities")):
                observed.add("entity")
            if _sequence(objects.get("facts", objects.get("relations", objects.get("triplets")))):
                observed.add("relation")
        required = ("chunk", "entity", "relation")
        # The ContextProjection adapter does not trust provider-generated
        # hybrid lineage. Typed Spine graph artifacts are read through the
        # CHUNKS lane and joined to public dataset metadata instead.
        required_evidence = ()

    evidence_kind_set = set(_evidence_kinds(response_items))
    if lane is SearchLane.CHUNKS:
        document_ids = set(_chunk_document_ids(response_items))
        public_data_ids = {str(value) for value in dataset_data_ids}
        if document_ids and document_ids <= public_data_ids:
            evidence_kind_set.add("dataset_metadata")
    evidence_kinds = tuple(sorted(evidence_kind_set))
    missing_shapes = tuple(shape for shape in required if shape not in observed)
    missing_evidence = tuple(
        kind
        for kind in required_evidence
        if kind not in evidence_kinds
        and not (kind == "segment" and "dataset_metadata" in evidence_kinds)
    )
    status = (
        SmokeStatus.SUPPORTED
        if response_items and not missing_shapes and not missing_evidence
        else SmokeStatus.NO_GO
    )
    return SearchObservation(
        lane=lane,
        status=status,
        item_count=len(response_items),
        observed_shapes=tuple(shape for shape in required if shape in observed),
        missing_shapes=missing_shapes,
        evidence_kinds=evidence_kinds,
        missing_evidence_kinds=missing_evidence,
    )


def build_backend_matrix() -> tuple[BackendProfile, ...]:
    """Return the bounded backend decision used by the qualification report."""

    return (
        BackendProfile(
            name="local",
            relational="sqlite",
            graph="ladybug-or-kuzu",
            vector="lancedb",
            approval=BackendApproval.CAPABILITY_ONLY,
            reason="Useful for local API-shape and smoke capability checks; not a production approval.",
        ),
        BackendProfile(
            name="production_candidate",
            relational="postgresql",
            graph="neo4j",
            vector="pgvector",
            approval=BackendApproval.NOT_APPROVED,
            reason="Candidate only until isolation, provenance, deletion, and retrieval gates pass.",
        ),
        BackendProfile(
            name="postgres_demo",
            relational="postgresql",
            graph="postgres_demo",
            vector="pgvector",
            approval=BackendApproval.REJECTED,
            reason="Cognee 1.5.4 marks the postgres_demo graph backend as not production-ready.",
        ),
    )


def render_report(*, api: ApiMatrix, searches: Sequence[SearchObservation] = ()) -> str:
    """Serialize qualification output without including source content."""

    payload = {
        "audited_version": AUDITED_COGNEE_VERSION,
        "api": api.as_dict(),
        "search": [observation.as_dict() for observation in searches],
        "backends": [profile.as_dict() for profile in build_backend_matrix()],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


__all__ = [
    "AUDITED_COGNEE_VERSION",
    "ApiCheck",
    "ApiMatrix",
    "BackendApproval",
    "BackendProfile",
    "SearchLane",
    "SearchObservation",
    "SmokeStatus",
    "build_api_matrix",
    "build_backend_matrix",
    "inspect_search_payload",
    "render_report",
]
