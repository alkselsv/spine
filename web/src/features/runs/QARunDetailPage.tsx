import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { Link, useParams, useSearchParams } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { createScopedApiClient } from '../../api/client';
import { apiQueryOptions } from '../../api/query';
import { useAuth } from '../../app/auth';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import { useScope, type Scope } from '../../app/scope';
import { OperationalCorrelationPanel } from '../diagnostics/OperationalCorrelationPanel';
import { CompletenessPresentation } from '../chat/CompletenessPresentation';
import { ru } from '../../i18n/resources';
import {
  isValidQARunId,
  loadQARunDetail,
  type QARunDetail,
} from './qa-run-api';
import styles from './QARunDetailPage.module.css';

type QARunAttempt = components['schemas']['QARunAttemptResponse'];
type QARunTimelineEvent = components['schemas']['QARunTimelineEventResponse'];
type QARunEvidence = components['schemas']['QARunEvidenceResponse'];

export function QARunDetailPage() {
  const auth = useAuth();
  const { scope, queryKey } = useScope();
  const { runId } = useParams();
  const [searchParams] = useSearchParams();
  const client = useMemo(
    () =>
      createScopedApiClient({
        baseUrl: window.location.origin,
        getBearerCredential: auth.getBearerCredential ?? (() => ''),
        scope,
      }),
    [auth.getBearerCredential, scope],
  );
  const detailQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadQARunDetail(client, runId ?? '', signal, scope),
      queryKey: [...queryKey('qa-run-detail'), runId],
    }),
    enabled: Boolean(auth.getBearerCredential && runId && isValidQARunId(runId)),
  });

  if (!auth.getBearerCredential) return <StatusPanel state="degraded" />;
  if (!runId || !isValidQARunId(runId)) return <StatusPanel state="failed" />;
  if (detailQuery.isPending) return <StatusPanel state="loading" />;
  if (detailQuery.error) {
    return (
      <section className={styles.page} aria-labelledby="qa-run-detail-heading">
        <DetailHeader detail={null} />
        <div className={styles.recovery}>
          <ApiErrorPanel error={detailQuery.error} />
          <div className={styles.actionRow}>
            <button className={styles.secondaryButton} onClick={() => void detailQuery.refetch()} type="button">
              {ru.documents.retry}
            </button>
            <BackToRuns searchParams={searchParams} scope={scope} />
          </div>
        </div>
      </section>
    );
  }
  if (!detailQuery.data) return <StatusPanel state="failed" />;

  return (
    <QARunDetail
      client={client}
      detail={detailQuery.data}
      queryKey={queryKey}
      searchParams={searchParams}
      scope={scope}
    />
  );
}

function QARunDetail({
  client,
  detail,
  queryKey,
  searchParams,
  scope,
}: {
  client: ReturnType<typeof createScopedApiClient>;
  detail: QARunDetail;
  queryKey: (resource: string) => readonly unknown[];
  searchParams: URLSearchParams;
  scope: Scope;
}) {
  return (
    <section className={styles.page} aria-labelledby="qa-run-detail-heading">
      <DetailHeader detail={detail} />
      <div className={styles.actionRow}>
        <BackToMessage detail={detail} searchParams={searchParams} scope={scope} />
        <BackToRuns searchParams={searchParams} scope={scope} />
      </div>

      <OperationalCorrelationPanel
        client={client}
        queryKey={queryKey}
        referenceId={detail.run_id}
        referenceKind="run"
        scope={scope}
      />

      <div className={styles.grid}>
        <IdentityCard detail={detail} />
        <WarningsCard warnings={detail.warnings} />
        <TimelineCard events={detail.diagnostic_events} />
        <AttemptsCard attempts={detail.attempts} />
        <EvidenceCard detail={detail} evidence={detail.accepted_evidence} scope={scope} />
      </div>
    </section>
  );
}

function DetailHeader({ detail }: { detail: QARunDetail | null }) {
  return (
    <header className={styles.header}>
      <div>
        <p className={styles.eyebrow}>{ru.page.eyebrow}</p>
        <h2 id="qa-run-detail-heading">{ru.qaRuns.detail.title}</h2>
        <p className={styles.lede}>{ru.qaRuns.detail.description}</p>
        {detail ? (
          <span className={styles.sourceId}>
            {ru.qaRuns.run}: <code>{detail.run_id}</code>
          </span>
        ) : null}
      </div>
      {detail ? <span className={styles.statusPill}>{stateLabel(detail.state)}</span> : null}
    </header>
  );
}

function IdentityCard({ detail }: { detail: QARunDetail }) {
  return (
    <section className={styles.card} aria-labelledby="qa-run-identity-heading">
      <h3 id="qa-run-identity-heading">{ru.qaRuns.detail.runIdentity}</h3>
      <dl className={styles.details}>
        <DetailValue label={ru.qaRuns.run} value={detail.run_id} code />
        <DetailValue label={ru.qaRuns.detail.state} value={stateLabel(detail.state)} />
        <DetailValue label={ru.qaRuns.detail.outcome} value={outcomeLabel(detail.outcome)} />
        <DetailValue label={ru.qaRuns.detail.subject} value={subjectKindLabel(detail.subject_kind)} />
        <DetailValue label={ru.qaRuns.detail.subjectId} value={detail.subject_id} code />
        <DetailValue label={ru.qaRuns.detail.createdAt} value={formatTimestamp(detail.created_at)} />
        <DetailValue label={ru.qaRuns.detail.completedAt} value={formatNullableTimestamp(detail.completed_at)} />
        <DetailValue label={ru.qaRuns.detail.duration} value={formatDuration(detail.created_at, detail.completed_at)} />
        <DetailValue label={ru.qaRuns.detail.traceId} value={detail.trace_id} code />
        <DetailValue label={ru.qaRuns.detail.correlationId} value={detail.correlation_id ?? ru.qaRuns.detail.unknown} code={Boolean(detail.correlation_id)} />
        <DetailValue label={ru.qaRuns.detail.causationId} value={detail.causation_id ?? ru.qaRuns.detail.unknown} code={Boolean(detail.causation_id)} />
        <DetailValue label={ru.qaRuns.detail.sessionId} value={detail.session_id ?? ru.qaRuns.detail.unknown} code={Boolean(detail.session_id)} />
        <DetailValue label={ru.qaRuns.detail.messageId} value={detail.message_id ?? ru.qaRuns.detail.unknown} code={Boolean(detail.message_id)} />
        <DetailValue label={ru.qaRuns.detail.commandDigest} value={detail.command_digest} code />
      </dl>
    </section>
  );
}

function WarningsCard({ warnings }: { warnings: readonly string[] }) {
  if (warnings.length === 0) return null;
  return (
    <section className={styles.card} aria-labelledby="qa-run-warnings-heading">
      <h3 id="qa-run-warnings-heading">{ru.qaRuns.detail.warnings}</h3>
      <ul className={styles.compactList}>
        {warnings.map((warning) => <li key={warning}>{warning}</li>)}
      </ul>
    </section>
  );
}

function TimelineCard({ events }: { events: readonly QARunTimelineEvent[] }) {
  const orderedEvents = [...events].sort(compareTimelineEvents);
  return (
    <section className={`${styles.card} ${styles.cardWide}`} aria-labelledby="qa-run-timeline-heading">
      <h3 id="qa-run-timeline-heading">{ru.qaRuns.detail.timeline}</h3>
      {orderedEvents.length === 0 ? (
        <p className={styles.muted}>{ru.qaRuns.detail.noTimeline}</p>
      ) : (
        <ol className={styles.timeline} aria-label={ru.qaRuns.detail.timeline}>
          {orderedEvents.map((event) => (
            <li className={styles.timelineItem} key={event.event_id}>
              <span className={styles.timelineMarker} aria-hidden="true" />
              <div className={styles.timelineBody}>
                <div className={styles.timelineHeading}>
                  <strong>{event.kind}</strong>
                  <time dateTime={event.occurred_at}>{formatTimestamp(event.occurred_at)}</time>
                </div>
                {event.reason ? <span className={styles.muted}>{event.reason}</span> : null}
                <code>{event.event_id}</code>
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function AttemptsCard({ attempts }: { attempts: readonly QARunAttempt[] }) {
  const orderedAttempts = [...attempts].sort(
    (left, right) => left.attempt_number - right.attempt_number || left.attempt_id.localeCompare(right.attempt_id),
  );
  return (
    <section className={`${styles.card} ${styles.cardWide}`} aria-labelledby="qa-run-attempts-heading">
      <h3 id="qa-run-attempts-heading">{ru.qaRuns.detail.attempts}</h3>
      {orderedAttempts.length === 0 ? (
        <p className={styles.muted}>{ru.qaRuns.detail.noAttempts}</p>
      ) : (
        <div className={styles.attemptList}>
          {orderedAttempts.map((attempt) => <AttemptCard attempt={attempt} key={attempt.attempt_id} />)}
        </div>
      )}
    </section>
  );
}

function AttemptCard({ attempt }: { attempt: QARunAttempt }) {
  const pin = attempt.execution_pin;
  const context = attempt.context;
  return (
    <article className={styles.attempt} aria-labelledby={`qa-run-attempt-${attempt.attempt_id}`}>
      <header className={styles.subHeader}>
        <div>
          <h4 id={`qa-run-attempt-${attempt.attempt_id}`}>
            {ru.qaRuns.detail.attempt} {attempt.attempt_number}
          </h4>
          <code>{attempt.attempt_id}</code>
        </div>
        <span className={styles.statusPill}>{stateLabel(attempt.state)}</span>
      </header>
      <dl className={styles.details}>
        <DetailValue label={ru.qaRuns.detail.attemptState} value={stateLabel(attempt.state)} />
        <DetailValue label={ru.qaRuns.detail.outcome} value={outcomeLabel(attempt.outcome)} />
        <DetailValue label={ru.qaRuns.detail.startedAt} value={formatTimestamp(attempt.started_at)} />
        <DetailValue label={ru.qaRuns.detail.completedAt} value={formatNullableTimestamp(attempt.completed_at)} />
        <DetailValue label={ru.qaRuns.detail.latency} value={formatLatency(attempt.latency_ms)} />
        <DetailValue label={ru.qaRuns.detail.reason} value={attempt.reason ?? ru.qaRuns.detail.unknown} />
      </dl>
      <CompletenessPresentation value={attempt.completeness ?? null} />
      {attempt.completeness ? (
        <dl className={`${styles.details} ${styles.completenessVersions}`}>
          <DetailValue
            label={ru.qaRuns.detail.targetScopeVersion}
            value={`${attempt.completeness.target_scope_version} · ${attempt.completeness.target_scope_id}`}
            code
          />
          <DetailValue
            label={ru.qaRuns.detail.observedCoverageVersion}
            value={`${attempt.completeness.observed_coverage_version} · ${attempt.completeness.observed_coverage_id}`}
            code
          />
        </dl>
      ) : null}

      <div className={styles.subGrid}>
        <section className={styles.subCard} aria-labelledby={`qa-run-attempt-${attempt.attempt_id}-versions`}>
          <h5 id={`qa-run-attempt-${attempt.attempt_id}-versions`}>{ru.qaRuns.detail.versions}</h5>
          <dl className={styles.details}>
            <DetailValue label={ru.qaRuns.detail.agentVersion} value={`${pin.agent_version.version} · ${pin.agent_version.digest}`} code />
            <DetailValue label={ru.qaRuns.detail.promptVersion} value={`${pin.prompt_version.version} · ${pin.prompt_version.digest}`} code />
            <DetailValue label={ru.qaRuns.detail.modelVersion} value={`${pin.model_profile.version} · ${pin.model_profile.digest}`} code />
            <DetailValue label={ru.qaRuns.detail.contextProfile} value={`${pin.context_profile_binding} · v${pin.context_profile_version}`} />
            <DetailValue label={ru.qaRuns.detail.projectionSnapshot} value={pin.projection.projection_snapshot_id} code />
            <DetailValue label={ru.qaRuns.detail.retrievalStrategy} value={context?.retrieval_strategy ?? ru.qaRuns.detail.unknown} />
            <DetailValue label={ru.qaRuns.detail.asOf} value={context ? `${formatTimestamp(context.canonical_as_of_time)} · generation ${context.canonical_as_of_generation}` : ru.qaRuns.detail.unknown} />
            <DetailValue label={ru.qaRuns.detail.freshness} value={context?.freshness ?? ru.qaRuns.detail.unknown} />
            <DetailValue label={ru.qaRuns.detail.health} value={context?.health ?? ru.qaRuns.detail.unknown} />
            <DetailValue label={ru.qaRuns.detail.evidenceCount} value={context ? String(context.evidence_count) : ru.qaRuns.detail.unknown} />
          </dl>
        </section>
        <UsageCard headingId={`qa-run-attempt-${attempt.attempt_id}-usage`} usage={attempt.usage} />
      </div>
    </article>
  );
}

function UsageCard({
  headingId,
  usage,
}: {
  headingId: string;
  usage: components['schemas']['QARunUsageResponse'];
}) {
  return (
    <section className={styles.subCard} aria-labelledby={headingId}>
      <h5 id={headingId}>{ru.qaRuns.detail.usage}</h5>
      <dl className={styles.details}>
        <DetailValue label={ru.qaRuns.detail.invocations} value={String(usage.model_invocations)} />
        <DetailValue label={ru.qaRuns.detail.inputTokens} value={String(usage.input_tokens)} />
        <DetailValue label={ru.qaRuns.detail.outputTokens} value={String(usage.output_tokens)} />
        <DetailValue label={ru.qaRuns.detail.cachedInputTokens} value={String(usage.cached_input_tokens)} />
        <DetailValue label={ru.qaRuns.detail.cost} value={usage.cost} code />
        <DetailValue label={ru.qaRuns.detail.currency} value={usage.currency ?? ru.qaRuns.detail.unknown} />
      </dl>
    </section>
  );
}

function EvidenceCard({
  detail,
  evidence,
  scope,
}: {
  detail: QARunDetail;
  evidence: readonly QARunEvidence[];
  scope: Scope;
}) {
  return (
    <section className={`${styles.card} ${styles.cardWide}`} aria-labelledby="qa-run-evidence-heading">
      <h3 id="qa-run-evidence-heading">{ru.qaRuns.detail.evidence}</h3>
      {!detail.evidence_eligible ? (
        <p className={styles.muted} role="note">{ru.qaRuns.detail.evidenceHidden}</p>
      ) : evidence.length === 0 ? (
        <p className={styles.muted}>{ru.qaRuns.detail.unknown}</p>
      ) : (
        <ul className={styles.evidenceList}>
          {evidence.map((item) => (
            <EvidenceItem detail={detail} item={item} key={item.evidence_id} scope={scope} />
          ))}
        </ul>
      )}
    </section>
  );
}

function EvidenceItem({
  detail,
  item,
  scope,
}: {
  detail: QARunDetail;
  item: QARunEvidence;
  scope: Scope;
}) {
  const location = citationLocation(detail, item.citation_id, scope);
  return (
    <li className={styles.evidenceItem}>
      {location ? (
        <Link className={styles.secondaryButton} to={location}>
          {ru.qaRuns.detail.openCitation}
        </Link>
      ) : null}
      <strong>{item.evidence_id}</strong>
      <span>{item.locator_kind}: {item.locator_value}</span>
      <code>{item.source_revision_id}</code>
    </li>
  );
}

function BackToMessage({
  detail,
  searchParams,
  scope,
}: {
  detail: QARunDetail;
  searchParams: URLSearchParams;
  scope: Scope;
}) {
  const sessionId = searchParams.get('return_session_id');
  const messageId = searchParams.get('return_message_id');
  if (
    !sessionId ||
    !messageId ||
    !isValidQARunId(sessionId) ||
    !isValidQARunId(messageId) ||
    detail.session_id !== sessionId ||
    detail.message_id !== messageId
  ) {
    return null;
  }
  const search = new URLSearchParams({
    workspace: scope.workspaceId,
    environment: scope.environmentId,
    message_id: messageId,
    run_id: detail.run_id,
    return_run_id: detail.run_id,
  });
  return (
    <Link
      className={styles.secondaryButton}
      to={{ pathname: `/chat/${sessionId}`, search: `?${search.toString()}` }}
    >
      {ru.qaRuns.detail.backToMessage}
    </Link>
  );
}

function citationLocation(
  detail: QARunDetail,
  citationId: string,
  scope: Scope,
): { pathname: string; search: string } | null {
  if (
    !detail.evidence_eligible ||
    !detail.session_id ||
    !detail.message_id ||
    !isValidQARunId(detail.session_id) ||
    !isValidQARunId(detail.message_id) ||
    !isValidQARunId(citationId)
  ) {
    return null;
  }
  const search = new URLSearchParams({
    workspace: scope.workspaceId,
    environment: scope.environmentId,
    message_id: detail.message_id,
    run_id: detail.run_id,
    citation_id: citationId,
    return_run_id: detail.run_id,
  });
  return { pathname: `/chat/${detail.session_id}`, search: `?${search.toString()}` };
}

function DetailValue({
  code = false,
  label,
  value,
}: {
  code?: boolean;
  label: string;
  value: string;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{code ? <code>{value}</code> : value}</dd>
    </div>
  );
}

function BackToRuns({ searchParams, scope }: { searchParams: URLSearchParams; scope: Scope }) {
  const nextSearch = new URLSearchParams(searchParams);
  nextSearch.set('workspace', scope.workspaceId);
  nextSearch.set('environment', scope.environmentId);
  return (
    <Link className={styles.secondaryButton} to={{ pathname: '/qa/runs', search: `?${nextSearch.toString()}` }}>
      {ru.qaRuns.detail.backToRuns}
    </Link>
  );
}

function compareTimelineEvents(left: QARunTimelineEvent, right: QARunTimelineEvent): number {
  const leftTime = Date.parse(left.occurred_at);
  const rightTime = Date.parse(right.occurred_at);
  if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) {
    return leftTime - rightTime;
  }
  return left.occurred_at.localeCompare(right.occurred_at) || left.event_id.localeCompare(right.event_id);
}

function stateLabel(value: string): string {
  if (value === 'completed' || value === 'terminal') return ru.qaRuns.terminal;
  if (value === 'running') return ru.qaRuns.pending;
  return value || ru.qaRuns.detail.unknown;
}

function outcomeLabel(value: QARunDetail['outcome']): string {
  return value ? ru.qaRuns.outcomes[value] : ru.qaRuns.detail.unknown;
}

function subjectKindLabel(value: string): string {
  return ru.qaRuns.subjectKinds[value as keyof typeof ru.qaRuns.subjectKinds] ?? value;
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return ru.qaRuns.detail.unknown;
  return new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function formatNullableTimestamp(value: string | null): string {
  return value ? formatTimestamp(value) : ru.qaRuns.detail.unknown;
}

function formatDuration(createdAt: string, completedAt: string | null): string {
  if (!completedAt) return ru.qaRuns.detail.unknown;
  const milliseconds = new Date(completedAt).valueOf() - new Date(createdAt).valueOf();
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return ru.qaRuns.detail.unknown;
  return `${Math.round(milliseconds / 1_000)} с`;
}

function formatLatency(value: number | null | undefined): string {
  return value == null ? ru.qaRuns.detail.unknown : `${value} мс`;
}
