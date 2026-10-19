from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.production_qa_composition import (
    _issue_production_qa_composition_evidence,
)


class _Verifier:
    def __init__(self, *, accepted: TrustedPersistenceContext) -> None:
        self.accepted = accepted

    def verify(self, context: TrustedPersistenceContext) -> TrustedPersistenceContext:
        if context != self.accepted:
            raise ValueError("context is not trusted")
        return context


def test_production_composition_evidence_verifies_context_through_port() -> None:
    context = _trusted_context()
    composition = object()
    evidence = _issue_production_qa_composition_evidence(
        composition,
        _Verifier(accepted=context),  # type: ignore[arg-type]
    )

    assert evidence.matches(composition)
    assert evidence.verify_context(context) is context


def test_production_composition_evidence_rejects_untrusted_context() -> None:
    accepted = _trusted_context()
    rejected = _trusted_context(trace_id=UUID(int=3))
    evidence = _issue_production_qa_composition_evidence(
        object(),
        _Verifier(accepted=accepted),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="not trusted"):
        evidence.verify_context(rejected)


def _trusted_context(*, trace_id: UUID = UUID(int=1)) -> TrustedPersistenceContext:
    """Build context values without creating trusted provenance authority."""
    from spine.application.persistence.context import (
        ContextOrigin,
        EnvironmentScope,
        PersistenceOperation,
        PersistencePurpose,
        TrustedContextProvenance,
    )

    return TrustedPersistenceContext(
        scope=EnvironmentScope(UUID(int=10), UUID(int=20)),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("qa.ask"),
        operation=PersistenceOperation("qa.ask-admission"),
        trace_id=trace_id,
        acting_subject_id=UUID(int=30),
        service_principal_id=None,
        provenance=TrustedContextProvenance(
            issuer_id=UUID(int=40),
            signature=b"synthetic",
        ),
    )
