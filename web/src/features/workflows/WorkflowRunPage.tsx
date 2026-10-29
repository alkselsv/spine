import { useQuery } from '@tanstack/react-query';
import { useMemo, useRef, useState, type FormEvent } from 'react';
import { useNavigate, useParams } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { createScopedApiClient } from '../../api/client';
import { apiQueryOptions } from '../../api/query';
import { useAuth } from '../../app/auth';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import { useScope } from '../../app/scope';
import styles from '../chat/SessionBrowserPage.module.css';
import { RequirementsArtifactCard } from './RequirementsArtifactCard';

type WorkflowRun = components['schemas']['WorkflowRunResponse'];
type WorkflowEventPage = components['schemas']['WorkflowEventPageResponse'];

export function WorkflowRunPage() {
  const auth = useAuth();
  const { scope, queryKey } = useScope();
  const navigate = useNavigate();
  const { runId = '' } = useParams();
  const client = useMemo(
    () => createScopedApiClient({
      baseUrl: window.location.origin,
      getBearerCredential: auth.getBearerCredential ?? (() => ''),
      scope,
    }),
    [auth.getBearerCredential, scope],
  );
  const runQuery = useQuery({
    ...apiQueryOptions({
      load: async (signal) => {
        const { data, error } = await client.GET('/api/v1/runs/{run_id}', {
          params: { path: { run_id: runId } },
          signal,
        });
        if (error || !data) throw new Error('Запуск недоступен в текущей рабочей области.');
        return data as WorkflowRun;
      },
      queryKey: [...queryKey('workflow-run'), runId],
    }),
    enabled: Boolean(auth.getBearerCredential && runId),
  });
  const eventsQuery = useQuery({
    ...apiQueryOptions({
      load: async (signal) => {
        const items: WorkflowEventPage['items'][number][] = [];
        let cursor: string | undefined;
        do {
          const { data, error } = await client.GET('/api/v1/runs/{run_id}/events', {
            params: {
              path: { run_id: runId },
              query: { limit: 100, ...(cursor ? { cursor } : {}) },
            },
            signal,
          });
          if (error || !data) throw new Error('События запуска недоступны в текущей рабочей области.');
          items.push(...data.items);
          cursor = data.next_cursor ?? undefined;
        } while (cursor);
        return { schema_version: 1, limit: 100, items, next_cursor: null } as WorkflowEventPage;
      },
      queryKey: [...queryKey('workflow-run-events'), runId],
    }),
    enabled: Boolean(auth.getBearerCredential && runId),
  });

  if (!auth.getBearerCredential) return <StatusPanel state="permissionDenied" />;
  if (runQuery.isPending) return <StatusPanel state="loading" />;
  if (runQuery.error) {
    return (
      <section className={styles.page}>
        <ApiErrorPanel error={runQuery.error} />
        <button className={styles.secondaryButton} onClick={() => void runQuery.refetch()} type="button">Повторить</button>
      </section>
    );
  }
  if (!runQuery.data) return <StatusPanel state="failed" />;

  return (
    <WorkflowRunDetail
      client={client}
      events={eventsQuery.data ?? { items: [], next_cursor: null, limit: 100, schema_version: 1 }}
      eventsDegraded={Boolean(eventsQuery.error)}
      eventsLoading={eventsQuery.isPending}
      onBack={() => navigate('/workflow/start')}
      onRefresh={() => Promise.all([runQuery.refetch(), eventsQuery.refetch()]).then(() => undefined)}
      run={runQuery.data}
      queryKey={queryKey('workflow-run')}
    />
  );
}

function WorkflowRunDetail({
  client,
  events,
  eventsDegraded,
  eventsLoading,
  onBack,
  onRefresh,
  run,
  queryKey,
}: {
  client: ReturnType<typeof createScopedApiClient>;
  events: WorkflowEventPage;
  eventsDegraded: boolean;
  eventsLoading: boolean;
  onBack: () => void;
  onRefresh: () => Promise<void>;
  run: WorkflowRun;
  queryKey: readonly unknown[];
}) {
  const [message, setMessage] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const commandRef = useRef<{
    commandId: string;
    idempotencyKey: string;
    message: string;
    expectedRunVersion: number;
  } | null>(null);
  const canAddContext = run.allowed_commands.includes('add_context');

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting || !message.trim() || !canAddContext) return;
    const command = commandRef.current ?? {
      commandId: crypto.randomUUID(),
      idempotencyKey: crypto.randomUUID(),
      message: message.trim(),
      expectedRunVersion: run.run_version,
    };
    commandRef.current = command;
    setSubmitting(true);
    setError(null);
    const { data, error: responseError } = await client.POST('/api/v1/runs/{run_id}/commands', {
      params: {
        path: { run_id: run.run_id },
        header: { 'Idempotency-Key': command.idempotencyKey },
      },
      body: {
        schema_version: 1,
        command_id: command.commandId,
        command_type: 'add_context',
        command_schema_version: 1,
        expected_run_version: command.expectedRunVersion,
        payload: { schema_version: 1, message: command.message },
        artifact_refs: [],
        correlation_id: run.run_id,
        causation_id: run.run_id,
      },
    });
    setSubmitting(false);
    if (responseError || !data) {
      setError('Не удалось добавить контекст. Обновите запуск и повторите команду.');
      return;
    }
    commandRef.current = null;
    setMessage('');
    await onRefresh();
  }

  return (
    <section className={styles.page} aria-labelledby="workflow-run-heading">
      <button className={styles.textButton} onClick={onBack} type="button">Новая работа</button>
      <h2 id="workflow-run-heading">Запуск workflow</h2>
      <dl>
        <dt>Состояние</dt><dd>{run.state}</dd>
        <dt>Workflow</dt><dd>{run.workflow_key} · {run.workflow_version}</dd>
        <dt>Версия запуска</dt><dd>{run.run_version}</dd>
        <dt>Run ID</dt><dd><code>{run.run_id}</code></dd>
      </dl>
      <h3>Хронология</h3>
      <RequirementsArtifactCard client={client} queryKey={queryKey} runId={run.run_id} />
      {eventsLoading ? <p role="status">Загружаем события…</p> : null}
      {eventsDegraded ? (
        <div role="status">
          <p>Хронология временно недоступна. Состояние запуска загружено.</p>
          <button className={styles.secondaryButton} onClick={() => void onRefresh()} type="button">Обновить хронологию</button>
        </div>
      ) : null}
      {!eventsLoading && !eventsDegraded && events.items.length ? (
        <ol>
          {events.items.map((item) => <li key={item.event_id ?? item.sequence}>{item.summary}</li>)}
        </ol>
      ) : null}
      {!eventsLoading && !eventsDegraded && !events.items.length ? <p>Событий пока нет.</p> : null}
      {canAddContext ? (
        <form className={styles.askForm} onSubmit={(event) => void submit(event)}>
          <label htmlFor="workflow-add-context">Дополнительный контекст</label>
          <textarea
            disabled={submitting}
            id="workflow-add-context"
            onChange={(event) => {
              setMessage(event.target.value);
              commandRef.current = null;
            }}
            value={message}
          />
          <button className={styles.primaryButton} disabled={submitting || !message.trim()} type="submit">
            {submitting ? 'Добавляем…' : 'Добавить контекст'}
          </button>
          {error ? <p role="alert">{error}</p> : null}
        </form>
      ) : null}
    </section>
  );
}
