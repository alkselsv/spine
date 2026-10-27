# Issue #509 local qualification profile

This profile is a disposable, localhost-only Docker Compose environment for
synthetic qualification work. It has separate SOURCE and RESTORE networks,
PostgreSQL databases, Neo4j instances, Temporal data and original-storage
volumes. It does not connect to production and does not contain credentials.

The application Compose file in this directory is retained for the earlier
readiness evidence. Physical recovery now uses the production runtime Compose
root plus the explicit one-shot composition in `recovery_job.py`. Build its
tooling image with `Dockerfile.recovery`; mount the script and one immutable
opaque-reference profile into the job container. Concrete database settings,
passfiles and volume roots remain deployment bindings supplied out of band.

The job invokes the existing `RecoveryOperatorService`,
`CoordinatedBackupProfile`, `BackupRecoveryCoordinator`, physical PostgreSQL and
original-storage sources, `IsolatedRestoreVerifier` and reconciliation path. It
does not add an HTTP route, scheduler, promotion or cutover path. Identity and
target provisioning remains an operator action and is not performed by the
job.

## 2026-10-27 physical attempt

The authorized synthetic run is recorded in
`docs/qualification/issue-509-recovery-attempt-v2.json`. It failed closed before
durable quiescence and produced three immutable incomplete manifests. RESTORE
remained empty and was never served. The attempt therefore does not qualify
backup, restore, projection recovery or canary behavior.

The exercise confirmed that tenant-scoped PostgreSQL dumps require
`--enable-row-security`, persisted strict enums require explicit conversion,
the backup identity needs a reviewed relation inventory, and the current
filesystem profile needs initialization access. Do not rerun the recorded
profiles or reuse their degraded deployment-scope references. A new run needs a
new authorization, recovery set, deployment-scope reference and independent
evidence review.

A later authorized retry is recorded in
`docs/qualification/issue-509-recovery-attempt-v3.json`. It verified the
coherent physical backup, isolated restore, original-storage reconciliation and
tenant-scoped storage canary. Overall qualification remains incomplete because
Context Projection recovery, the restored-application canary and independent
final evidence review have not run.

## Start

Run from WSL2 or PowerShell with Docker Desktop's Linux engine. Use a fresh
process and generate values in memory; never commit an environment file:

```powershell
$newQualificationSecret = {
  $bytes = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
  [BitConverter]::ToString($bytes).Replace('-', '')
}
$env:SOURCE_OPERATOR_PASSWORD = & $newQualificationSecret
$env:SOURCE_MIGRATION_PASSWORD = & $newQualificationSecret
$env:SOURCE_RUNTIME_PASSWORD = & $newQualificationSecret
$env:SOURCE_DISPATCHER_PASSWORD = & $newQualificationSecret
$env:SOURCE_TEMPORAL_PASSWORD = & $newQualificationSecret
$env:RESTORE_OPERATOR_PASSWORD = & $newQualificationSecret
$env:RESTORE_MIGRATION_PASSWORD = & $newQualificationSecret
$env:RESTORE_RUNTIME_PASSWORD = & $newQualificationSecret
$env:RESTORE_DISPATCHER_PASSWORD = & $newQualificationSecret
$env:RESTORE_TEMPORAL_PASSWORD = & $newQualificationSecret
$env:SOURCE_NEO4J_PASSWORD = & $newQualificationSecret
$env:RESTORE_NEO4J_PASSWORD = & $newQualificationSecret
$env:SOURCE_NEO4J_ENCRYPTION_KEY = & $newQualificationSecret
$env:RESTORE_NEO4J_ENCRYPTION_KEY = & $newQualificationSecret

# Supply these values out-of-band for the approved test provider. Do not
# commit them or put them into the qualification report.
$env:SOURCE_LLM_PROVIDER = '<approved-provider>'
$env:SOURCE_LLM_MODEL = '<approved-model>'
$env:SOURCE_LLM_API_KEY = '<approved-secret-from-secret-manager>'
$env:SOURCE_EMBEDDING_PROVIDER = '<approved-provider>'
$env:SOURCE_EMBEDDING_MODEL = '<approved-embedding-model>'
$env:SOURCE_EMBEDDING_DIMENSIONS = '<approved-dimension>'
$env:SOURCE_COGNEE_RUN_ID = 'issue509-source-001'
$env:RESTORE_LLM_PROVIDER = '<approved-provider>'
$env:RESTORE_LLM_MODEL = '<approved-model>'
$env:RESTORE_LLM_API_KEY = '<approved-secret-from-secret-manager>'
$env:RESTORE_EMBEDDING_PROVIDER = '<approved-provider>'
$env:RESTORE_EMBEDDING_MODEL = '<approved-embedding-model>'
$env:RESTORE_EMBEDDING_DIMENSIONS = '<approved-dimension>'
$env:RESTORE_COGNEE_RUN_ID = 'issue509-restore-001'

docker --context desktop-linux compose -p spine509qual -f deploy/qualification/issue-509/compose.yaml up -d --build
```

Replace the angle-bracket values through the approved local secret mechanism;
the placeholders above are not runnable credentials. The command intentionally
fails closed if any required binding is absent.
The application images expose only the legacy `/health` and `/api/v1/health/*`
surface until a reviewed production composition is available; a green health
endpoint is not a qualification result.

## Stop and cleanup

Stopping is non-destructive:

```powershell
docker --context desktop-linux compose -p spine509qual -f deploy/qualification/issue-509/compose.yaml stop
```

Do not remove the named volumes until the qualification owner has exported the
approved metadata-only evidence. When cleanup is authorized, the exact profile
scope is:

```powershell
docker --context desktop-linux compose -p spine509qual -f deploy/qualification/issue-509/compose.yaml down
$expectedVolumes = @(
  'spine509qual_source_postgres', 'spine509qual_restore_postgres',
  'spine509qual_source_temporal_postgres', 'spine509qual_restore_temporal_postgres',
  'spine509qual_source_neo4j_data', 'spine509qual_source_neo4j_logs',
  'spine509qual_restore_neo4j_data', 'spine509qual_restore_neo4j_logs',
  'spine509qual_source_original_storage', 'spine509qual_restore_original_storage',
  'spine509qual_source_api_data', 'spine509qual_restore_api_data'
)
$ownedVolumes = @($expectedVolumes | ForEach-Object {
  $volumeJson = docker --context desktop-linux volume inspect $_ 2>$null
  if ($LASTEXITCODE -ne 0) {
    return
  }
  $volume = $volumeJson | ConvertFrom-Json
  if ($volume[0].Labels.'com.docker.compose.project' -ne 'spine509qual') {
    throw "Refusing to remove a foreign volume: $_"
  }
  $_
})
if ($ownedVolumes.Count -gt 0) {
  docker --context desktop-linux volume rm $ownedVolumes
}
```

The ownership check refuses a foreign volume before removal and skips volumes
that were never created. These commands target only the `spine509qual_*`
resources created by this profile; they must not be run against another Compose
project.
