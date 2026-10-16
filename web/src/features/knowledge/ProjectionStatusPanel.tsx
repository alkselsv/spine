import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef } from 'react';
import type { components } from '../../../api/generated/schema';

import { createScopedApiClient } from '../../api/client';
import { apiQueryOptions } from '../../api/query';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import { ru } from '../../i18n/resources';
import { formatDate } from './format';
import styles from './DocumentDetailPage.module.css';

type ProjectionStatus = components['schemas']['ProjectionStatusResponse'];
type HealthSummary = components['schemas']['HealthSummaryResponse'];
type RecoveryAction = components['schemas']['ProjectionRecoveryAction'];
type ReconciliationOutcome = NonNullable<ProjectionStatus['reconciliation']>['outcome'] | undefined;
type HealthOperation = NonNullable<HealthSummary['last_operation']>;
type ProjectionOperationStatus = NonNullable<ProjectionStatus['last_operation']>['status'];

export function ProjectionStatusPanel({
  client,
  indexingState,
  projectionKind,
  queryKey,
}: {
  client: ReturnType<typeof createScopedApiClient>;
  indexingState: components['schemas']['IngestionItemState'] | null | undefined;
  projectionKind: string;
  queryKey: (resource: string) => readonly unknown[];
}) {
  const queryClient = useQueryClient();
  const commandKeys = useRef(new Map<RecoveryAction, string>());
  const statusKey = [...queryKey('projection-status'), projectionKind];
  const statusQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadProjectionStatus(client, projectionKind, signal),
      queryKey: statusKey,
    }),
  });
  const healthQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadHealthSummary(client, signal),
      queryKey: queryKey('health-summary'),
    }),
  });
  const recoveryMutation = useMutation({
    mutationFn: (action: RecoveryAction) =>
      requestProjectionRecovery(client, projectionKind, action, recoveryKey(commandKeys.current, action)),
    onSuccess: (_result, action) => {
      commandKeys.current.delete(action);
      return queryClient.invalidateQueries({ queryKey: statusKey });
    },
  });

  return (
    <section className={`${styles.card} ${styles.cardWide}`} aria-labelledby="projection-status-heading">
      <div className={styles.statusHeader}>
        <div>
          <h3 id="projection-status-heading">{ru.documents.projectionStatusTitle}</h3>
          <p className={styles.cardDescription}>{ru.documents.projectionStatusDescription}</p>
        </div>
        <span className={styles.projectionKindValue}>{ru.documents.projectionKind}: <code>{projectionKind}</code></span>
        <button
          className={styles.refreshButton}
          disabled={statusQuery.isFetching || healthQuery.isFetching}
          onClick={() => {
            void statusQuery.refetch();
            void healthQuery.refetch();
          }}
          type="button"
        >
          {statusQuery.isFetching || healthQuery.isFetching ? ru.documents.refreshingStatus : ru.documents.refreshStatus}
        </button>
      </div>

      {statusQuery.isPending ? <StatusPanel state="loading" /> : null}
      {statusQuery.error ? <ApiErrorPanel error={statusQuery.error} /> : null}
      {statusQuery.data ? (
        <>
          {statusQuery.isStale ? <p className={styles.operationalNotice} role="status">{ru.documents.stalePanelData}</p> : null}
          <dl className={styles.details}>
            <DetailValue label={ru.documents.publication} value={publicationLabel(statusQuery.data.publication_status)} />
            <DetailValue label={ru.documents.freshness} value={freshnessLabel(statusQuery.data.freshness)} />
            <DetailValue label={ru.documents.projectionHealth} value={projectionHealthLabel(statusQuery.data.health)} />
            <DetailValue label={ru.documents.indexing} value={indexingLabel(indexingState)} />
            <DetailValue label={ru.documents.snapshot} value={statusQuery.data.snapshot_id} code />
            <DetailValue label={ru.documents.canonicalBoundary} value={statusQuery.data.canonical_boundary_id} code />
            <DetailValue label={ru.documents.asOf} value={statusQuery.data.as_of ? `${formatDate(statusQuery.data.as_of.boundary_time)} · ${ru.documents.generation} ${statusQuery.data.as_of.accepted_through_generation}` : null} />
            <DetailValue label={ru.documents.activationGeneration} value={statusQuery.data.activation_generation == null ? null : String(statusQuery.data.activation_generation)} />
            <DetailValue label={ru.documents.projectionConfigVersion} value={configVersionLabel(statusQuery.data)} code />
            <DetailValue label={ru.documents.drift} value={statusQuery.data.drift_count === undefined || statusQuery.data.drift_count === null ? null : String(statusQuery.data.drift_count)} />
            <DetailValue label={ru.documents.reconciliation} value={reconciliationLabel(statusQuery.data.reconciliation?.outcome)} />
            <DetailValue label={ru.documents.reconciliationCheckedAt} value={statusQuery.data.reconciliation?.checked_at ? formatDate(statusQuery.data.reconciliation.checked_at) : null} />
            <DetailValue label={ru.documents.reconciliationResult} value={statusQuery.data.reconciliation?.result_id} code />
            <DetailValue label={ru.documents.lastOperation} value={operationLabel(statusQuery.data.last_operation)} />
            <DetailValue label={ru.documents.operationCompletedAt} value={statusQuery.data.last_operation?.completed_at ? formatDate(statusQuery.data.last_operation.completed_at) : null} />
            <DetailValue label={ru.documents.recoveryState} value={recoveryStateLabel(statusQuery.data.recovery_state)} />
            <DetailValue label={ru.documents.reasonCode} value={safeReasonLabel(statusQuery.data.reason_code)} />
          </dl>
          <RecoveryActions
            data={statusQuery.data}
            indexingState={indexingState}
            isPending={recoveryMutation.isPending}
            onRequest={(action) => recoveryMutation.mutate(action)}
          />
          {recoveryMutation.isError ? <ApiErrorPanel error={recoveryMutation.error} /> : null}
          {recoveryMutation.isSuccess ? <p className={styles.operationalNotice} role="status">{ru.documents.recoveryAccepted}</p> : null}
        </>
      ) : null}

      <section className={styles.healthSummary} aria-labelledby="health-summary-heading">
        <h4 id="health-summary-heading">{ru.documents.operationalHealth}</h4>
        <p className={styles.cardDescription}>{ru.documents.operationalHealthDescription}</p>
        {healthQuery.isPending ? <StatusPanel state="loading" /> : null}
        {healthQuery.error ? <ApiErrorPanel error={healthQuery.error} /> : null}
        {healthQuery.data ? (
          <dl className={styles.details}>
            <DetailValue label={ru.documents.operationalSignal} value={healthLabel(healthQuery.data.status)} />
            <DetailValue label={ru.documents.authorization} value={healthLabel(healthQuery.data.authorization.status)} />
            <DetailValue label={ru.documents.ingestion} value={healthLabel(healthQuery.data.ingestion.status)} />
            <DetailValue label={ru.documents.publication} value={healthLabel(healthQuery.data.publication.status)} />
            <DetailValue label={ru.documents.configurationVersion} value={healthQuery.data.configuration_version} code />
            <DetailValue label={ru.documents.buildVersion} value={healthQuery.data.build_version} code />
            <DetailValue label={ru.documents.lastOperation} value={healthOperationLabel(healthQuery.data.last_operation?.operation, healthQuery.data.last_operation?.status)} />
          </dl>
        ) : null}
      </section>
    </section>
  );
}

export async function loadProjectionStatus(
  client: ReturnType<typeof createScopedApiClient>,
  projectionKind: string,
  signal: AbortSignal,
): Promise<ProjectionStatus> {
  const result = await client.GET('/api/v1/projections/{projection_kind}/status', {
    params: { path: { projection_kind: projectionKind } },
    signal,
  });
  if (!result.data) throw new Error('The projection status response is unavailable.');
  return result.data.data;
}

export async function loadHealthSummary(
  client: ReturnType<typeof createScopedApiClient>,
  signal: AbortSignal,
): Promise<HealthSummary> {
  const result = await client.GET('/api/v1/health/summary', { signal });
  if (!result.data) throw new Error('The health summary response is unavailable.');
  return result.data.data;
}

export async function requestProjectionRecovery(
  client: ReturnType<typeof createScopedApiClient>,
  projectionKind: string,
  action: RecoveryAction,
  idempotencyKey: string,
) {
  const result = await client.POST('/api/v1/projections/{projection_kind}/recovery', {
    params: {
      header: { 'Idempotency-Key': idempotencyKey },
      path: { projection_kind: projectionKind },
    },
    body: { action },
  });
  if (!result.data) throw new Error('The projection recovery response is unavailable.');
  return result.data;
}

function recoveryKey(keys: Map<RecoveryAction, string>, action: RecoveryAction) {
  const existing = keys.get(action);
  if (existing) return existing;
  const next = `projection-recovery-${globalThis.crypto.randomUUID()}`;
  keys.set(action, next);
  return next;
}

function RecoveryActions({
  data,
  indexingState,
  isPending,
  onRequest,
}: {
  data: ProjectionStatus;
  indexingState: components['schemas']['IngestionItemState'] | null | undefined;
  isPending: boolean;
  onRequest: (action: RecoveryAction) => void;
}) {
  if (data.recovery_state === 'pending') {
    return <p className={styles.operationalNotice}>{ru.documents.recoveryPending}</p>;
  }
  const retryable = data.retryable || data.last_operation?.retryable;
  if (retryable) {
    return <button className={styles.recoveryButton} disabled={isPending} onClick={() => onRequest('retry')} type="button">{ru.documents.retryProjection}</button>;
  }
  const needsRebuild = data.publication_status === 'published' && (data.freshness === 'stale' || (data.drift_count ?? 0) > 0);
  if (needsRebuild) {
    return <button className={styles.recoveryButton} disabled={isPending} onClick={() => onRequest('rebuild')} type="button">{ru.documents.rebuildProjection}</button>;
  }
  const needsRecovery = data.health === 'degraded' || ['degraded', 'failed', 'interrupted'].includes(data.reconciliation?.outcome ?? '');
  if (needsRecovery) {
    return <button className={styles.recoveryButton} disabled={isPending} onClick={() => onRequest('recover')} type="button">{ru.documents.recoverProjection}</button>;
  }
  if (data.publication_status === 'unpublished' && indexingState && indexingState !== 'processed' && indexingState !== 'processing_failed') {
    return <p className={styles.operationalNotice}>{ru.documents.indexingInProgress}</p>;
  }
  if (data.publication_status === 'published' && data.freshness === 'current' && data.health === 'healthy') {
    return <p className={styles.operationalNotice}>{ru.documents.noRecoveryAction}</p>;
  }
  return <p className={styles.operationalNotice}>{ru.documents.noActionAvailable}</p>;
}

function DetailValue({ label, value, code = false }: { label: string; value: string | null | undefined; code?: boolean }) {
  return <div><dt>{label}</dt><dd className={styles.value}>{value ? (code ? <code>{value}</code> : value) : ru.documents.unknown}</dd></div>;
}

function publicationLabel(value: ProjectionStatus['publication_status']) {
  if (value === 'published') return ru.documents.filterOptions.publication_status.published;
  if (value === 'unpublished') return ru.documents.filterOptions.publication_status.unpublished;
  return ru.documents.filterOptions.publication_status.unavailable;
}

function freshnessLabel(value: ProjectionStatus['freshness']) {
  if (value === 'current') return ru.documents.current;
  if (value === 'stale') return ru.documents.states.stale;
  return ru.documents.unknown;
}

function projectionHealthLabel(value: ProjectionStatus['health']) {
  if (value === 'healthy') return ru.documents.healthy;
  if (value === 'degraded') return ru.documents.filterOptions.publication_health.degraded;
  return ru.documents.unknown;
}

function indexingLabel(value: string | null | undefined) {
  if (value === 'processed') return ru.documents.indexed;
  if (value === 'projection_requested' || value === 'processing') return ru.documents.states.indexing;
  if (value === 'processing_failed') return ru.documents.states.processingFailed;
  if (value) return ru.documents.indexingNotReady;
  return ru.documents.unknown;
}

function healthLabel(value: components['schemas']['HealthStatus']) {
  if (value === 'ready') return ru.documents.healthy;
  if (value === 'stale') return ru.documents.healthStale;
  if (value === 'degraded') return ru.documents.filterOptions.publication_health.degraded;
  return ru.documents.states.unavailable;
}

function reconciliationLabel(value: ReconciliationOutcome) {
  if (value === 'healthy') return ru.documents.healthy;
  if (value === 'degraded') return ru.documents.filterOptions.publication_health.degraded;
  if (value === 'failed') return ru.documents.states.processingFailed;
  if (value === 'interrupted') return ru.documents.interrupted;
  return ru.documents.unknown;
}

function operationLabel(value: ProjectionStatus['last_operation']) {
  if (!value) return null;
  const operation = value.operation === 'reconciliation' ? ru.documents.reconciliation : value.operation === 'rebuild' ? ru.documents.rebuild : value.operation === 'rollback' ? ru.documents.rollback : value.operation === 'catch_up' ? ru.documents.catchUp : value.operation === 'publish' ? ru.documents.publish : ru.documents.build;
  return `${operation} · ${operationStatusLabel(value.status)}${value.retryable ? ` · ${ru.documents.retryable}` : ''}`;
}

function healthOperationLabel(operation: HealthOperation['operation'] | undefined, status: HealthOperation['status'] | undefined) {
  if (!operation || !status) return null;
  const name = operation === 'authorization' ? ru.documents.authorization : operation === 'ingestion' ? ru.documents.ingestion : operation === 'publication' ? ru.documents.publication : ru.documents.reconciliation;
  return `${name} · ${operationStatusLabel(status)}`;
}

function operationStatusLabel(status: ProjectionOperationStatus | HealthOperation['status']) {
  if (status === 'accepted') return ru.documents.operationAccepted;
  if (status === 'running') return ru.documents.operationRunning;
  if (status === 'completed') return ru.documents.operationCompleted;
  if (status === 'failed') return ru.documents.operationFailed;
  if (status === 'stale') return ru.documents.operationStale;
  return ru.documents.operationUnavailable;
}

function recoveryStateLabel(value: ProjectionStatus['recovery_state']) {
  if (value === 'not_requested') return ru.documents.recoveryNotRequested;
  if (value === 'pending') return ru.documents.recoveryPending;
  if (value === 'succeeded') return ru.documents.recoverySucceeded;
  if (value === 'failed') return ru.documents.recoveryFailed;
  return ru.documents.states.unavailable;
}

function safeReasonLabel(value: ProjectionStatus['reason_code']) {
  if (!value) return null;
  const known: Record<string, string> = {
    'publication.unavailable': ru.documents.reasonUnavailable,
    active_publication_integrity_conflict: ru.documents.reasonIntegrityConflict,
  };
  return known[value] ?? ru.documents.safeReasonAvailable;
}

function configVersionLabel(data: ProjectionStatus) {
  const parts = [data.projection_config_version, data.projection_config_version_id].filter((part) => part != null);
  return parts.length ? parts.join(' · ') : null;
}
