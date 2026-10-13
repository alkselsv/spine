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
from spine.domain.artifacts import ParseCoverage, ParseExclusion, ParsedDocument, ParsedSegment, PhysicalLineLocator

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
    "ParseCoverage",
    "ParseExclusion",
    "ParsedDocument",
    "ParsedSegment",
    "PhysicalLineLocator",
]
