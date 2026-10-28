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
tenant-scoped storage canary.

The subsequent provider-backed run is recorded in
`docs/qualification/issue-509-projection-recovery-attempt-v1.json`. It verified
physical Context Projection recovery, strict candidate retrieval lineage and
the restored application's fail-closed `unpublished` status. It also exposed
restore-time ACL and SECURITY DEFINER ownership gaps. Overall qualification
remains incomplete because an authorized application read through an active
publication was outside the approved no-activation scope, the restore procedure
still needs those gaps corrected, and independent final evidence review has not
run.

The recovered-application retry and its graph-runtime diagnosis are recorded in
`docs/qualification/issue-509-application-recovery-attempt-v2.json` and
`docs/qualification/issue-509-neo4j-runtime-compatibility-v1.json`. The retry
proved provider connectivity, then failed closed because a containerized runner
used Cognee's default multi-database `neo4j` handler against Community edition.

For the pinned Community image, use a host-side qualification runner with
Docker access and all of these bindings:

```text
GRAPH_DATABASE_PROVIDER=neo4j
GRAPH_DATASET_DATABASE_HANDLER=neo4j_community
NEO4J_COMMUNITY_IMAGE=spine/neo4j-cognee:5-community
NEO4J_ENCRYPTION_KEY=<dedicated-qualification-binding>
ENABLE_BACKEND_ACCESS_CONTROL=true
```

The community handler creates one container and volume per dataset and returns a
localhost Bolt address, so it must not be used from the existing containerized
qualification runner. Keep that runner only with Neo4j Enterprise or AuraDB,
where Cognee's default `neo4j` multi-database handler is supported. Never set
`ENABLE_BACKEND_ACCESS_CONTROL=false` as a workaround: that removes the required
dataset isolation and cannot produce qualification evidence. Run the no-provider
graph compatibility probe before requesting a new provider authorization, and
remove its synthetic dataset through Cognee's public handler lifecycle.

The third recovered-application attempt used this compatible placement and
completed the physical provider-backed pipeline, but the canonical handoff
failed closed with `projection.request.integrity_conflict`. The immutable result
is recorded in
`docs/qualification/issue-509-application-recovery-attempt-v3.json`. Do not
interpret physical completion as publication: the attempt committed no receipt,
snapshot, activation or active pointer, and it performed no application read.
The consumer now emits a content-free stage marker for resolution, preparation,
receipt, mapping or publication-completion conflicts. A future authorized retry
must capture that marker without exposing exception text or content.

The offline, no-provider diagnosis is recorded in
`docs/qualification/issue-509-application-recovery-diagnostic-v1.json`. It
reproduced the mapping refusal on an isolated clone and proved that the attempt's
qualification composition issued the wrong trusted persistence operation.
ProjectionReceiptCommand requires `projection_receipt`. Recovered-application
composition must use the production `_projection_receipt_context` helper and
must not issue `projection_result` for the result bridge, receipt builder or
canonical locator resolver. Run the existing worker regression that asserts the
helper's exact operation before requesting a provider-backed retry.

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
