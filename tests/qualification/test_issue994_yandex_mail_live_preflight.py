from datetime import datetime, timezone
from uuid import UUID, uuid5, NAMESPACE_URL

import pytest

from spine.application.yandex_mail_live_qualification import (
    CurrentBinding, Decision, Evidence, ExecutionAuthorization, QualificationEvidence,
    YandexMailQualificationManifest, evaluate_yandex_mail_preflight,
    manifest_digest, run_yandex_mail_qualification,
)


def uid(n: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{n:012d}")


def opaque(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, name))


NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
KEYS = ("mailbox_owner", "data_owner", "security", "legal_compliance", "operations", "oauth_app", "oauth_account", "oauth_grant", "corporate_agreement", "scope", "tls_xoauth2", "retention", "cleanup_revocation", "incident_contacts")


def manifest(**changes):
    ev = {key: Evidence(reference=opaque(f"evidence-{key}"), verified=True) for key in KEYS}
    ev["mailbox_owner"] = Evidence(reference=opaque("evidence-mailbox_owner"), verified=True, subject_refs=(opaque("mailbox"),))
    ev["data_owner"] = Evidence(reference=opaque("evidence-data_owner"), verified=True, subject_refs=(opaque("mailbox"), opaque("purpose"), opaque("folder-in"), opaque("folder-out")))
    for key in ("oauth_app", "oauth_account", "oauth_grant"):
        ev[key] = Evidence(reference=opaque(f"evidence-{key}"), verified=True, subject_refs=(opaque(key),))
    ev["retention"] = Evidence(reference=opaque("evidence-retention"), verified=True, artifact_classes=("originals", "parsed", "attachments", "projections", "caches", "staging", "replicas", "backups", "diagnostics"))
    data = dict(workspace_id=uid(1), environment_id=uid(2), connection_id=uid(3), connection_version_id=uid(4), configuration_generation=7, authorization_generation=9, mailbox_ref=opaque("mailbox"), oauth_app_ref=opaque("oauth_app"), oauth_account_ref=opaque("oauth_account"), oauth_grant_ref=opaque("oauth_grant"), included_folder_refs=(opaque("folder-in"),), excluded_folder_refs=(opaque("folder-out"),), purpose_ref=opaque("purpose"), operator_ref=opaque("operator"), backup_operator_ref=opaque("backup"), window_start=NOW, window_end=datetime(2026, 11, 5, 13, tzinfo=timezone.utc), evidence=ev)
    data.update(changes)
    data["evidence"] = QualificationEvidence(**data["evidence"])
    return YandexMailQualificationManifest(**data)


def binding(m=None, **changes):
    m = m or manifest()
    data = dict(workspace_id=m.workspace_id, environment_id=m.environment_id, connection_id=m.connection_id, connection_version_id=m.connection_version_id, configuration_generation=m.configuration_generation, authorization_generation=m.authorization_generation, evaluated_at=NOW, mailbox_ref=m.mailbox_ref, folder_refs=m.included_folder_refs)
    data.update(changes)
    return CurrentBinding(**data)


def authorization(m):
    return ExecutionAuthorization(authorization_ref=opaque("authorization"), manifest_digest=manifest_digest(m), workspace_id=m.workspace_id, environment_id=m.environment_id, connection_id=m.connection_id, connection_version_id=m.connection_version_id, configuration_generation=m.configuration_generation, authorization_generation=m.authorization_generation, approved_window_start=m.window_start, approved_window_end=m.window_end, approver_ref=opaque("approver"), decision="APPROVED")


@pytest.mark.parametrize("field", ["mailbox_owner", "data_owner", "security", "legal_compliance", "operations", "oauth_app", "oauth_account", "oauth_grant", "corporate_agreement", "scope", "tls_xoauth2", "retention", "cleanup_revocation", "incident_contacts"])
def test_missing_evidence_fails_closed(field):
    evidence = manifest().evidence.model_dump()
    evidence[field] = None
    m = manifest(evidence=evidence)
    report = evaluate_yandex_mail_preflight(m, authorization(m), binding(m))
    assert report.decision in (Decision.NOT_APPROVED, Decision.NOT_VERIFIED)
    assert report.provider_call_permitted is False


@pytest.mark.parametrize("changes", [{"oauth_scope": "mail:imap_full"}, {"auth_mechanism": "PASSWORD"}, {"port": 143}, {"commands": ("EXAMINE", "EXPUNGE")}, {"tls_certificate_validated": False}])
def test_prohibited_transport_or_scope_is_denied(changes):
    m = manifest(**changes)
    assert evaluate_yandex_mail_preflight(m, authorization(m), binding(m)).decision == Decision.DENY


def test_stale_generation_and_cross_workspace_are_denied():
    m = manifest()
    stale = binding(m).model_copy(update={"authorization_generation": 10})
    assert evaluate_yandex_mail_preflight(m, authorization(m), stale).decision == Decision.DENY
    cross = binding(m).model_copy(update={"workspace_id": uid(99)})
    assert evaluate_yandex_mail_preflight(m, authorization(m), cross).decision == Decision.DENY


def test_protected_values_missing_retention_coverage_and_unapproved_host_are_blocked():
    m = manifest(transport_host_profile="attacker-host")
    assert evaluate_yandex_mail_preflight(m, authorization(m), binding(m)).decision == Decision.DENY
    with pytest.raises(ValueError):
        manifest(mailbox_ref="person@example.test")
    m = manifest()
    ev = m.evidence.model_copy(update={"retention": Evidence(reference=opaque("retention-evidence"), verified=True)})
    m = m.model_copy(update={"evidence": ev})
    assert evaluate_yandex_mail_preflight(m, authorization(m), binding(m)).decision == Decision.NOT_APPROVED


def test_execution_authorization_requires_project_owner_and_pinned_profile():
    m = manifest()
    auth = authorization(m).model_copy(update={"approver_role": "Operator"})
    assert evaluate_yandex_mail_preflight(m, auth, binding(m)).decision == Decision.NOT_APPROVED


@pytest.mark.asyncio
async def test_verified_preflight_calls_only_injected_fake():
    m = manifest()
    report = evaluate_yandex_mail_preflight(m, authorization(m), binding(m))
    assert report.decision == Decision.VERIFIED

    class Fake:
        calls = 0
        async def qualify(self, manifest):
            self.calls += 1
            return "fake-result"

    class Gate:
        used = False
        async def current_binding(self, manifest): return binding(manifest)
        async def consume(self, authorization_ref, digest, *, at):
            if self.used: return False
            self.used = True
            return True

    fake = Fake()
    gate = Gate()
    assert await run_yandex_mail_qualification(report, m, authorization(m), gate, fake) == "fake-result"
    assert fake.calls == 1
    with pytest.raises(PermissionError):
        await run_yandex_mail_qualification(report, m, authorization(m), gate, fake)
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_denied_preflight_never_calls_fake():
    m = manifest()
    report = evaluate_yandex_mail_preflight(m, None, binding(m))

    class Fake:
        calls = 0
        async def qualify(self, manifest): self.calls += 1

    class Gate:
        async def current_binding(self, manifest): return binding(manifest)
        async def consume(self, authorization_ref, digest, *, at): return True

    fake = Fake()
    with pytest.raises(PermissionError):
        await run_yandex_mail_qualification(report, m, authorization(m), Gate(), fake)
    assert fake.calls == 0
