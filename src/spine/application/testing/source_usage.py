"""Deterministic content-free current-source authority for offline tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from spine.application.source_usage_guard import (
    CurrentSourceUsageDecision,
    SourceUsageReason,
    SourceUsageRequest,
)


@dataclass
class DeterministicCurrentSourceUsageAuthority:
    allowed: bool = True
    reason: SourceUsageReason = SourceUsageReason.ALLOWED
    requests: list[SourceUsageRequest] = field(default_factory=list)

    async def evaluate(self, request: SourceUsageRequest) -> CurrentSourceUsageDecision:
        self.requests.append(request)
        reason = self.reason if not self.allowed else SourceUsageReason.ALLOWED
        return CurrentSourceUsageDecision(
            allowed=self.allowed,
            reason=reason,
            required_dependency_count=len(request.dependencies),
            checked_dependencies=request.dependencies,
        )


__all__ = ["DeterministicCurrentSourceUsageAuthority"]
