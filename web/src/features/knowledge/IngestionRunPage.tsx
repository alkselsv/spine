import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { Link, useParams, useSearchParams } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { createScopedApiClient, SpineApiError } from '../../api/client';
import { apiQueryOptions } from '../../api/query';
import { useAuth } from '../../app/auth';
import { ApiErrorPanel, StatusPanel, type InterfaceState } from '../../app/status';
import { useScope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import { OperationalCorrelationPanel } from '../diagnostics/OperationalCorrelationPanel';
import { loadBatchStatus } from './upload-api';
import { ProcessingCommandAction } from './ProcessingCommandAction';
import {
  loadProcessingHistory,
  loadRunHistory,
  type HistoryQuery,
  type ProcessingHistoryCollection,
  type RunHistoryCollection,
} from './run-api';
import { mapIngestionItemState, type IngestionItemDisplayState } from './run-state';
import styles from './DocumentsPage.module.css';

const POLL_INTERVAL_MS = 750;

type BatchItem = components['schemas']['BatchIngestionItemResponse'];
type ProcessingHistoryItem = components['schemas']['DocumentProcessingHistoryResponse'];
type RunItem = BatchItem | ProcessingHistoryItem;

export interface RunQueryState extends HistoryQuery {
  processingCursor?: string;
}

export function parseRunQuery(search: string): RunQueryState {
  const parameters = new URLSearchParams(search);
  const cursor = readCursor(parameters.get('cursor'));
  const processingCursor = readCursor(parameters.get('processing_cursor'));
  return {
    ...(cursor ? { cursor } : {}),
    ...(processingCursor ? { processingCursor } : {}),
  };
}

export function updateRunQuery(search: string, next: RunQueryState): string {
  const parameters = new URLSearchParams(search);
  parameters.delete('cursor');
  parameters.delete('processing_cursor');
  if (next.cursor) parameters.set('cursor', next.cursor);
  if (next.processingCursor) parameters.set('processing_cursor', next.processingCursor);
  return parameters.toString();
}

export function IngestionRunPage() {
  const auth = useAuth();
  const { scope, queryKey } = useScope();
  const { runId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryState = parseRunQuery(`?${searchParams.toString()}`);
  const client = useMemo(
    () =>
      createScopedApiClient({
        baseUrl: window.location.origin,
        getBearerCredential: auth.getBearerCredential ?? (() => ''),
        scope,
      }),
    [auth.getBearerCredential, scope],
  );
  const runHistoryQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadRunHistory(client, queryState, signal),
      queryKey: [...queryKey('ingestion-run-history'), queryState.cursor],
    }),
    enabled: Boolean(auth.getBearerCredential),
  });
  const processingHistoryQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) =>
        loadProcessingHistory(client, { cursor: queryState.processingCursor }, signal),
      queryKey: [...queryKey('document-processing-history'), queryState.processingCursor],
    }),
    enabled: Boolean(auth.getBearerCredential),
  });
  const runQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadBatchStatus(client, runId ?? '', signal),
      queryKey: [...queryKey('ingestion-run'), runId],
      staleTime: 0,
    }),
    enabled: Boolean(auth.getBearerCredential && runId),
    refetchInterval: (query) =>
      query.state.data?.terminal || query.state.error ? false : POLL_INTERVAL_MS,
    refetchOnMount: (query) => !query.state.data?.terminal,
  });

  if (!auth.getBearerCredential) return <StatusPanel state="degraded" />;

  const currentSearch = searchParams.toString();

  return (
    <section className={styles.runPage} aria-labelledby="runs-heading">
      <div className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>{ru.page.eyebrow}</p>
          <h2 id="runs-heading">{ru.documents.run.title}</h2>
          <p className={styles.lede}>{ru.documents.run.description}</p>
        </div>
        <Link
          className={styles.secondaryButton}
          to={{ pathname: '/knowledge/documents', search: currentSearch }}
        >
          {ru.documents.backToList}
        </Link>
      </div>

      {runId ? (
        <RunDetails
          client={client}
          currentSearch={currentSearch}
          diagnosticsQueryKey={queryKey}
          queryKey={queryKey('document-processing-history')}
          query={runQuery}
          scope={scope}
        />
      ) : null}

      <RunHistorySection
        currentSearch={currentSearch}
        onNextPage={() => {
          if (!runHistoryQuery.data?.next_cursor) return;
          setSearchParams(
            updateRunQuery(currentSearch, {
              ...queryState,
              cursor: runHistoryQuery.data.next_cursor,
            }),
          );
        }}
        query={runHistoryQuery}
      />
      <ProcessingHistorySection
        currentSearch={currentSearch}
        onNextPage={() => {
          if (!processingHistoryQuery.data?.next_cursor) return;
          setSearchParams(
            updateRunQuery(currentSearch, {
              ...queryState,
              processingCursor: processingHistoryQuery.data.next_cursor,
            }),
          );
        }}
        query={processingHistoryQuery}
      />
    </section>
  );
}

function RunDetails({
  client,
  currentSearch,
  diagnosticsQueryKey,
  queryKey,
  query,
  scope,
}: {
  client: ReturnType<typeof createScopedApiClient>;
  currentSearch: string;
  diagnosticsQueryKey: (resource: string) => readonly unknown[];
  queryKey: readonly unknown[];
  query: ReturnType<typeof useQuery<components['schemas']['BatchIngestionStatusResponse'], SpineApiError>>;
  scope: { workspaceId: string; environmentId: string };
}) {
  if (query.isPending) {
    return (
      <section className={styles.runSection} aria-labelledby="run-detail-heading">
        <h3 id="run-detail-heading">{ru.documents.run.detailTitle}</h3>
        <StatusPanel state="loading" />
      </section>
    );
  }
  if (query.error && (!query.data || query.error.kind === 'authorization')) {
    return (
      <section className={styles.runSection} aria-labelledby="run-detail-heading">
        <h3 id="run-detail-heading">{ru.documents.run.detailTitle}</h3>
        <ApiErrorPanel error={query.error} />
        <button className={styles.secondaryButton} onClick={() => void query.refetch()} type="button">
          {ru.documents.retry}
        </button>
      </section>
    );
  }
  if (!query.data) {
    return (
      <section className={styles.runSection} aria-labelledby="run-detail-heading">
        <h3 id="run-detail-heading">{ru.documents.run.detailTitle}</h3>
        <StatusPanel state="failed" />
      </section>
    );
  }

  return (
    <section className={styles.runSection} aria-labelledby="run-detail-heading">
      <div className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>{ru.page.eyebrow}</p>
          <h3 id="run-detail-heading">{ru.documents.run.detailTitle}</h3>
          <p className={styles.lede}>{ru.documents.run.detailDescription}</p>
        </div>
        <Link
          className={styles.secondaryButton}
          to={{ pathname: '/runs', search: currentSearch }}
        >
          {ru.documents.run.historyTitle}
        </Link>
      </div>

      {query.error ? (
        <>
          <ApiErrorPanel error={query.error} />
          <StatusPanel state="stale" />
          <button className={styles.secondaryButton} onClick={() => void query.refetch()} type="button">
            {ru.documents.retry}
          </button>
        </>
      ) : null}
      <dl className={styles.runMeta}>
        <MetaEntry label={ru.documents.run.identity} value={query.data.run_id} />
        <MetaEntry label={ru.documents.run.scopeWorkspace} value={scope.workspaceId} />
        <MetaEntry label={ru.documents.run.scopeEnvironment} value={scope.environmentId} />
        <MetaEntry label={ru.documents.run.status} value={batchStatusLabel(query.data.status)} />
        <MetaEntry label={ru.documents.run.runState} value={runStateLabel(query.data.run_state)} />
      </dl>

      <OperationalCorrelationPanel
        client={client}
        queryKey={diagnosticsQueryKey}
        referenceId={query.data.run_id}
        referenceKind="run"
        scope={scope}
      />

      <ul className={styles.runItemList} aria-label={ru.documents.upload.runItems}>
        {query.data.items.map((item) => (
          <RunItem
            client={client}
            currentSearch={currentSearch}
            item={item}
            key={item.item_id}
            queryKey={queryKey}
            runId={query.data.run_id}
          />
        ))}
      </ul>
    </section>
  );
}

function RunHistorySection({
  currentSearch,
  onNextPage,
  query,
}: {
  currentSearch: string;
  onNextPage: () => void;
  query: ReturnType<typeof useQuery<RunHistoryCollection, SpineApiError>>;
}) {
  if (query.error?.kind === 'authorization') {
    return (
      <section className={styles.runSection} aria-labelledby="run-history-heading">
        <h3 id="run-history-heading">{ru.documents.run.historyTitle}</h3>
        <ApiErrorPanel error={query.error} />
      </section>
    );
  }
  const state = collectionState(query.data, query.error, query.isStale);
  return (
    <section className={styles.runSection} aria-labelledby="run-history-heading">
      <h3 id="run-history-heading">{ru.documents.run.historyTitle}</h3>
      {query.error ? (
        <>
          <ApiErrorPanel error={query.error} />
          <button className={styles.secondaryButton} onClick={() => void query.refetch()} type="button">
            {ru.documents.retry}
          </button>
        </>
      ) : null}
      {query.isPending ? <StatusPanel state="loading" /> : null}
      {query.data && state === 'stale' ? <StatusPanel state="stale" /> : null}
      {query.data?.items.length === 0 ? <StatusPanel state="empty" /> : null}
      {query.data && query.data.items.length > 0 ? (
        <>
          <div className={styles.tableFrame}>
            <table className={styles.table}>
              <caption className={styles.visuallyHidden}>{ru.documents.run.historyTitle}</caption>
              <thead>
                <tr>
                  <th scope="col">{ru.documents.run.identity}</th>
                  <th scope="col">{ru.documents.run.status}</th>
                  <th scope="col">{ru.documents.run.requestedAt}</th>
                  <th scope="col">{ru.documents.run.itemCount.replace('{count}', '')}</th>
                  <th scope="col"><span className={styles.visuallyHidden}>{ru.documents.run.open}</span></th>
                </tr>
              </thead>
              <tbody>
                {query.data.items.map((run) => (
                  <tr key={run.run_id}>
                    <td><code>{run.run_id}</code></td>
                    <td>{batchStatusLabel(run.status)}</td>
                    <td>{formatDate(run.requested_at)}</td>
                    <td>{ru.documents.run.itemCount.replace('{count}', String(run.items.length))}</td>
                    <td>
                      <Link
                        className={styles.rowLink}
                        to={{ pathname: `/runs/${run.run_id}`, search: currentSearch }}
                      >
                        {ru.documents.run.open}
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {query.data.next_cursor ? (
            <button className={styles.secondaryButton} onClick={onNextPage} type="button">
              {ru.documents.run.nextPage}
            </button>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

function ProcessingHistorySection({
  currentSearch,
  onNextPage,
  query,
}: {
  currentSearch: string;
  onNextPage: () => void;
  query: ReturnType<typeof useQuery<ProcessingHistoryCollection, SpineApiError>>;
}) {
  if (query.error?.kind === 'authorization') {
    return (
      <section className={styles.runSection} aria-labelledby="processing-history-heading">
        <h3 id="processing-history-heading">{ru.documents.run.processingHistoryTitle}</h3>
        <ApiErrorPanel error={query.error} />
      </section>
    );
  }
  const state = collectionState(query.data, query.error, query.isStale);
  return (
    <section className={styles.runSection} aria-labelledby="processing-history-heading">
      <h3 id="processing-history-heading">{ru.documents.run.processingHistoryTitle}</h3>
      {query.error ? (
        <>
          <ApiErrorPanel error={query.error} />
          <button className={styles.secondaryButton} onClick={() => void query.refetch()} type="button">
            {ru.documents.retry}
          </button>
        </>
      ) : null}
      {query.isPending ? <StatusPanel state="loading" /> : null}
      {query.data && state === 'stale' ? <StatusPanel state="stale" /> : null}
      {query.data?.items.length === 0 ? <StatusPanel state="empty" /> : null}
      {query.data && query.data.items.length > 0 ? (
        <>
          <div className={styles.tableFrame}>
            <table className={styles.table}>
              <caption className={styles.visuallyHidden}>{ru.documents.run.processingHistoryTitle}</caption>
              <thead>
                <tr>
                  <th scope="col">{ru.documents.run.file}</th>
                  <th scope="col">{ru.documents.run.identity}</th>
                  <th scope="col">{ru.documents.run.status}</th>
                  <th scope="col">{ru.documents.run.attempt.replace('{count}', '')}</th>
                  <th scope="col"><span className={styles.visuallyHidden}>{ru.documents.run.open}</span></th>
                </tr>
              </thead>
              <tbody>
                {query.data.items.map((item) => (
                  <tr key={item.item_id}>
                    <td>{item.input_identity}</td>
                    <td><code>{item.run_id}</code></td>
                    <td>{itemStateLabel(item, mapIngestionItemState(item))}</td>
                    <td>{ru.documents.run.attempt.replace('{count}', String(item.attempt))}</td>
                    <td>
                      <Link
                        className={styles.rowLink}
                        to={{ pathname: `/runs/${item.run_id}`, search: currentSearch }}
                      >
                        {ru.documents.run.open}
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {query.data.next_cursor ? (
            <button className={styles.secondaryButton} onClick={onNextPage} type="button">
              {ru.documents.run.nextPage}
            </button>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

function RunItem({
  client,
  currentSearch,
  item,
  queryKey,
  runId,
}: {
  client: ReturnType<typeof createScopedApiClient>;
  currentSearch: string;
  item: RunItem;
  queryKey: readonly unknown[];
  runId: string;
}) {
  const state = mapIngestionItemState(item);
  const retryable = itemRetryable(item);
  return (
    <li className={styles.runItem}>
      <div className={styles.runItemHeader}>
        <div>
          <strong>{item.input_identity}</strong>
          <span>{ru.documents.upload.runItem.replace('{ordinal}', String(item.input_ordinal))}</span>
        </div>
        <span className={styles.statusPill}>{itemStateLabel(item, state)}</span>
      </div>
      <dl className={styles.runItemMeta}>
        <MetaEntry label={ru.documents.run.acceptance} value={acceptanceLabel(item.acceptance)} />
        <MetaEntry label={ru.documents.run.processing} value={processingLabel(item.processing)} />
        <MetaEntry label="Состояние" value={ingestionStatusLabel(item.status)} />
        <MetaEntry label={ru.documents.run.attempt.replace('{count}', '')} value={String(item.attempt)} />
        <MetaEntry label={ru.documents.run.coverage.replace('{value}', '')} value={coverageLabel(item.coverage)} />
      </dl>
      <div className={styles.runDiagnostics}>
        {item.warnings.map((warning) => (
          <span key={warning.warning_id}>
            {ru.documents.run.warning
              .replace('{code}', warning.code)
              .replace('{stage}', warning.stage)}{' '}
            · {ru.documents.run.warningId}: <code>{warning.warning_id}</code>
          </span>
        ))}
        {item.failures.map((failure) => (
          <span key={failure.failure_id}>
            {ru.documents.run.failure.replace('{code}', failure.code).replace('{stage}', failure.stage)} ·{' '}
            {failure.retryable ? ru.documents.run.failureRetryable : ru.documents.run.failureFinal} ·{' '}
            {ru.documents.run.failureId}: <code>{failure.failure_id}</code>
          </span>
        ))}
        {item.retryable ? <span>{ru.documents.run.retryable}</span> : null}
        <span>
          {ru.documents.run.nextAction.replace('{action}', nextActionLabel(state, retryable))}
        </span>
        {item.processing_receipt ? <ReceiptView receipt={item.processing_receipt} /> : null}
      </div>
      {item.retryable && item.source_object_id && item.source_revision_id
        ? item.failures.filter((failure) => failure.retryable).map((failure) => (
            <ProcessingCommandAction
              client={client}
              currentRevisionId={item.source_revision_id!}
              key={failure.failure_id}
              mode={{ kind: 'retry', failureId: failure.failure_id, retryable: failure.retryable }}
              processingReceiptId={item.processing_receipt?.receipt_id}
              queryKey={queryKey}
              sourceObjectId={item.source_object_id!}
              sourceRevisionId={item.source_revision_id!}
            />
          ))
        : null}
      <Link
        className={styles.rowLink}
        to={{ pathname: `/runs/${runId}`, search: currentSearch }}
      >
        {ru.documents.run.open}
      </Link>
    </li>
  );
}

function itemRetryable(item: RunItem): boolean {
  return item.retryable;
}

function ReceiptView({ receipt }: { receipt: components['schemas']['IngestionReceiptResponse'] }) {
  return (
    <span>
      {ru.documents.run.parser.replace('{name}', receipt.parser_name)} ·{' '}
      {ru.documents.run.receipt.replace('{id}', receipt.receipt_id)}
      {receipt.artifact_id ? ` · ${ru.documents.run.artifact.replace('{id}', receipt.artifact_id)}` : ''}
    </span>
  );
}

function MetaEntry({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function collectionState<T extends { items: readonly unknown[] }>(
  data: T | undefined,
  error: SpineApiError | null,
  isStale: boolean,
): InterfaceState {
  if (data && (error || isStale)) return 'stale';
  if (error) return apiErrorState(error);
  if (!data || data.items.length === 0) return 'empty';
  return 'ready';
}

function apiErrorState(error: SpineApiError): InterfaceState {
  if (error.kind === 'authorization') return 'permissionDenied';
  if (error.kind === 'validation') return 'validationFailed';
  if (error.kind === 'unavailable') return 'degraded';
  return 'failed';
}

function readCursor(value: string | null): string | undefined {
  return value && value.length <= 768 ? value : undefined;
}

function itemStateLabel(_item: RunItem, state: IngestionItemDisplayState): string {
  if (state === 'parsing') return ru.documents.run.states.parsing;
  if (state === 'indexing') return ru.documents.run.states.indexing;
  return ru.documents.run.states[state];
}

function nextActionLabel(state: IngestionItemDisplayState, retryable: boolean): string {
  if (state === 'failed') {
    return retryable
      ? ru.documents.run.nextActions.failedRetryable
      : ru.documents.run.nextActions.failedFinal;
  }
  return ru.documents.run.nextActions[state];
}

function acceptanceLabel(value: components['schemas']['PublicAcceptanceStatus']): string {
  return ru.documents.run.acceptanceStates[value];
}

function processingLabel(value: components['schemas']['PublicProcessingStatus']): string {
  return ru.documents.run.processingStates[value];
}

function ingestionStatusLabel(value: components['schemas']['PublicIngestionStatus']): string {
  return ru.documents.run.ingestionStates[value];
}

function coverageLabel(value: string | null | undefined): string {
  if (!value) return ru.documents.unknown;
  const labels: Record<string, string> = ru.documents.run.coverageStates;
  return labels[value] ?? ru.documents.run.unknownState;
}

function runStateLabel(value: string): string {
  const labels: Record<string, string> = ru.documents.run.runStates;
  return labels[value] ?? ru.documents.run.unknownState;
}

function batchStatusLabel(status: components['schemas']['PublicBatchStatus']): string {
  return ru.documents.run.batchStates[status];
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return ru.documents.unknown;
  return new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}
