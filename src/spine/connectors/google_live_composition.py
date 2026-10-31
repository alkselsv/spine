"""Explicit dependency-injected composition for Google live qualification."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import random

from spine.application.connections.google import GoogleConnectionRepository
from spine.application.diagnostics import DiagnosticEventRegistry, DiagnosticSink
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.connectors.google_api import (
    GoogleApiRetryObserver,
    GoogleApiService,
    GoogleApiTransport,
    GoogleCredentialResolver,
)
from spine.connectors.google_execution_envelope import GoogleExecutionApprovalVerifier
from spine.connectors.google_live_qualification import (
    GoogleLiveQualificationConfig,
    GoogleLiveQualificationRefused,
    GoogleLiveQualificationRunner,
    GoogleSharedDrivePreflightPort,
    compose_google_live_qualification_runner,
)


@dataclass(frozen=True, slots=True)
class GoogleLiveQualificationBindings:
    """Required deployment ports for constructing one qualification runner."""

    credential_resolver: GoogleCredentialResolver
    transport: GoogleApiTransport
    retry_observer: GoogleApiRetryObserver
    connection_repository: GoogleConnectionRepository
    diagnostic_sink: DiagnosticSink
    diagnostic_registry: DiagnosticEventRegistry
    clock: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[None]]
    context: TrustedPersistenceContext
    approval_verifier: GoogleExecutionApprovalVerifier
    jitter: Callable[[float, float], float] = random.uniform
    shared_drive_preflight: GoogleSharedDrivePreflightPort | None = None

    def build_runner(
        self, config: GoogleLiveQualificationConfig
    ) -> GoogleLiveQualificationRunner:
        """Build from the exact validated config and explicitly supplied ports."""

        if not isinstance(config, GoogleLiveQualificationConfig):
            raise GoogleLiveQualificationRefused(
                "Google live composition requires a validated configuration"
            )
        if self.shared_drive_preflight is None and config.resource_scope.drive_id:
            raise GoogleLiveQualificationRefused(
                "Google live composition lacks the required Shared Drive preflight"
            )
        self._validate_bindings(config)
        try:
            service = GoogleApiService(
                endpoints=config.endpoints,
                request_policy=config.request_policy,
                resource_scopes=(config.resource_scope,),
                credential_resolver=self.credential_resolver,
                transport=self.transport,
                retry_observer=self.retry_observer,
                jitter=self.jitter,
                sheet_profiles=tuple(
                    target.profile for target in config.sheet_targets
                ),
                clock=self.clock,
                sleep=self.sleep,
            )
            if (
                service.configuration_fingerprint
                != config.api_configuration_fingerprint
            ):
                raise GoogleLiveQualificationRefused(
                    "Google live composition does not match the validated configuration"
                )
            runner = compose_google_live_qualification_runner(
                config=config,
                service=service,
                repository=self.connection_repository,
                diagnostic_sink=self.diagnostic_sink,
                diagnostic_registry=self.diagnostic_registry,
                clock=self.clock,
                context=self.context,
                approval_verifier=self.approval_verifier,
                shared_drive_preflight=self.shared_drive_preflight,
            )
        except GoogleLiveQualificationRefused:
            raise
        except Exception:
            raise GoogleLiveQualificationRefused(
                "Google live composition could not be constructed"
            ) from None
        if runner.config != config:
            raise GoogleLiveQualificationRefused(
                "Google live composition does not match the validated configuration"
            )
        return runner

    def _validate_bindings(self, config: GoogleLiveQualificationConfig) -> None:
        required_methods = (
            (self.credential_resolver, "resolve"),
            (self.transport, "request"),
            (self.retry_observer, "record_retry"),
            (self.connection_repository, "admit_read"),
            (self.diagnostic_sink, "emit"),
            (self.diagnostic_registry, "build_event"),
            (self.approval_verifier, "is_approved"),
        )
        if any(
            not callable(getattr(port, method, None))
            for port, method in required_methods
        ):
            raise GoogleLiveQualificationRefused(
                "Google live composition bindings are unsupported"
            )
        if (
            not callable(self.clock)
            or not callable(self.sleep)
            or not callable(self.jitter)
        ):
            raise GoogleLiveQualificationRefused(
                "Google live composition bindings are unsupported"
            )
        if self.shared_drive_preflight is not None and any(
            not callable(getattr(self.shared_drive_preflight, method, None))
            for method in ("resolve_approval", "resolve_execution_authorization")
        ):
            raise GoogleLiveQualificationRefused(
                "Google live composition bindings are unsupported"
            )
        if not isinstance(self.context, TrustedPersistenceContext):
            raise GoogleLiveQualificationRefused(
                "Google live composition requires trusted persistence context"
            )
        try:
            self.context.validate_shape()
        except Exception:
            raise GoogleLiveQualificationRefused(
                "Google live composition requires trusted persistence context"
            ) from None
        scope = self.context.scope
        if (
            not isinstance(scope, EnvironmentScope)
            or scope.workspace_id != config.connection.workspace_id
            or scope.environment_id != config.connection.environment_id
        ):
            raise GoogleLiveQualificationRefused(
                "Google live composition context does not match the validated workspace"
            )


def make_google_live_qualification_factory(
    bindings: GoogleLiveQualificationBindings,
) -> Callable[[GoogleLiveQualificationConfig], GoogleLiveQualificationRunner]:
    """Return the one-argument factory expected by the CLI module:function API."""

    if not isinstance(bindings, GoogleLiveQualificationBindings):
        raise GoogleLiveQualificationRefused(
            "Google live composition bindings are unavailable"
        )

    def factory(
        config: GoogleLiveQualificationConfig,
    ) -> GoogleLiveQualificationRunner:
        return bindings.build_runner(config)

    return factory


__all__ = [
    "GoogleLiveQualificationBindings",
    "make_google_live_qualification_factory",
]
