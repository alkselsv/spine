import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Link } from 'react-router';

import { SpineApiError, type ApiScope, type ScopedApiClient } from '../../api/client';
import { apiQueryOptions } from '../../api/query';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import { ru } from '../../i18n/resources';
import {
  loadOperationalCorrelation,
  type CorrelationReferenceKind,
  type OperationalCorrelationView,
} from './operational-correlation';
import styles from './OperationalCorrelationPanel.module.css';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function OperationalCorrelationPanel({
  client,
  queryKey,
  referenceId,
  referenceKind,
  scope,
}: {
  client: ScopedApiClient;
  queryKey: (resource: string) => readonly unknown[];
  referenceId: string | null | undefined;
  referenceKind: CorrelationReferenceKind;
  scope: ApiScope;
}) {
  const [expanded, setExpanded] = useState(false);
  const [activeReference, setActiveReference] = useState({
    referenceId: referenceId ?? '',
    referenceKind,
  });
  useEffect(() => {
    setActiveReference({ referenceId: referenceId ?? '', referenceKind });
  }, [referenceId, referenceKind]);
  const hasReference = Boolean(activeReference.referenceId && UUID_PATTERN.test(activeReference.referenceId));
  const correlationQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) =>
        loadOperationalCorrelation(
          client,
          { ...activeReference, scope },
          signal,
        ),
      queryKey: [...queryKey('operational-correlation'), activeReference.referenceKind, activeReference.referenceId],
    }),
    enabled: expanded && hasReference,
    staleTime: 0,
  });
  const scopeSearch = new URLSearchParams({
    workspace: scope.workspaceId,
    environment: scope.environmentId,
  }).toString();
  const denied = correlationQuery.error instanceof SpineApiError &&
    (correlationQuery.error.kind === 'authorization' || correlationQuery.error.kind === 'authentication');
  const safeResult = denied ? null : correlationQuery.data;

  return (
    <section className={styles.panel} aria-labelledby="operational-correlation-heading">
      <div className={styles.header}>
        <div>
          <h3 id="operational-correlation-heading">{ru.diagnostics.title}</h3>
          <p className={styles.description}>{ru.diagnostics.description}</p>
        </div>
        <button
          aria-controls="operational-correlation-content"
          aria-expanded={expanded}
          className={styles.toggle}
          onClick={() => setExpanded((current) => !current)}
          type="button"
        >
          {expanded ? ru.diagnostics.close : ru.diagnostics.open}
        </button>
      </div>

      {expanded ? (
        <div id="operational-correlation-content">
          {!hasReference ? <p className={styles.notice} role="status">{ru.diagnostics.referenceUnavailable}</p> : null}
          {hasReference && correlationQuery.isPending ? <StatusPanel state="loading" /> : null}
          {correlationQuery.error ? <ApiErrorPanel error={correlationQuery.error} /> : null}
          {correlationQuery.error && safeResult ? <StatusPanel state="stale" /> : null}
          {correlationQuery.error ? (
            <button
              className={styles.retry}
              disabled={correlationQuery.isFetching}
              onClick={() => void correlationQuery.refetch()}
              type="button"
            >
              {correlationQuery.isFetching ? ru.documents.refreshingStatus : ru.documents.retry}
            </button>
          ) : null}
          {safeResult ? (
            <>
              <code className={styles.reference} aria-label={ru.diagnostics.reference}>
                {boundedReference(safeResult.view.reference_id)}
              </code>
              {activeReference.referenceId !== referenceId || activeReference.referenceKind !== referenceKind ? (
                <button
                  className={styles.retry}
                  onClick={() => setActiveReference({ referenceId: referenceId ?? '', referenceKind })}
                  type="button"
                >
                  {ru.diagnostics.backToOriginalReference}
                </button>
              ) : null}
              <CorrelationView
                onInspectDelivery={(deliveryId) => setActiveReference({ referenceId: deliveryId, referenceKind: 'delivery' })}
                view={safeResult.view}
              />
              <p className={styles.auditReference}>
                {ru.diagnostics.auditReference}: <code>{boundedReference(safeResult.authorizationAuditEventId)}</code>
                {' · '}{ru.diagnostics.requestTrace}: <code>{boundedReference(safeResult.requestTraceId)}</code>
              </p>
            </>
          ) : null}

          <nav className={styles.navigation} aria-label={ru.diagnostics.navigation}>
            <Link to={{ pathname: '/qa/runs', search: scopeSearch }}>{ru.diagnostics.qaRuns}</Link>
            <Link to={{ pathname: '/runs', search: scopeSearch }}>{ru.diagnostics.ingestionRuns}</Link>
            <Link to={{ pathname: '/knowledge/documents', search: scopeSearch }}>{ru.diagnostics.documents}</Link>
          </nav>
        </div>
      ) : null}
    </section>
  );
}

function CorrelationView({
  onInspectDelivery,
  view,
}: {
  onInspectDelivery: (deliveryId: string) => void;
  view: OperationalCorrelationView;
}) {
  if (view.coverage === 'unavailable') return <p className={styles.notice} role="status">{ru.diagnostics.unavailable}</p>;
  if (view.coverage === 'denied') return <StatusPanel state="permissionDenied" />;

  return (
    <div className={styles.content}>
      <p className={styles.coverage} role="status">
        {view.coverage === 'complete' ? ru.diagnostics.complete : ru.diagnostics.partial}
      </p>
      {view.missing_sources.length ? (
        <p className={styles.notice}>{ru.diagnostics.missingSources}: {view.missing_sources.map(sourceLabel).join(', ')}</p>
      ) : null}
      {view.conflict_codes.length ? (
        <p className={styles.notice}>{ru.diagnostics.conflicts}: {view.conflict_codes.map(conflictLabel).join(', ')}</p>
      ) : null}
      {view.records.length === 0 ? <StatusPanel state="empty" /> : null}
      <ul className={styles.records} aria-label={ru.diagnostics.records}>
        {view.records.map((record) => {
          const deliveryEventId = record.delivery_event_id;
          return (
            <li className={styles.record} key={`${record.source_kind}:${record.source_record_id}`}>
              <div className={styles.recordHeader}>
                <strong>{sourceLabel(record.source_kind)}</strong>
                <time dateTime={record.observed_at}>{formatTimestamp(record.observed_at)}</time>
              </div>
              <dl className={styles.recordDetails}>
                <Detail label={ru.diagnostics.sourceVersion} value={record.source_schema_version} />
                <Detail label={ru.diagnostics.sourceRecord} value={boundedReference(record.source_record_id)} code />
                {record.source_identity ? <Detail label={ru.diagnostics.sourceIdentity} value={record.source_identity} /> : null}
                {record.metric_measurement ? <Detail label={ru.diagnostics.measurement} value={record.metric_measurement} /> : null}
                {record.metric_coverage ? <Detail label={ru.diagnostics.measurementCoverage} value={record.metric_coverage} /> : null}
                {record.metric_value !== null && record.metric_value !== undefined ? <Detail label={ru.diagnostics.measurementValue} value={record.metric_value} /> : null}
                {record.metric_currency ? <Detail label={ru.diagnostics.currency} value={record.metric_currency} /> : null}
                {record.run_state ? <Detail label={ru.diagnostics.runState} value={record.run_state} /> : null}
                {record.run_outcome ? <Detail label={ru.diagnostics.runOutcome} value={record.run_outcome} /> : null}
                {record.run_id ? <Detail label={ru.diagnostics.runReference} value={boundedReference(record.run_id)} code /> : null}
                {record.message_id ? <Detail label={ru.diagnostics.messageReference} value={boundedReference(record.message_id)} code /> : null}
                {record.delivery_status ? <Detail label={ru.diagnostics.deliveryState} value={record.delivery_status} /> : null}
                {record.delivery_attempt_count ? <Detail label={ru.diagnostics.deliveryAttempts} value={String(record.delivery_attempt_count)} /> : null}
                {record.delivery_last_error_code ? <Detail label={ru.diagnostics.safeFailureCode} value={record.delivery_last_error_code} code /> : null}
                {record.feedback_outcome ? <Detail label={ru.diagnostics.feedback} value={record.feedback_outcome} /> : null}
                {record.trace_id ? <Detail label={ru.diagnostics.traceReference} value={boundedReference(record.trace_id)} code /> : null}
                {record.correlation_id ? <Detail label={ru.diagnostics.correlationReference} value={boundedReference(record.correlation_id)} code /> : null}
              </dl>
              {deliveryEventId ? (
                <button
                  className={styles.drilldown}
                  onClick={() => onInspectDelivery(deliveryEventId)}
                  type="button"
                >
                  {ru.diagnostics.inspectDelivery}: {boundedReference(deliveryEventId)}
                </button>
              ) : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function Detail({ label, value, code = false }: { label: string; value: string; code?: boolean }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{code ? <code>{value}</code> : value}</dd>
    </div>
  );
}

function sourceLabel(source: OperationalCorrelationView['records'][number]['source_kind'] | OperationalCorrelationView['missing_sources'][number]): string {
  return ru.diagnostics.sources[source];
}

function conflictLabel(value: string): string {
  return ru.diagnostics.conflictCodes[value as keyof typeof ru.diagnostics.conflictCodes] ?? ru.diagnostics.unknownConflict;
}

function boundedReference(value: string): string {
  return `${value.slice(0, 8)}…`;
}

function formatTimestamp(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return ru.documents.unknown;
  return new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }).format(timestamp);
}
