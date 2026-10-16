"""Safe exception taxonomy for the feedback application boundary."""

from __future__ import annotations


class FeedbackTargetNotFound(LookupError):
    """The target is unknown, inaccessible, or does not match its references."""


class InvalidFeedbackRequest(ValueError):
    """The feedback command cannot be accepted safely."""


__all__ = ["FeedbackTargetNotFound", "InvalidFeedbackRequest"]
