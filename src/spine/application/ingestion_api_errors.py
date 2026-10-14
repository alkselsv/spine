"""Stable failure categories for the document-ingestion transport seam."""


class InvalidIngestionRequest(ValueError):
    """The transport could not form a valid ingestion command."""


class UploadPayloadTooLarge(ValueError):
    """The upload exceeded the server-owned request limit."""


class IngestionServiceUnavailable(RuntimeError):
    """The API has no usable ingestion service configured."""


class IngestionRunNotFound(LookupError):
    """No run/item pair exists in the authorized tenant scope."""


__all__ = [
    "IngestionRunNotFound",
    "IngestionServiceUnavailable",
    "InvalidIngestionRequest",
    "UploadPayloadTooLarge",
]
