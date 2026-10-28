import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { useNavigate, useParams } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { createScopedApiClient } from '../../api/client';
import { apiQueryOptions } from '../../api/query';
import { useAuth } from '../../app/auth';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import { useScope } from '../../app/scope';
import styles from '../chat/SessionBrowserPage.module.css';

type WorkflowRun = components['schemas']['WorkflowRunResponse'];

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

  return <WorkflowRunDetail run={runQuery.data} onBack={() => navigate('/workflow/start')} />;
}

function WorkflowRunDetail({ run, onBack }: { run: WorkflowRun; onBack: () => void }) {
  return (
    <section className={styles.page} aria-labelledby="workflow-run-heading">
      <button className={styles.textButton} onClick={onBack} type="button">Новая работа</button>
      <h2 id="workflow-run-heading">Запуск workflow</h2>
      <dl>
        <dt>Состояние</dt><dd>{run.state}</dd>
        <dt>Workflow</dt><dd>{run.workflow_key} · {run.workflow_version}</dd>
        <dt>Запрос</dt><dd>{run.message}</dd>
        <dt>Run ID</dt><dd><code>{run.run_id}</code></dd>
        {run.accepted_sources.length ? <><dt>Принятые источники</dt><dd>{run.accepted_sources.length}</dd></> : null}
      </dl>
    </section>
  );
}
