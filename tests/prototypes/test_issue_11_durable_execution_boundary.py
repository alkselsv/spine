from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Owner(str, Enum):
    POSTGRES = "PostgreSQL canonical row"
    OUTBOX = "transactional outbox"
    WORKER = "explicit worker / activity"
    TEMPORAL = "Temporal workflow"
    API = "API/read model"
    UI = "UI/SSE stream"


class Status(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXHAUSTED = "retry_exhausted"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class Event:
    key: str
    status: Status
    owner: Owner


@dataclass
class Lifecycle:
    """Throwaway issue #11 prototype: fake ownership model, not production code."""

    mode: str
    events: list[Event] = field(default_factory=list)
    canonical_rows: dict[str, str] = field(default_factory=dict)
    outbox: dict[str, int] = field(default_factory=dict)
    projection_effects: set[str] = field(default_factory=set)
    active_projection: str = "projection-v1"
    shadow_projection: str | None = None
    retry_budget: int = 2
    cancelled: bool = False

    def accept(self, operation_key: str) -> None:
        self.canonical_rows.setdefault(operation_key, Status.ACCEPTED.value)
        self.outbox[operation_key] = self.outbox.get(operation_key, 0) + 1
        self.events.append(Event(operation_key, Status.ACCEPTED, Owner.POSTGRES))
        self.events.append(Event(operation_key, Status.ACCEPTED, Owner.OUTBOX))

    def deliver(self, operation_key: str, *, duplicate: bool = False) -> None:
        effect_key = f"effect:{operation_key}"
        self.events.append(Event(operation_key, Status.RUNNING, Owner.WORKER))
        if duplicate and effect_key in self.projection_effects:
            return
        self.projection_effects.add(effect_key)
        self.canonical_rows[operation_key] = Status.SUCCEEDED.value
        self.events.append(Event(operation_key, Status.SUCCEEDED, Owner.POSTGRES))

    def fail_then_retry(self, operation_key: str) -> None:
        self.events.append(Event(operation_key, Status.FAILED, Owner.WORKER))
        self.deliver(operation_key)

    def exhaust(self, operation_key: str) -> None:
        for _ in range(self.retry_budget + 1):
            self.events.append(Event(operation_key, Status.FAILED, Owner.WORKER))
        self.canonical_rows[operation_key] = Status.EXHAUSTED.value
        self.events.append(Event(operation_key, Status.EXHAUSTED, Owner.TEMPORAL))

    def cancel(self, operation_key: str) -> None:
        self.cancelled = True
        self.canonical_rows[operation_key] = Status.CANCELLED.value
        owner = Owner.TEMPORAL if self.mode != "postgres_owns_everything" else Owner.POSTGRES
        self.events.append(Event(operation_key, Status.CANCELLED, owner))

    def rebuild_shadow(self, operation_key: str, *, validation_passes: bool) -> None:
        self.events.append(Event(operation_key, Status.RUNNING, Owner.TEMPORAL))
        self.shadow_projection = "projection-v2-shadow"
        self.events.append(Event(self.shadow_projection, Status.SUCCEEDED, Owner.WORKER))
        if validation_passes:
            self.active_projection = "projection-v2"
            self.events.append(Event(self.active_projection, Status.SUCCEEDED, Owner.TEMPORAL))
        else:
            self.shadow_projection = None
            self.events.append(Event(self.active_projection, Status.ROLLED_BACK, Owner.TEMPORAL))

    def progress(self, operation_key: str) -> list[Status]:
        return [event.status for event in self.events if event.key == operation_key]


def test_at_least_once_duplicate_delivery_is_idempotent() -> None:
    lifecycle = Lifecycle(mode="hybrid")

    lifecycle.accept("source-revision:doc-a:v1")
    lifecycle.deliver("source-revision:doc-a:v1")
    lifecycle.deliver("source-revision:doc-a:v1", duplicate=True)

    assert lifecycle.outbox["source-revision:doc-a:v1"] == 1
    assert lifecycle.projection_effects == {"effect:source-revision:doc-a:v1"}
    assert lifecycle.canonical_rows["source-revision:doc-a:v1"] == Status.SUCCEEDED.value


def test_retry_after_partial_failure_preserves_single_logical_effect() -> None:
    lifecycle = Lifecycle(mode="hybrid")

    lifecycle.accept("projection-command:doc-a:v1")
    lifecycle.fail_then_retry("projection-command:doc-a:v1")
    lifecycle.deliver("projection-command:doc-a:v1", duplicate=True)

    assert lifecycle.progress("projection-command:doc-a:v1") == [
        Status.ACCEPTED,
        Status.ACCEPTED,
        Status.FAILED,
        Status.RUNNING,
        Status.SUCCEEDED,
        Status.RUNNING,
    ]
    assert lifecycle.projection_effects == {"effect:projection-command:doc-a:v1"}


def test_per_file_failure_in_batch_does_not_poison_other_files() -> None:
    lifecycle = Lifecycle(mode="hybrid")

    for key in ("file:a", "file:b", "file:c"):
        lifecycle.accept(key)
    lifecycle.deliver("file:a")
    lifecycle.exhaust("file:b")
    lifecycle.deliver("file:c")

    assert lifecycle.canonical_rows["file:a"] == Status.SUCCEEDED.value
    assert lifecycle.canonical_rows["file:b"] == Status.EXHAUSTED.value
    assert lifecycle.canonical_rows["file:c"] == Status.SUCCEEDED.value


def test_crash_between_canonical_write_and_delivery_recovers_from_outbox() -> None:
    before_crash = Lifecycle(mode="hybrid")
    before_crash.accept("source-revision:doc-crash:v1")

    recovered = Lifecycle(
        mode="hybrid",
        canonical_rows=dict(before_crash.canonical_rows),
        outbox=dict(before_crash.outbox),
    )
    recovered.deliver("source-revision:doc-crash:v1")

    assert recovered.canonical_rows["source-revision:doc-crash:v1"] == Status.SUCCEEDED.value
    assert recovered.projection_effects == {"effect:source-revision:doc-crash:v1"}


def test_cancellation_is_temporal_owned_for_composite_lifecycle() -> None:
    lifecycle = Lifecycle(mode="hybrid")

    lifecycle.accept("qa-run:1")
    lifecycle.cancel("qa-run:1")

    assert lifecycle.canonical_rows["qa-run:1"] == Status.CANCELLED.value
    assert lifecycle.events[-1] == Event("qa-run:1", Status.CANCELLED, Owner.TEMPORAL)


def test_rebuild_uses_shadow_projection_and_rolls_back_to_last_active_version() -> None:
    lifecycle = Lifecycle(mode="hybrid")

    lifecycle.rebuild_shadow("rebuild:workspace-a", validation_passes=False)

    assert lifecycle.active_projection == "projection-v1"
    assert lifecycle.shadow_projection is None
    assert lifecycle.events[-1] == Event("projection-v1", Status.ROLLED_BACK, Owner.TEMPORAL)


def test_progress_has_accepted_running_terminal_shape() -> None:
    lifecycle = Lifecycle(mode="hybrid")

    lifecycle.accept("projection-command:doc-progress:v1")
    lifecycle.deliver("projection-command:doc-progress:v1")

    progress = lifecycle.progress("projection-command:doc-progress:v1")
    assert progress[0] == Status.ACCEPTED
    assert Status.RUNNING in progress
    assert progress[-1] == Status.SUCCEEDED


def test_postgres_owns_everything_path_is_an_adr_amendment_path() -> None:
    lifecycle = Lifecycle(mode="postgres_owns_everything")

    lifecycle.accept("rebuild:workspace-a")
    lifecycle.cancel("rebuild:workspace-a")

    assert lifecycle.events[-1] == Event("rebuild:workspace-a", Status.CANCELLED, Owner.POSTGRES)
    assert lifecycle.mode == "postgres_owns_everything"
