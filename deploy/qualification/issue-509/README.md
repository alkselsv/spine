# Issue #509 local qualification profile

This profile is a disposable, localhost-only Docker Compose environment for
synthetic qualification work. It has separate SOURCE and RESTORE networks,
PostgreSQL databases, Neo4j instances, Temporal data and original-storage
volumes. It does not connect to production and does not contain credentials.

The profile deliberately does not claim a complete Spine production runtime.
The repository currently has no reviewed deployment composition root for the
PostgreSQL-backed API/Temporal worker graph and no reviewed operator entrypoint
that binds `CoordinatedBackupProfile` and `IsolatedRestoreVerifier` to this
deployment. Those are implementation gaps, not reasons to substitute ad-hoc
backup commands or fake evidence.

The Compose profile uses the existing `spine-db-bootstrap` and
`spine-db-migrate` entrypoints. It intentionally does not create backup,
restore, verifier or AccessPolicy identities: no reviewed coordinator currently
consumes those identities in this deployment.

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
  $volume = docker --context desktop-linux volume inspect $_ 2>$null | ConvertFrom-Json
  if ($null -eq $volume -or $volume[0].Labels.'com.docker.compose.project' -ne 'spine509qual') {
    throw "Refusing to remove a missing or foreign volume: $_"
  }
  $_
})
docker --context desktop-linux volume rm $ownedVolumes
```

The ownership check refuses a missing or foreign volume before removal. These
commands target only the `spine509qual_*` resources created by this profile;
they must not be run against another Compose project.
