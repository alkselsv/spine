"""Stable, redacted errors crossing the original-object storage boundary."""

from __future__ import annotations

from enum import Enum
from uuid import UUID


class StorageErrorCategory(str, Enum):
    INVALID_OBJECT_REFERENCE = "invalid_object_reference"
    UPLOAD_CONFLICT = "upload_conflict"
    UPLOAD_NOT_FOUND = "upload_not_found"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FENCE_ACTIVE = "fence_active"
    INTEGRITY_MISMATCH = "integrity_mismatch"
    OBJECT_NOT_FOUND = "object_not_found"
    OBJECT_UNAVAILABLE = "object_unavailable"
    OBJECT_CORRUPT = "object_corrupt"
    OBJECT_ALREADY_EXISTS = "object_already_exists"
    OBJECT_PROTECTED = "object_protected"
    OBJECT_DELETION_FAILED = "object_deletion_failed"
    UPLOAD_LIMIT_EXCEEDED = "upload_limit_exceeded"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    STORAGE_CONFIGURATION_ERROR = "storage_configuration_error"
    STORAGE_INTEGRITY_INDETERMINATE = "storage_integrity_indeterminate"
    RECOVERY_EVIDENCE_CHAIN_CONFLICT = "recovery_evidence_chain_conflict"
    RECOVERY_EVIDENCE_INDETERMINATE = "recovery_evidence_indeterminate"


class StorageRetryability(str, Enum):
    RETRYABLE = "retryable"
    NOT_RETRYABLE = "not_retryable"


_DEFAULT_DETAILS = {category: category.value.replace("_", " ") for category in StorageErrorCategory}


class StorageError(Exception):
    """Provider-neutral error with a stable category and disclosure-safe detail."""

    category: StorageErrorCategory
    retryability: StorageRetryability

    def __init__(self, *, trace_id: UUID | None = None) -> None:
        self.trace_id = trace_id
        super().__init__(self.redacted_detail)

    @property
    def retryable(self) -> bool:
        return self.retryability is StorageRetryability.RETRYABLE

    @property
    def redacted_detail(self) -> str:
        return _DEFAULT_DETAILS[self.category]

    def redacted(self) -> dict[str, object]:
        """Return the only representation safe to cross the application seam."""

        return {
            "category": self.category.value,
            "retryability": self.retryability.value,
            "trace_id": self.trace_id,
            "detail": self.redacted_detail,
        }


def _error_type(
    name: str,
    category: StorageErrorCategory,
    retryability: StorageRetryability,
) -> type[StorageError]:
    return type(name, (StorageError,), {"category": category, "retryability": retryability})


_CATEGORIES = {
    "InvalidObjectReference": (
        StorageErrorCategory.INVALID_OBJECT_REFERENCE,
        StorageRetryability.NOT_RETRYABLE,
    ),
    "UploadConflict": (StorageErrorCategory.UPLOAD_CONFLICT, StorageRetryability.NOT_RETRYABLE),
    "UploadNotFound": (StorageErrorCategory.UPLOAD_NOT_FOUND, StorageRetryability.NOT_RETRYABLE),
    "ReconciliationRequired": (
        StorageErrorCategory.RECONCILIATION_REQUIRED,
        StorageRetryability.NOT_RETRYABLE,
    ),
    "FenceActive": (StorageErrorCategory.FENCE_ACTIVE, StorageRetryability.RETRYABLE),
    "IntegrityMismatch": (StorageErrorCategory.INTEGRITY_MISMATCH, StorageRetryability.NOT_RETRYABLE),
    "ObjectNotFound": (StorageErrorCategory.OBJECT_NOT_FOUND, StorageRetryability.NOT_RETRYABLE),
    "ObjectUnavailable": (StorageErrorCategory.OBJECT_UNAVAILABLE, StorageRetryability.RETRYABLE),
    "ObjectCorrupt": (StorageErrorCategory.OBJECT_CORRUPT, StorageRetryability.NOT_RETRYABLE),
    "ObjectAlreadyExists": (StorageErrorCategory.OBJECT_ALREADY_EXISTS, StorageRetryability.NOT_RETRYABLE),
    "ObjectProtected": (StorageErrorCategory.OBJECT_PROTECTED, StorageRetryability.NOT_RETRYABLE),
    "ObjectDeletionFailed": (StorageErrorCategory.OBJECT_DELETION_FAILED, StorageRetryability.RETRYABLE),
    "UploadLimitExceeded": (StorageErrorCategory.UPLOAD_LIMIT_EXCEEDED, StorageRetryability.NOT_RETRYABLE),
    "UnsupportedMediaType": (StorageErrorCategory.UNSUPPORTED_MEDIA_TYPE, StorageRetryability.NOT_RETRYABLE),
    "StorageConfigurationError": (
        StorageErrorCategory.STORAGE_CONFIGURATION_ERROR,
        StorageRetryability.NOT_RETRYABLE,
    ),
    "StorageIntegrityIndeterminate": (
        StorageErrorCategory.STORAGE_INTEGRITY_INDETERMINATE,
        StorageRetryability.NOT_RETRYABLE,
    ),
    "RecoveryEvidenceChainConflict": (
        StorageErrorCategory.RECOVERY_EVIDENCE_CHAIN_CONFLICT,
        StorageRetryability.NOT_RETRYABLE,
    ),
    "RecoveryEvidenceIndeterminate": (
        StorageErrorCategory.RECOVERY_EVIDENCE_INDETERMINATE,
        StorageRetryability.NOT_RETRYABLE,
    ),
}

globals().update(
    {
        name: _error_type(name, category, retryability)
        for name, (category, retryability) in _CATEGORIES.items()
    }
)

__all__ = ["StorageError", "StorageErrorCategory", "StorageRetryability", *_CATEGORIES]
