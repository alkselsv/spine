"""Narrow evidence that a durable Q&A production composition was validated."""

from __future__ import annotations

from dataclasses import dataclass

from spine.application.persistence.context import (
    TrustedContextVerifier,
    TrustedPersistenceContext,
)

_ISSUER = object()


@dataclass(frozen=True, slots=True, init=False)
class ProductionQAACompositionEvidence:
    """Evidence bound to the exact composition checked by its API root."""

    _composition: object
    _context_verifier: TrustedContextVerifier

    def __init__(
        self,
        composition: object,
        context_verifier: TrustedContextVerifier,
        *,
        _issuer: object = None,
    ) -> None:
        if _issuer is not _ISSUER:
            raise TypeError("production Q&A evidence must be issued by the API composition root")
        object.__setattr__(self, "_composition", composition)
        object.__setattr__(self, "_context_verifier", context_verifier)

    def matches(self, composition: object) -> bool:
        return self._composition is composition

    def matches_evidence(self, other: object) -> bool:
        return (
            type(other) is ProductionQAACompositionEvidence
            and self._composition is other._composition
            and self._context_verifier is other._context_verifier
        )

    def verify_context(self, context: TrustedPersistenceContext) -> TrustedPersistenceContext:
        return self._context_verifier.verify(context)


def _issue_production_qa_composition_evidence(
    composition: object,
    context_verifier: TrustedContextVerifier,
) -> ProductionQAACompositionEvidence:
    return ProductionQAACompositionEvidence(
        composition,
        context_verifier,
        _issuer=_ISSUER,
    )


__all__ = ["ProductionQAACompositionEvidence"]
