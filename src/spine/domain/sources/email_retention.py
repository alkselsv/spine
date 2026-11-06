"""Lossless mapping from the #820 email artifact taxonomy to retention classes."""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Mapping

from spine.domain.sources.retention import RetentionArtifactClass


class EmailArtifactClass(str, Enum):
    """The twenty distinct email artifact classes defined by #820.

    This taxonomy identifies policy subjects. Membership does not approve a
    retention policy, applicability binding, purpose, or deletion outcome.
    """

    RAW_MIME_RFC822 = "raw_mime_rfc822"
    HEADERS_ADDRESSES = "headers_addresses"
    PLAIN_TEXT_BODY = "plain_text_body"
    PARSED_HTML = "parsed_html"
    SANITIZED_CONTENT = "sanitized_content"
    ATTACHMENT_ORIGINALS = "attachment_originals"
    ATTACHMENT_DERIVED_TEXT = "attachment_derived_text"
    ATTACHMENT_DERIVED_STRUCTURED = "attachment_derived_structured"
    THREAD_CONVERSATION_METADATA = "thread_conversation_metadata"
    PROVIDER_MESSAGE_FOLDER_IDENTIFIERS = "provider_message_folder_identifiers"
    SOURCE_OBJECT_REVISION_IDENTITY_LINEAGE = "source_object_revision_identity_lineage"
    SEARCH_INDEXES = "search_indexes"
    VECTOR_EMBEDDINGS_PROJECTIONS = "vector_embeddings_projections"
    GENERATED_SUMMARIES = "generated_summaries"
    MONITORING_EVIDENCE = "monitoring_evidence"
    STAGING_ARTIFACTS = "staging_artifacts"
    TEMPORARY_FILES_CACHES = "temporary_files_caches"
    REPLICAS_BACKUPS = "replicas_backups"
    AUDIT_RECORDS = "audit_records"
    OPERATIONAL_DIAGNOSTICS = "operational_diagnostics"


_EMAIL_ARTIFACT_RETENTION_CLASS_ENTRIES = (
    (EmailArtifactClass.RAW_MIME_RFC822, RetentionArtifactClass.EMAIL_RAW_MIME),
    (
        EmailArtifactClass.HEADERS_ADDRESSES,
        RetentionArtifactClass.EMAIL_HEADERS_ADDRESSES,
    ),
    (
        EmailArtifactClass.PLAIN_TEXT_BODY,
        RetentionArtifactClass.EMAIL_PLAIN_TEXT_BODY,
    ),
    (EmailArtifactClass.PARSED_HTML, RetentionArtifactClass.EMAIL_PARSED_HTML),
    (
        EmailArtifactClass.SANITIZED_CONTENT,
        RetentionArtifactClass.EMAIL_SANITIZED_CONTENT,
    ),
    (
        EmailArtifactClass.ATTACHMENT_ORIGINALS,
        RetentionArtifactClass.EMAIL_ATTACHMENT_ORIGINAL,
    ),
    (
        EmailArtifactClass.ATTACHMENT_DERIVED_TEXT,
        RetentionArtifactClass.EMAIL_ATTACHMENT_DERIVED_TEXT,
    ),
    (
        EmailArtifactClass.ATTACHMENT_DERIVED_STRUCTURED,
        RetentionArtifactClass.EMAIL_ATTACHMENT_DERIVED_STRUCTURED,
    ),
    (
        EmailArtifactClass.THREAD_CONVERSATION_METADATA,
        RetentionArtifactClass.EMAIL_THREAD_CONVERSATION_METADATA,
    ),
    (
        EmailArtifactClass.PROVIDER_MESSAGE_FOLDER_IDENTIFIERS,
        RetentionArtifactClass.EMAIL_PROVIDER_IDENTIFIERS,
    ),
    (
        EmailArtifactClass.SOURCE_OBJECT_REVISION_IDENTITY_LINEAGE,
        RetentionArtifactClass.EMAIL_SOURCE_IDENTITY_LINEAGE,
    ),
    (EmailArtifactClass.SEARCH_INDEXES, RetentionArtifactClass.EMAIL_SEARCH_INDEX),
    (
        EmailArtifactClass.VECTOR_EMBEDDINGS_PROJECTIONS,
        RetentionArtifactClass.EMAIL_VECTOR_EMBEDDING,
    ),
    (
        EmailArtifactClass.GENERATED_SUMMARIES,
        RetentionArtifactClass.EMAIL_GENERATED_SUMMARY,
    ),
    (
        EmailArtifactClass.MONITORING_EVIDENCE,
        RetentionArtifactClass.EMAIL_MONITORING_EVIDENCE,
    ),
    (
        EmailArtifactClass.STAGING_ARTIFACTS,
        RetentionArtifactClass.EMAIL_STAGING_ARTIFACT,
    ),
    (
        EmailArtifactClass.TEMPORARY_FILES_CACHES,
        RetentionArtifactClass.EMAIL_TEMPORARY_FILE_CACHE,
    ),
    (
        EmailArtifactClass.REPLICAS_BACKUPS,
        RetentionArtifactClass.EMAIL_REPLICA_BACKUP,
    ),
    (EmailArtifactClass.AUDIT_RECORDS, RetentionArtifactClass.EMAIL_AUDIT_RECORD),
    (
        EmailArtifactClass.OPERATIONAL_DIAGNOSTICS,
        RetentionArtifactClass.EMAIL_OPERATIONAL_DIAGNOSTIC,
    ),
)

EMAIL_ARTIFACT_RETENTION_CLASS: Mapping[
    EmailArtifactClass, RetentionArtifactClass
] = MappingProxyType(dict(_EMAIL_ARTIFACT_RETENTION_CLASS_ENTRIES))


def retention_class_for_email_artifact(
    artifact_class: EmailArtifactClass,
) -> RetentionArtifactClass:
    """Return the distinct RetentionApplicability class for one #820 class."""

    return EMAIL_ARTIFACT_RETENTION_CLASS[artifact_class]
