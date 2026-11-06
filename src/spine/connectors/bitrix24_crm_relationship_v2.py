"""Read exact CRM activity/entity binding evidence through a narrow port.

This adapter is deliberately offline-first: callers provide an exact activity
ID and synthetic Operations bounds, while an injected page port supplies
deterministic responses. It has no generic REST dispatch or legacy fallback.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Literal, Protocol

from pydantic import ConfigDict, Field, model_validator

from spine.application.crm_relationship_v2_contracts import (
    Bitrix24CrmRelationshipV2ProviderPort,
    Bitrix24CrmRelationshipV2ReadCommand,
    Bitrix24CrmRelationshipV2ReadResult,
    CrmRelationshipBindingOperationsProfile,
    CrmRelationshipBindingOutcome,
)
from spine.domain.common import DefinitionModel

BINDING_LIST_METHOD = "crm.activity.binding.list"
ENTITY_TYPE_NORMALIZER_ID = "crm.activity.binding.list:entityTypeId"
ENTITY_TYPE_NORMALIZER_VERSION = "v1"


class CrmBindingPageStatus(str, Enum):
    SUCCESS = "success"
    TRANSIENT_FAILURE = "transient_failure"
    INACCESSIBLE = "inaccessible"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"
    MALFORMED = "malformed"


class CrmActivityEntityPair(DefinitionModel):
    """Raw values returned by crm.activity.binding.list."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_type_id: str = Field(min_length=1, max_length=128)
    entity_id: str = Field(min_length=1, max_length=256)


class CrmActivityBindingPage(DefinitionModel):
    """One typed response page; raw response bodies never cross this seam."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CrmBindingPageStatus
    items: tuple[CrmActivityEntityPair, ...] = ()
    response_bytes: int = Field(ge=0)
    next_start: int | None = Field(default=None, ge=0)
    enumeration_complete: bool
    permission_filtered: bool = False

    @model_validator(mode="after")
    def validate_page(self) -> "CrmActivityBindingPage":
        if self.status is not CrmBindingPageStatus.SUCCESS and self.items:
            raise ValueError("unsuccessful binding pages cannot carry items")
        if self.next_start is not None and self.enumeration_complete:
            raise ValueError("a page with a next cursor cannot complete enumeration")
        return self


class CrmActivityBindingListRequest(DefinitionModel):
    """The only operation this connector can request from its transport."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Literal["crm.activity.binding.list"] = BINDING_LIST_METHOD
    activity_id: str = Field(min_length=1, max_length=256)
    start: int = Field(ge=0)
    limit: int = Field(ge=1)
    max_response_bytes: int = Field(ge=1)


class CrmActivityBindingPagePort(Protocol):
    async def list_activity_bindings(
        self,
        request: CrmActivityBindingListRequest,
        *,
        admitted_command: Bitrix24CrmRelationshipV2ReadCommand,
    ) -> CrmActivityBindingPage: ...


class Bitrix24CrmActivityBindingReader(Bitrix24CrmRelationshipV2ProviderPort):
    """Observe one exact raw entity pair with caller-supplied bounded paging."""

    def __init__(self, pages: CrmActivityBindingPagePort) -> None:
        self._pages = pages

    async def read(
        self, command: Bitrix24CrmRelationshipV2ReadCommand
    ) -> Bitrix24CrmRelationshipV2ReadResult:
        profile = command.mapping_profile
        bounds = command.operations_profile
        normalizer_id = profile.normalizer_id
        normalizer_version = profile.normalizer_version
        attempts = 0
        total_bytes = 0
        if (
            BINDING_LIST_METHOD not in profile.read_methods
            or normalizer_id != ENTITY_TYPE_NORMALIZER_ID
            or normalizer_version != ENTITY_TYPE_NORMALIZER_VERSION
            or command.raw_entity_type_id not in profile.raw_entity_type_ids
            or command.entity_namespace != profile.entity_namespace
        ):
            return self._result(
                command,
                CrmRelationshipBindingOutcome.NOT_VERIFIED,
                page_count=0,
                attempt_count=attempts,
                response_bytes=total_bytes,
                normalizer_id=normalizer_id,
                normalizer_version=normalizer_version,
            )

        evidence_pages: list[
            tuple[int, int, tuple[tuple[str, str], ...], int | None, bool]
        ] = []
        seen_pairs: set[tuple[str, str]] = set()
        start = 0
        successful_pages = 0

        for _ in range(bounds.max_pages):
            page: CrmActivityBindingPage | None = None
            while attempts < bounds.max_attempts:
                attempts += 1
                try:
                    page = await self._pages.list_activity_bindings(
                        CrmActivityBindingListRequest(
                            activity_id=command.activity_id,
                            start=start,
                            limit=bounds.page_size,
                            max_response_bytes=bounds.max_response_bytes - total_bytes,
                        ),
                        admitted_command=command,
                    )
                except Exception:
                    page = None
                if page is None:
                    continue
                if not isinstance(page, CrmActivityBindingPage):
                    break
                if page.status is CrmBindingPageStatus.TRANSIENT_FAILURE:
                    continue
                break

            if page is None:
                return self._result(
                    command,
                    CrmRelationshipBindingOutcome.AUTHORITY_UNAVAILABLE,
                    page_count=successful_pages,
                    attempt_count=attempts,
                    response_bytes=total_bytes,
                    evidence_pages=evidence_pages,
                    normalizer_id=normalizer_id,
                    normalizer_version=normalizer_version,
                )
            if not isinstance(page, CrmActivityBindingPage):
                return self._result(
                    command,
                    CrmRelationshipBindingOutcome.NOT_VERIFIED,
                    page_count=successful_pages,
                    attempt_count=attempts,
                    response_bytes=total_bytes,
                    evidence_pages=evidence_pages,
                    normalizer_id=normalizer_id,
                    normalizer_version=normalizer_version,
                )
            if page.status is CrmBindingPageStatus.AUTHORITY_UNAVAILABLE:
                return self._result(
                    command,
                    CrmRelationshipBindingOutcome.AUTHORITY_UNAVAILABLE,
                    page_count=successful_pages,
                    attempt_count=attempts,
                    response_bytes=total_bytes,
                    evidence_pages=evidence_pages,
                    normalizer_id=normalizer_id,
                    normalizer_version=normalizer_version,
                )
            if page.status is CrmBindingPageStatus.INACCESSIBLE:
                return self._result(
                    command,
                    CrmRelationshipBindingOutcome.INACCESSIBLE,
                    page_count=successful_pages,
                    attempt_count=attempts,
                    response_bytes=total_bytes,
                    evidence_pages=evidence_pages,
                    normalizer_id=normalizer_id,
                    normalizer_version=normalizer_version,
                )
            if page.status is not CrmBindingPageStatus.SUCCESS:
                return self._result(
                    command,
                    CrmRelationshipBindingOutcome.NOT_VERIFIED,
                    page_count=successful_pages,
                    attempt_count=attempts,
                    response_bytes=total_bytes,
                    evidence_pages=evidence_pages,
                    normalizer_id=normalizer_id,
                    normalizer_version=normalizer_version,
                )
            if page.response_bytes <= 0 or total_bytes + page.response_bytes > bounds.max_response_bytes:
                return self._result(
                    command,
                    CrmRelationshipBindingOutcome.NOT_VERIFIED,
                    page_count=successful_pages,
                    attempt_count=attempts,
                    response_bytes=total_bytes,
                    evidence_pages=evidence_pages,
                    normalizer_id=normalizer_id,
                    normalizer_version=normalizer_version,
                )
            if len(page.items) > bounds.page_size or (
                page.next_start is not None
                and (
                    page.next_start <= start
                    or page.next_start > bounds.max_pages * bounds.page_size
                )
            ):
                return self._result(
                    command,
                    CrmRelationshipBindingOutcome.NOT_VERIFIED,
                    page_count=successful_pages,
                    attempt_count=attempts,
                    response_bytes=total_bytes,
                    evidence_pages=evidence_pages,
                    normalizer_id=normalizer_id,
                    normalizer_version=normalizer_version,
                )

            pairs = tuple((item.entity_type_id, item.entity_id) for item in page.items)
            if len(set(pairs)) != len(pairs) or seen_pairs.intersection(pairs):
                return self._result(
                    command,
                    CrmRelationshipBindingOutcome.NOT_VERIFIED,
                    page_count=successful_pages,
                    attempt_count=attempts,
                    response_bytes=total_bytes,
                    evidence_pages=evidence_pages,
                    normalizer_id=normalizer_id,
                    normalizer_version=normalizer_version,
                )
            seen_pairs.update(pairs)
            total_bytes += page.response_bytes
            successful_pages += 1
            evidence_pages.append(
                (
                    start,
                    page.response_bytes,
                    pairs,
                    page.next_start,
                    page.enumeration_complete,
                )
            )

            if (command.raw_entity_type_id, command.entity_id) in pairs:
                return self._result(
                    command,
                    CrmRelationshipBindingOutcome.POSITIVE_OBSERVED,
                    page_count=successful_pages,
                    attempt_count=attempts,
                    response_bytes=total_bytes,
                    evidence_pages=evidence_pages,
                    normalizer_id=normalizer_id,
                    normalizer_version=normalizer_version,
                )
            if page.permission_filtered:
                return self._result(
                    command,
                    CrmRelationshipBindingOutcome.INACCESSIBLE,
                    page_count=successful_pages,
                    attempt_count=attempts,
                    response_bytes=total_bytes,
                    evidence_pages=evidence_pages,
                    normalizer_id=normalizer_id,
                    normalizer_version=normalizer_version,
                )
            if page.next_start is None:
                return self._result(
                    command,
                    CrmRelationshipBindingOutcome.NOT_VERIFIED
                    if page.enumeration_complete
                    else CrmRelationshipBindingOutcome.AUTHORITY_UNAVAILABLE,
                    page_count=successful_pages,
                    attempt_count=attempts,
                    response_bytes=total_bytes,
                    evidence_pages=evidence_pages,
                    normalizer_id=normalizer_id,
                    normalizer_version=normalizer_version,
                )
            start = page.next_start

        return self._result(
            command,
            CrmRelationshipBindingOutcome.NOT_VERIFIED,
            page_count=successful_pages,
            attempt_count=attempts,
            response_bytes=total_bytes,
            evidence_pages=evidence_pages,
            normalizer_id=normalizer_id,
            normalizer_version=normalizer_version,
        )

    @staticmethod
    def _result(
        command: Bitrix24CrmRelationshipV2ReadCommand,
        outcome: CrmRelationshipBindingOutcome,
        *,
        page_count: int,
        evidence_pages: list[
            tuple[int, int, tuple[tuple[str, str], ...], int | None, bool]
        ]
        | None = None,
        normalizer_id: str,
        normalizer_version: str,
        attempt_count: int,
        response_bytes: int,
    ) -> Bitrix24CrmRelationshipV2ReadResult:
        # Hash only the bounded, allowlisted evidence shape. The reference does
        # not embed raw response text or provider identifiers.
        material = {
            "method": BINDING_LIST_METHOD,
            "activity_digest": hashlib.sha256(command.activity_id.encode()).hexdigest(),
            "target_digest": hashlib.sha256(
                f"{command.raw_entity_type_id}\0{command.entity_id}".encode()
            ).hexdigest(),
            "profile_id": str(command.mapping_profile.profile_id),
            "profile_version": command.mapping_profile.profile_version,
            "normalizer_id": normalizer_id,
            "normalizer_version": normalizer_version,
            "outcome": outcome.value,
            "pages": evidence_pages or [],
        }
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return Bitrix24CrmRelationshipV2ReadResult(
            binding_evidence_reference=f"binding-observation:{digest}",
            binding_evidence_digest=digest,
            positive_pair_observed=outcome is CrmRelationshipBindingOutcome.POSITIVE_OBSERVED,
            outcome=outcome,
            binding_method=BINDING_LIST_METHOD,
            raw_entity_type_id=command.raw_entity_type_id,
            entity_id=command.entity_id,
            normalizer_id=normalizer_id,
            normalizer_version=normalizer_version,
            page_count=page_count,
            attempt_count=attempt_count,
            response_bytes=response_bytes,
            snapshot_atomic=False,
        )


__all__ = [
    "BINDING_LIST_METHOD",
    "ENTITY_TYPE_NORMALIZER_ID",
    "ENTITY_TYPE_NORMALIZER_VERSION",
    "Bitrix24CrmActivityBindingReader",
    "Bitrix24CrmRelationshipV2ProviderPort",
    "Bitrix24CrmRelationshipV2ReadCommand",
    "Bitrix24CrmRelationshipV2ReadResult",
    "CrmActivityBindingListRequest",
    "CrmActivityBindingPage",
    "CrmActivityBindingPagePort",
    "CrmActivityEntityPair",
    "CrmBindingPageStatus",
    "CrmRelationshipBindingOperationsProfile",
    "CrmRelationshipBindingOutcome",
]
