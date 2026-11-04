"""PostgreSQL authority for immutable private-ASR manifests and current releases.

Runtime access is read-only. Governance writes are performed by the separately
privileged migration/governance principal; this adapter never approves a release.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Column, DateTime, MetaData, String, Table, Text, select
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID

from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.private_asr_corpus import PrivateReportSourceBinding
_METADATA = MetaData(schema="spine")
private_asr_corpus_manifests = Table(
    "private_asr_corpus_manifests", _METADATA,
    Column("workspace_id", PGUUID(as_uuid=True)), Column("environment_id", PGUUID(as_uuid=True)),
    Column("purpose", Text), Column("dataset_id", PGUUID(as_uuid=True)),
    Column("manifest_id", PGUUID(as_uuid=True)), Column("manifest_version", Text),
    Column("manifest_sha256", String(64)), Column("manifest_payload", JSONB),
    Column("created_at", DateTime(timezone=True)),
)
private_asr_corpus_releases = Table(
    "private_asr_corpus_releases", _METADATA,
    Column("workspace_id", PGUUID(as_uuid=True)), Column("environment_id", PGUUID(as_uuid=True)),
    Column("purpose", Text), Column("dataset_id", PGUUID(as_uuid=True)),
    Column("generation", BigInteger), Column("manifest_id", PGUUID(as_uuid=True)),
    Column("manifest_version", Text), Column("manifest_sha256", String(64)),
    Column("status", Text), Column("updated_at", DateTime(timezone=True)),
)


def _canonical_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PostgreSQLPrivateASRManifestAuthority:
    """Trusted resolver implementing the existing #795 lineage-verifier port."""

    async def verify_current(
        self,
        *,
        transaction: object,
        context: TrustedPersistenceContext,
        dataset_id: UUID,
        manifest_version: str,
        manifest_sha256: str,
        retention_profile_ref: object,
        source_bindings: tuple[PrivateReportSourceBinding, ...],
    ) -> bool:
        del retention_profile_ref  # retention is independently resolved by #795
        try:
            context.validate_shape()
            scope = context.scope
            if not isinstance(scope, EnvironmentScope) or context.purpose.value != "asr_model_evaluation":
                return False
            if not source_bindings or len({item.record_id for item in source_bindings}) != len(source_bindings):
                return False
            if any(item.dataset_id != dataset_id for item in source_bindings):
                return False
            base = (
                private_asr_corpus_manifests.c.workspace_id == scope.workspace_id,
                private_asr_corpus_manifests.c.environment_id == scope.environment_id,
                private_asr_corpus_manifests.c.purpose == "asr_model_evaluation",
                private_asr_corpus_manifests.c.dataset_id == dataset_id,
                private_asr_corpus_manifests.c.manifest_version == manifest_version,
                private_asr_corpus_manifests.c.manifest_sha256 == manifest_sha256,
            )
            manifest = (await transaction.execute(select(
                private_asr_corpus_manifests.c.manifest_id,
                private_asr_corpus_manifests.c.manifest_version,
                private_asr_corpus_manifests.c.manifest_sha256,
                private_asr_corpus_manifests.c.manifest_payload,
            ).where(*base))).mappings().one_or_none()
            if manifest is None:
                return False
            payload = manifest["manifest_payload"]
            if not isinstance(payload, dict) or _canonical_digest(payload) != manifest_sha256:
                return False
            if (
                payload.get("dataset_id") != str(dataset_id)
                or payload.get("version") != manifest_version
                or payload.get("workspace_id") != str(scope.workspace_id)
                or payload.get("environment_id") != str(scope.environment_id)
                or payload.get("purpose") != "asr_model_evaluation"
            ):
                return False
            records = payload.get("records")
            if not isinstance(records, list):
                return False
            membership: dict[UUID, dict[str, Any]] = {}
            for record in records:
                if not isinstance(record, dict):
                    return False
                record_id = UUID(str(record["record_id"]))
                if record_id in membership:
                    return False
                membership[record_id] = record
            for binding in source_bindings:
                record = membership.get(binding.record_id)
                if record is None:
                    return False
                source = record.get("source_binding")
                if not isinstance(source, dict) or (
                    source.get("source_object_id") != str(binding.source_object_id)
                    or source.get("source_revision_id") != str(binding.source_revision_id)
                    or source.get("source_revision_digest") != binding.source_revision_digest
                    or record.get("workspace_id") != str(scope.workspace_id)
                    or record.get("environment_id") != str(scope.environment_id)
                    or record.get("split") != binding.split.value
                ):
                    return False
            release = (await transaction.execute(select(
                private_asr_corpus_releases.c.generation,
                private_asr_corpus_releases.c.manifest_id,
                private_asr_corpus_releases.c.manifest_version,
                private_asr_corpus_releases.c.manifest_sha256,
                private_asr_corpus_releases.c.status,
            ).where(
                private_asr_corpus_releases.c.workspace_id == scope.workspace_id,
                private_asr_corpus_releases.c.environment_id == scope.environment_id,
                private_asr_corpus_releases.c.purpose == "asr_model_evaluation",
                private_asr_corpus_releases.c.dataset_id == dataset_id,
            ).with_for_update(read=True))).mappings().one_or_none()
            return bool(
                release is not None and release["generation"] > 0
                and release["status"] == "released"
                and release["manifest_id"] == manifest["manifest_id"]
                and release["manifest_version"] == manifest_version
                and release["manifest_sha256"] == manifest_sha256
            )
        except Exception:
            # No private IDs, hashes, or membership details leave the authority.
            return False


__all__ = ["PostgreSQLPrivateASRManifestAuthority"]
