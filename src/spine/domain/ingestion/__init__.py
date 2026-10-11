"""Typed operational records for document ingestion."""

from .models import (
    IngestionFailure,
    IngestionItemState,
    IngestionRun,
    IngestionRunSource,
    IngestionRunSourceBinding,
    IngestionRunState,
    IngestionTargetKind,
    IngestionTransition,
    IngestionWarning,
    ProcessingCoverage,
    ProcessingExclusion,
    ProcessingReceipt,
    SafeDiagnosticDetail,
    WarningStage,
    FailureStage,
)

__all__ = [
    "FailureStage",
    "IngestionFailure",
    "IngestionItemState",
    "IngestionRun",
    "IngestionRunSource",
    "IngestionRunSourceBinding",
    "IngestionRunState",
    "IngestionTargetKind",
    "IngestionTransition",
    "IngestionWarning",
    "ProcessingCoverage",
    "ProcessingExclusion",
    "ProcessingReceipt",
    "SafeDiagnosticDetail",
    "WarningStage",
]
