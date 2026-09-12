"""Pure business models and invariants for Spine.

This package must not import FastAPI, Cognee, Temporal, or infrastructure
adapters.  Application and infrastructure code depend on the domain, never
the other way around.
"""

from spine.domain.common import ActorKind, ActorRef, EnvironmentKind

__all__ = ["ActorKind", "ActorRef", "EnvironmentKind"]
