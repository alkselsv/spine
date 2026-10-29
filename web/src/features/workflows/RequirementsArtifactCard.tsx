import { useQuery } from '@tanstack/react-query';

import type { components } from '../../../api/generated/schema';
import { apiQueryOptions } from '../../api/query';
import type { createScopedApiClient } from '../../api/client';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import styles from '../chat/SessionBrowserPage.module.css';

type Client = Pick<ReturnType<typeof createScopedApiClient>, 'GET'>;
type RequirementsArtifact = components['schemas']['RequirementsArtifact'];

export function RequirementsArtifactCard({
  client,
  queryKey,
  runId,
}: {
  client: Client;
  queryKey: readonly unknown[];
  runId: string;
}) {
  const query = useQuery({
    ...apiQueryOptions({
      load: async (signal) => {
        const { data, error } = await client.GET('/api/v1/runs/{run_id}/requirements', {
          params: { path: { run_id: runId } }, signal,
        });
        if (error || !data) throw new Error('Требования к запуску недоступны в этой рабочей области.');
        return data as RequirementsArtifact;
      },
      queryKey: [...queryKey, 'requirements-artifact', runId],
    }),
    enabled: Boolean(runId),
  });

  if (query.isPending) return <StatusPanel state="loading" />;
  if (query.error) return <ApiErrorPanel error={query.error} />;
  if (!query.data) return <StatusPanel state="empty" />;
  const artifact = query.data;

  return (
    <section aria-label="Требования к работе" className={styles.page}>
      <h3>Требования · версия {artifact.artifact_version}</h3>
      <p role="status">Статус: {artifact.state}</p>
      <ul>
        {artifact.requirements.map((requirement) => (
          <li key={requirement.requirement_id}>
            <strong>{requirement.category}</strong>: {requirement.statement}
            {requirement.constraints.length > 0 ? (
              <ul>{requirement.constraints.map((constraint) => <li key={constraint}>{constraint}</li>)}</ul>
            ) : null}
            {requirement.missing_markers.map((marker) => <p key={marker}>Не указано: {marker}</p>)}
            {requirement.ambiguous_markers.map((marker) => <p key={marker}>Требует уточнения: {marker}</p>)}
            <small>Ссылки на источники: {requirement.source_refs.length ? `${requirement.source_refs.length} (доступ проверяется отдельно)` : 'нет'}</small>
            {requirement.state === 'unsupported' || requirement.state === 'unverified' ? (
              <p role="status">{requirement.state === 'unsupported' ? 'Не подтверждено источниками' : 'Не проверено'}</p>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
