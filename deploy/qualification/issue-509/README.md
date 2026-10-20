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

## Start

Run from WSL2 or PowerShell with Docker Desktop's Linux engine. Use a fresh
process and generate values in memory; never commit an environment file:

```powershell
$newQualificationSecret = { [BitConverter]::ToString([byte[]](1..32 | ForEach-Object { Get-Random -Maximum 256 })).Replace('-', '') }
$env:SOURCE_OPERATOR_PASSWORD = & $newQualificationSecret
$env:SOURCE_MIGRATION_PASSWORD = & $newQualificationSecret
$env:SOURCE_RUNTIME_PASSWORD = & $newQualificationSecret
$env:SOURCE_DISPATCHER_PASSWORD = & $newQualificationSecret
$env:SOURCE_BACKUP_PASSWORD = & $newQualificationSecret
$env:SOURCE_RESTORE_PASSWORD = & $newQualificationSecret
$env:SOURCE_VERIFIER_PASSWORD = & $newQualificationSecret
$env:SOURCE_TEMPORAL_PASSWORD = & $newQualificationSecret
$env:RESTORE_OPERATOR_PASSWORD = & $newQualificationSecret
$env:RESTORE_MIGRATION_PASSWORD = & $newQualificationSecret
$env:RESTORE_RUNTIME_PASSWORD = & $newQualificationSecret
$env:RESTORE_DISPATCHER_PASSWORD = & $newQualificationSecret
$env:RESTORE_BACKUP_PASSWORD = & $newQualificationSecret
$env:RESTORE_RESTORE_PASSWORD = & $newQualificationSecret
$env:RESTORE_VERIFIER_PASSWORD = & $newQualificationSecret
$env:RESTORE_TEMPORAL_PASSWORD = & $newQualificationSecret
$env:SOURCE_NEO4J_PASSWORD = & $newQualificationSecret
$env:RESTORE_NEO4J_PASSWORD = & $newQualificationSecret

docker --context desktop-linux compose -p spine509qual -f deploy/qualification/issue-509/compose.yaml up -d --build
```

The command intentionally fails closed if any credential variable is absent.
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
docker --context desktop-linux volume rm spine509qual_source_postgres spine509qual_restore_postgres spine509qual_source_neo4j_data spine509qual_source_neo4j_logs spine509qual_restore_neo4j_data spine509qual_restore_neo4j_logs spine509qual_source_original_storage spine509qual_restore_original_storage spine509qual_source_api_data spine509qual_restore_api_data spine509qual_backup_artifacts
```

These commands target only the `spine509qual_*` resources created by this
profile. They must not be run against another Compose project.
