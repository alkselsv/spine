"""Datasets and runners used by Spine evaluation suites."""

from spine.evals.qa_r1 import (
    QADataset,
    QA_R1_CASE_SCHEMA_VERSION,
    QA_R1_SCHEMA_VERSION,
    load_r1_dataset,
    serialize_dataset,
)
from spine.evals.offline_runner import (
    CaseEvaluationContext,
    DatasetIdentity,
    ExecutionMode,
    MeasurementObservation,
    OfflineEvaluationReport,
    OfflineEvaluationRunner,
    ProductionContractObservationPort,
    run_r1_evaluation,
    run_r1_evaluation_sync,
    run_r1_production_contract_evaluation,
    run_r1_production_contract_evaluation_sync,
)
from spine.evals.production_contract import (
    PRODUCTION_CONTRACT_CASE_REQUEST_SCHEMA,
    PRODUCTION_CONTRACT_INDEX_VERSION,
    PRODUCTION_CONTRACT_OBSERVATION_SEAM,
    ProductionContractApplicationFlow,
    ProductionContractCaseRequest,
    ProductionContractObservation,
    ProductionContractObservationBridge,
    ProductionContractObservationError,
)

__all__ = [
    "QADataset",
    "QA_R1_CASE_SCHEMA_VERSION",
    "QA_R1_SCHEMA_VERSION",
    "load_r1_dataset",
    "serialize_dataset",
    "DatasetIdentity",
    "ExecutionMode",
    "CaseEvaluationContext",
    "MeasurementObservation",
    "OfflineEvaluationReport",
    "OfflineEvaluationRunner",
    "PRODUCTION_CONTRACT_CASE_REQUEST_SCHEMA",
    "PRODUCTION_CONTRACT_INDEX_VERSION",
    "PRODUCTION_CONTRACT_OBSERVATION_SEAM",
    "ProductionContractApplicationFlow",
    "ProductionContractCaseRequest",
    "ProductionContractObservation",
    "ProductionContractObservationBridge",
    "ProductionContractObservationError",
    "ProductionContractObservationPort",
    "run_r1_evaluation",
    "run_r1_evaluation_sync",
    "run_r1_production_contract_evaluation",
    "run_r1_production_contract_evaluation_sync",
]
