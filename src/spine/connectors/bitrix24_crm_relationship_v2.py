"""Public connector-facing CRM v2 contract owned by the application layer."""

from spine.application.crm_relationship_v2_contracts import (
    Bitrix24CrmRelationshipV2ProviderPort,
    Bitrix24CrmRelationshipV2ReadCommand,
    Bitrix24CrmRelationshipV2ReadResult,
)

__all__ = [
    "Bitrix24CrmRelationshipV2ProviderPort",
    "Bitrix24CrmRelationshipV2ReadCommand",
    "Bitrix24CrmRelationshipV2ReadResult",
]
