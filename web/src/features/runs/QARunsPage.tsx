import { Select } from '@base-ui/react';
import { useQuery } from '@tanstack/react-query';
import { type FormEvent, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router';

import { createScopedApiClient, SpineApiError } from '../../api/client';
import { apiQueryOptions } from '../../api/query';
import { useAuth } from '../../app/auth';
import { ApiErrorPanel, StatusPanel, type InterfaceState } from '../../app/status';
import { useScope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import {
  QA_RUN_OUTCOMES,
  loadQARuns,
  parseQARunQuery,
  isValidQARunSubjectId,
  updateQARunQuery,
  type QARunCollection,
  type QARunOutcome,
  type QARunQuery,
  type QARunSummary,
} from './qa-run-api';
import styles from './QARunsPage.module.css';

const ALL_OUTCOMES = '__all__';

interface FilterDraft {
  subjectId: string;
  outcome: QARunOutcome | '';
  createdAfter: string;
  createdBefore: string;
}
export function QARunsPage() {
  const auth = useAuth();
  const { scope, queryKey } = useScope();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryState = parseQARunQuery(`?${searchParams.toString()}`);
  const [draft, setDraft] = useState<FilterDraft>(() => draftFromQuery(queryState));
  const [validationMessage, setValidationMessage] = useState<string | null>(null);
  const client = useMemo(
    () =>
      createScopedApiClient({
        baseUrl: window.location.origin,
        getBearerCredential: auth.getBearerCredential ?? (() => ''),
        scope,
      }),
    [auth.getBearerCredential, scope],
  );
  const runsQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadQARuns(client, queryState, signal, scope),
      queryKey: [...queryKey('qa-runs'), queryState],
    }),
    enabled: Boolean(auth.getBearerCredential),
  });

  useEffect(() => {
    setDraft(draftFromQuery(queryState));
  }, [queryState.subjectId, queryState.outcome, queryState.createdAfter, queryState.createdBefore]);

  useEffect(() => {
    if (
      searchParams.get('workspace') === scope.workspaceId &&
      searchParams.get('environment') === scope.environmentId
    ) {
      return;
    }
    const nextSearch = new URLSearchParams(searchParams);
    nextSearch.set('workspace', scope.workspaceId);
    nextSearch.set('environment', scope.environmentId);
    setSearchParams(nextSearch, { replace: true });
  }, [scope.environmentId, scope.workspaceId, searchParams, setSearchParams]);

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const subjectId = draft.subjectId.trim();
    if (subjectId && !isValidQARunSubjectId(subjectId)) {
      setValidationMessage(ru.qaRuns.invalidInitiator);
      return;
    }
    const createdAfter = localDateTimeToIso(draft.createdAfter);
    const createdBefore = localDateTimeToIso(draft.createdBefore);
    if ((draft.createdAfter && !createdAfter) || (draft.createdBefore && !createdBefore)) {
      setValidationMessage(ru.status.validationFailed.description);
      return;
    }
    if (createdAfter && createdBefore && createdAfter > createdBefore) {
      setValidationMessage(ru.qaRuns.invalidDateRange);
      return;
    }
    setValidationMessage(null);
    setSearchParams(
      updateQARunQuery(searchParams.toString(), {
        ...(subjectId ? { subjectId } : {}),
        ...(draft.outcome ? { outcome: draft.outcome } : {}),
        ...(createdAfter ? { createdAfter } : {}),
        ...(createdBefore ? { createdBefore } : {}),
      }),
      { replace: true },
    );
  }

  function clearFilters() {
    setValidationMessage(null);
    setDraft({ subjectId: '', outcome: '', createdAfter: '', createdBefore: '' });
    setSearchParams(updateQARunQuery(searchParams.toString(), {}), { replace: true });
  }

  function nextPage() {
    if (!runsQuery.data?.next_cursor) return;
    setSearchParams(
      updateQARunQuery(searchParams.toString(), {
        ...queryState,
        cursor: runsQuery.data.next_cursor,
      }),
    );
  }

  const hasFilters = Boolean(
    queryState.subjectId ||
      queryState.outcome ||
      queryState.createdAfter ||
      queryState.createdBefore,
  );

  return (
    <section className={styles.page} aria-labelledby="qa-runs-heading">
      <div className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>{ru.page.eyebrow}</p>
          <h2 id="qa-runs-heading">{ru.qaRuns.title}</h2>
          <p className={styles.lede}>{ru.qaRuns.description}</p>
        </div>
        {hasFilters ? (
          <button className={styles.secondaryButton} onClick={clearFilters} type="button">
            {ru.qaRuns.clearFilters}
          </button>
        ) : null}
      </div>

      <form className={styles.filters} aria-label={ru.qaRuns.filtersLabel} onSubmit={applyFilters}>
        <OutcomeSelect
          value={draft.outcome}
          onValueChange={(outcome) => setDraft((current) => ({ ...current, outcome }))}
        />
        <label className={styles.filter}>
          <span>{ru.qaRuns.initiator}</span>
          <input
            aria-label={ru.qaRuns.initiator}
            className={styles.input}
            placeholder={ru.qaRuns.initiatorPlaceholder}
            value={draft.subjectId}
            onChange={(event) => setDraft((current) => ({ ...current, subjectId: event.target.value }))}
          />
        </label>
        <label className={styles.filter}>
          <span>{ru.qaRuns.createdAfter}</span>
          <input
            aria-label={ru.qaRuns.createdAfter}
            className={styles.input}
            type="datetime-local"
            value={draft.createdAfter}
            onChange={(event) => setDraft((current) => ({ ...current, createdAfter: event.target.value }))}
          />
        </label>
        <label className={styles.filter}>
          <span>{ru.qaRuns.createdBefore}</span>
          <input
            aria-label={ru.qaRuns.createdBefore}
            className={styles.input}
            type="datetime-local"
            value={draft.createdBefore}
            onChange={(event) => setDraft((current) => ({ ...current, createdBefore: event.target.value }))}
          />
        </label>
        <button className={styles.primaryButton} type="submit">
          {ru.qaRuns.applyFilters}
        </button>
      </form>
      {validationMessage ? <p className={styles.validation} role="alert">{validationMessage}</p> : null}

      {!auth.getBearerCredential ? (
        <StatusPanel state="degraded" />
      ) : runsQuery.error && !runsQuery.data ? (
        <div className={styles.recovery}>
          <ApiErrorPanel error={runsQuery.error} />
          <button className={styles.secondaryButton} onClick={() => void runsQuery.refetch()} type="button">
            {ru.documents.retry}
          </button>
        </div>
      ) : runsQuery.isPending ? (
        <StatusPanel state="loading" />
      ) : runsQuery.data ? (
        <RunResults
          data={runsQuery.data}
          error={runsQuery.error}
          isStale={runsQuery.isStale}
          onNextPage={nextPage}
          onRetry={() => void runsQuery.refetch()}
        />
      ) : (
        <StatusPanel state="failed" />
      )}
    </section>
  );
}

function OutcomeSelect({
  onValueChange,
  value,
}: {
  onValueChange: (value: QARunOutcome | '') => void;
  value: QARunOutcome | '';
}) {
  const items = [
    { value: ALL_OUTCOMES, label: ru.documents.all },
    ...QA_RUN_OUTCOMES.map((outcome) => ({ value: outcome, label: ru.qaRuns.outcomes[outcome] })),
  ];
  return (
    <label className={styles.filter}>
      <span>{ru.qaRuns.outcome}</span>
      <Select.Root
        items={items}
        value={value || ALL_OUTCOMES}
        onValueChange={(nextValue) =>
          onValueChange(nextValue === ALL_OUTCOMES || nextValue === null ? '' : (nextValue as QARunOutcome))
        }
      >
        <Select.Trigger className={styles.filterTrigger} aria-label={ru.qaRuns.outcome}>
          <Select.Value />
          <Select.Icon className={styles.filterIcon}>⌄</Select.Icon>
        </Select.Trigger>
        <Select.Portal>
          <Select.Positioner className={styles.filterPositioner} sideOffset={4}>
            <Select.Popup className={styles.filterPopup}>
              <Select.List>
                {items.map((item) => (
                  <Select.Item className={styles.filterOption} key={item.value} value={item.value}>
                    <Select.ItemText>{item.label}</Select.ItemText>
                  </Select.Item>
                ))}
              </Select.List>
            </Select.Popup>
          </Select.Positioner>
        </Select.Portal>
      </Select.Root>
    </label>
  );
}

function RunResults({
  data,
  error,
  isStale,
  onNextPage,
  onRetry,
}: {
  data: QARunCollection;
  error: SpineApiError | null;
  isStale: boolean;
  onNextPage: () => void;
  onRetry: () => void;
}) {
  if (error) {
    return (
      <div className={styles.recovery}>
        <ApiErrorPanel error={error} />
        {data ? <StatusPanel state="stale" /> : null}
        <button className={styles.secondaryButton} onClick={onRetry} type="button">
          {ru.documents.retry}
        </button>
      </div>
    );
  }
  const state = collectionState(data, isStale);
  return (
    <div>
      {state === 'stale' ? <StatusPanel state="stale" /> : null}
      {data.items.length === 0 ? (
        <StatusPanel state="empty" />
      ) : (
        <>
          <div className={styles.tableFrame}>
            <table className={styles.table} aria-label={ru.qaRuns.tableCaption}>
              <caption className={styles.visuallyHidden}>{ru.qaRuns.tableCaption}</caption>
              <thead>
                <tr>
                  <th scope="col">{ru.qaRuns.run}</th>
                  <th scope="col">{ru.qaRuns.status}</th>
                  <th scope="col">{ru.qaRuns.result}</th>
                  <th scope="col">{ru.qaRuns.initiatorColumn}</th>
                  <th scope="col">{ru.qaRuns.createdAt}</th>
                  <th scope="col">{ru.qaRuns.completedAt}</th>
                  <th scope="col">{ru.qaRuns.duration}</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((run) => (
                  <RunRow key={run.run_id} run={run} />
                ))}
              </tbody>
            </table>
          </div>
          {data.next_cursor ? (
            <button className={styles.secondaryButton} onClick={onNextPage} type="button">
              {ru.qaRuns.nextPage}
            </button>
          ) : null}
        </>
      )}
    </div>
  );
}

function RunRow({ run }: { run: QARunSummary }) {
  return (
    <tr>
      <td><code>{run.run_id}</code></td>
      <td><span className={styles.statusPill}>{stateLabel(run.state)}</span></td>
      <td>{run.outcome ? ru.qaRuns.outcomes[run.outcome] : ru.qaRuns.unknown}</td>
      <td>
        <span className={styles.primaryCell}><code>{run.subject_id}</code></span>
        <span className={styles.secondaryCell}>{subjectKindLabel(run.subject_kind)}</span>
      </td>
      <td><time dateTime={run.created_at}>{formatTimestamp(run.created_at)}</time></td>
      <td>
        {run.completed_at ? (
          <time dateTime={run.completed_at}>{formatTimestamp(run.completed_at)}</time>
        ) : ru.qaRuns.unknown}
      </td>
      <td>{formatDuration(run.created_at, run.completed_at)}</td>
    </tr>
  );
}

function collectionState(
  data: QARunCollection,
  isStale: boolean,
): InterfaceState {
  if (isStale) return 'stale';
  if (data.items.length === 0) return 'empty';
  return 'ready';
}

function draftFromQuery(query: QARunQuery): FilterDraft {
  return {
    subjectId: query.subjectId ?? '',
    outcome: query.outcome ?? '',
    createdAfter: isoToLocalDateTime(query.createdAfter),
    createdBefore: isoToLocalDateTime(query.createdBefore),
  };
}

function isoToLocalDateTime(value: string | undefined): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return '';
  const pad = (part: number) => String(part).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function localDateTimeToIso(value: string): string | undefined {
  if (!value) return undefined;
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? undefined : date.toISOString();
}

function stateLabel(value: string): string {
  if (value === 'completed' || value === 'terminal') return ru.qaRuns.terminal;
  if (value === 'running') return ru.qaRuns.pending;
  return ru.qaRuns.unknown;
}

function subjectKindLabel(value: string): string {
  return ru.qaRuns.subjectKinds[value as keyof typeof ru.qaRuns.subjectKinds] ?? ru.qaRuns.unknown;
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return ru.qaRuns.unknown;
  return new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function formatDuration(createdAt: string, completedAt: string | null): string {
  if (!completedAt) return ru.qaRuns.unknown;
  const milliseconds = new Date(completedAt).valueOf() - new Date(createdAt).valueOf();
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return ru.qaRuns.unknown;
  const seconds = Math.round(milliseconds / 1_000);
  if (seconds >= 60) {
    return new Intl.NumberFormat('ru-RU', { style: 'unit', unit: 'minute', unitDisplay: 'short' }).format(
      Math.round(seconds / 60),
    );
  }
  return new Intl.NumberFormat('ru-RU', { style: 'unit', unit: 'second', unitDisplay: 'short' }).format(seconds);
}

