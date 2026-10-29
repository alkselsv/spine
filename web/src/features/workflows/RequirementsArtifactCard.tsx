import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import type { components } from '../../../api/generated/schema';
import { apiQueryOptions } from '../../api/query';
import type { createScopedApiClient } from '../../api/client';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import styles from '../chat/SessionBrowserPage.module.css';

type Client = Pick<ReturnType<typeof createScopedApiClient>, 'GET'>;
type RequirementsArtifact = components['schemas']['RequirementsArtifact'];
type RequirementEvidenceRef = components['schemas']['RequirementEvidenceRef'];

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
            <small>Ревизии источников: {requirement.source_refs.length || 'нет'}</small>
            <small>
              Подтверждения: {evidenceStatusLabel(requirement.evidence_status)} · точных ссылок {requirement.evidence_refs.length}
            </small>
            {requirement.state === 'unsupported' || requirement.state === 'unverified' ? (
              <p role="status">{requirement.state === 'unsupported' ? 'Не подтверждено источниками' : 'Не проверено'}</p>
            ) : null}
            {requirement.evidence_status === 'unverified' ? <p role="status">Источники не проверены</p> : null}
            {requirement.evidence_refs.map((evidence) => (
              <RequirementEvidenceInspector
                key={`${requirement.requirement_id}-${evidence.segment_id}`}
                client={client}
                artifactId={artifact.artifact_id}
                artifactVersion={artifact.artifact_version}
                requirementId={requirement.requirement_id}
                evidence={evidence}
              />
            ))}
          </li>
        ))}
      </ul>
    </section>
  );
}

function RequirementEvidenceInspector({
  client,
  artifactId,
  artifactVersion,
  requirementId,
  evidence,
}: {
  client: Client;
  artifactId: string;
  artifactVersion: number;
  requirementId: string;
  evidence: RequirementEvidenceRef;
}) {
  const [open, setOpen] = useState(false);
  const query = useQuery({
    ...apiQueryOptions({
      load: async (signal) => {
        const { data, error } = await client.GET(
          '/api/v1/artifacts/{artifact_id}/requirements/{requirement_id}/evidence/{segment_id}',
          {
            params: {
              path: {
                artifact_id: artifactId,
                requirement_id: requirementId,
                segment_id: evidence.segment_id,
              },
              query: { artifact_version: artifactVersion },
            },
            signal,
          },
        );
        if (error || !data) throw new Error('Evidence inspection is unavailable.');
        return data;
      },
      queryKey: [
        'requirements-evidence',
        artifactId,
        artifactVersion,
        requirementId,
        evidence.segment_id,
      ],
    }),
    enabled: open,
  });

  const payload = query.data?.data;
  return (
    <div>
      <button type="button" onClick={() => setOpen((value) => !value)}>
        {open ? 'Скрыть evidence' : 'Открыть evidence'}
      </button>
      {open && query.isPending ? <p role="status">Загрузка evidence…</p> : null}
      {open && query.error ? <ApiErrorPanel error={query.error} /> : null}
      {open && payload?.status === 'available' && payload.evidence ? (
        <p role="status">{payload.evidence.text}</p>
      ) : null}
      {open && payload && payload.status !== 'available' ? (
        <p role="status">Evidence недоступна: {payload.status}</p>
      ) : null}
    </div>
  );
}

function evidenceStatusLabel(status: RequirementsArtifact['requirements'][number]['evidence_status']): string {
  switch (status) {
    case 'referenced': return 'есть ссылки';
    case 'unsupported': return 'не подтверждено';
    case 'unverified': return 'не проверено';
  }
}
