import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { Link, useParams } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { createScopedApiClient } from '../../api/client';
import { apiQueryOptions } from '../../api/query';
import { useAuth } from '../../app/auth';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import { useScope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import { loadBatchStatus } from './upload-api';
import { mapBatchItemState } from './upload';
import styles from './DocumentsPage.module.css';

export function IngestionRunPage() {
  const auth = useAuth();
  const { scope, queryKey } = useScope();
  const { runId = '' } = useParams();
  const client = useMemo(
    () =>
      createScopedApiClient({
        baseUrl: window.location.origin,
        getBearerCredential: auth.getBearerCredential ?? (() => ''),
        scope,
      }),
    [auth.getBearerCredential, scope],
  );
  const runQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadBatchStatus(client, runId, signal),
      queryKey: [...queryKey('ingestion-run'), runId],
      staleTime: 0,
    }),
    enabled: Boolean(auth.getBearerCredential && runId),
    refetchInterval: (query) => (query.state.data?.terminal || query.state.error ? false : 750),
  });

  const scopeSearch = new URLSearchParams({
    workspace: scope.workspaceId,
    environment: scope.environmentId,
  }).toString();

  if (!auth.getBearerCredential) return <StatusPanel state="degraded" />;
  if (runQuery.isPending) return <StatusPanel state="loading" />;
  if (runQuery.error) {
    return (
      <section className={styles.runPage} aria-labelledby="ingestion-run-heading">
        <h2 id="ingestion-run-heading">{ru.page.runDetails}</h2>
        <ApiErrorPanel error={runQuery.error} />
        <div className={styles.uploadActions}>
          <button className={styles.secondaryButton} onClick={() => void runQuery.refetch()} type="button">
            {ru.documents.upload.resumeObservation}
          </button>
        </div>
      </section>
    );
  }
  if (!runQuery.data) return <StatusPanel state="failed" />;

  return (
    <section className={styles.runPage} aria-labelledby="ingestion-run-heading">
      <div className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>{ru.page.eyebrow}</p>
          <h2 id="ingestion-run-heading">{ru.page.runDetails}</h2>
          <p className={styles.lede}>{ru.documents.upload.runDescription.replace('{runId}', runQuery.data.run_id)}</p>
        </div>
        <Link
          className={styles.secondaryButton}
          to={{ pathname: '/knowledge/documents', search: `?${scopeSearch}` }}
        >
          {ru.documents.backToList}
        </Link>
      </div>

      <p className={styles.uploadBatchStatus}>{batchStatusLabel(runQuery.data.status)}</p>
      <ul className={styles.runItemList} aria-label={ru.documents.upload.runItems}>
        {runQuery.data.items.map((item) => (
          <RunItem key={item.item_id} item={item} />
        ))}
      </ul>
    </section>
  );
}

function RunItem({ item }: { item: components['schemas']['BatchIngestionItemResponse'] }) {
  const state = mapBatchItemState(item);
  const label = stateLabel(item, state);
  return (
    <li className={styles.runItem}>
      <div>
        <strong>{item.input_identity}</strong>
        <span>{ru.documents.upload.runItem.replace('{ordinal}', String(item.input_ordinal))}</span>
      </div>
      <div>
        <strong>{label}</strong>
        <span>{stateDescription(item, state)}</span>
        {item.warnings.length > 0 ? <span>{ru.documents.upload.warning}</span> : null}
        {item.retryable ? <span>{ru.documents.upload.retryable}</span> : null}
      </div>
    </li>
  );
}

function stateLabel(
  item: components['schemas']['BatchIngestionItemResponse'],
  state: ReturnType<typeof mapBatchItemState>,
): string {
  if (state === 'processing' && item.item_state === 'projection_requested') return ru.documents.states.indexing;
  if (state === 'processing' && item.item_state === 'processing') return ru.documents.states.parsing;
  return ru.documents.upload[state];
}

function stateDescription(
  item: components['schemas']['BatchIngestionItemResponse'],
  state: ReturnType<typeof mapBatchItemState>,
): string {
  if (state === 'accepted') return ru.documents.upload.acceptedDescription;
  if (state === 'processing') {
    return item.item_state === 'projection_requested'
      ? ru.documents.upload.indexingDescription
      : ru.documents.upload.processingDescription;
  }
  if (state === 'ready') return ru.documents.upload.readyDescription;
  if (state === 'cancelled') return ru.documents.upload.serverCancelledDescription;
  if (state === 'failed' || state === 'rejected') return ru.documents.upload.serverFailureDescription;
  return '';
}

function batchStatusLabel(status: components['schemas']['PublicBatchStatus']): string {
  if (status === 'processing') return ru.documents.upload.processing;
  if (status === 'succeeded') return ru.documents.upload.ready;
  if (status === 'partial') return ru.documents.upload.partial;
  if (status === 'failed') return ru.documents.upload.failed;
  return ru.documents.upload.cancelled;
}
