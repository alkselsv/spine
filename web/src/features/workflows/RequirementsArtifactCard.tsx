import { useRef, useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';

import type { components } from '../../../api/generated/schema';
import { apiQueryOptions } from '../../api/query';
import type { createScopedApiClient } from '../../api/client';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import styles from '../chat/SessionBrowserPage.module.css';

type Client = Pick<ReturnType<typeof createScopedApiClient>, 'GET' | 'POST'>;
type RequirementsArtifact = components['schemas']['RequirementsArtifact'];
type RequirementEvidenceRef = components['schemas']['RequirementEvidenceRef'];
type AllowedCommand = components['schemas']['WorkflowRunResponse']['allowed_commands'][number];

export function RequirementsArtifactCard({
  client,
  allowedCommands = [],
  onRefresh,
  queryKey,
  runId,
  runVersion,
}: {
  client: Client;
  allowedCommands?: AllowedCommand[];
  onRefresh?: () => Promise<void>;
  queryKey: readonly unknown[];
  runId: string;
  runVersion?: number;
}) {
  const [editingRequirementId, setEditingRequirementId] = useState<string | null>(null);
  const [statement, setStatement] = useState('');
  const [editReason, setEditReason] = useState('');
  const [reviewReason, setReviewReason] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const commandRef = useRef<{ action: string; commandId: string; idempotencyKey: string } | null>(null);
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
  const canEdit = runVersion !== undefined && allowedCommands.includes('edit_requirement');
  const canSaveReview = runVersion !== undefined && allowedCommands.includes('save_review');
  const canRequestRework = runVersion !== undefined && allowedCommands.includes('request_rework');

  async function refreshCard() {
    await Promise.all([query.refetch(), onRefresh?.() ?? Promise.resolve()]);
  }

  async function submitReview(
    action: 'edit_requirement' | 'save_review' | 'request_rework',
    payload: Record<string, unknown>,
  ) {
    if (submitting || runVersion === undefined) return;
    const pending = commandRef.current?.action === action
      ? commandRef.current
      : { action, commandId: crypto.randomUUID(), idempotencyKey: crypto.randomUUID() };
    commandRef.current = pending;
    setSubmitting(true);
    setReviewError(null);
    const { data, error } = await client.POST('/api/v1/runs/{run_id}/commands', {
      params: {
        path: { run_id: runId },
        header: { 'Idempotency-Key': pending.idempotencyKey },
      },
      body: {
        schema_version: 1,
        command_id: pending.commandId,
        command_type: action,
        command_schema_version: 1,
        expected_run_version: runVersion,
        payload: { schema_version: 1, ...payload },
        artifact_refs: [{
          artifact_id: artifact.artifact_id,
          schema_name: 'requirements',
          schema_version: artifact.schema_version,
          artifact_version: artifact.artifact_version,
        }],
        correlation_id: runId,
        causation_id: runId,
      } as never,
    });
    setSubmitting(false);
    if (error || !data) {
      const errorCode = (error as { code?: string } | undefined)?.code;
      const stale = errorCode === 'requirements.artifact_stale';
      const denied = errorCode === 'spine.authorization.denied';
      if (stale) commandRef.current = null;
      setReviewError(
        stale
          ? 'Версия требований устарела. Обновите карточку перед повторной проверкой.'
          : denied
            ? 'Недостаточно прав для изменения требований.'
            : 'Не удалось сохранить проверку. Команду можно безопасно повторить.',
      );
      return;
    }
    commandRef.current = null;
    setEditingRequirementId(null);
    setEditReason('');
    setReviewReason('');
    await refreshCard();
  }

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
            {canEdit && editingRequirementId !== requirement.requirement_id ? (
              <button
                type="button"
                onClick={() => {
                  setEditingRequirementId(requirement.requirement_id);
                  setStatement(requirement.statement);
                  setEditReason('');
                  commandRef.current = null;
                }}
              >Редактировать требование</button>
            ) : null}
            {canEdit && editingRequirementId === requirement.requirement_id ? (
              <form onSubmit={(event: FormEvent) => {
                event.preventDefault();
                void submitReview('edit_requirement', {
                  requirement_id: requirement.requirement_id,
                  statement: statement.trim(),
                  reason: editReason.trim(),
                });
              }}>
                <label>
                  Формулировка требования
                  <textarea value={statement} onChange={(event) => {
                    setStatement(event.target.value);
                    commandRef.current = null;
                  }} />
                </label>
                <label>
                  Причина изменения
                  <input value={editReason} onChange={(event) => {
                    setEditReason(event.target.value);
                    commandRef.current = null;
                  }} />
                </label>
                <button disabled={submitting || !statement.trim() || !editReason.trim()} type="submit">
                  {submitting ? 'Сохраняем…' : 'Сохранить изменение'}
                </button>
                <button type="button" onClick={() => setEditingRequirementId(null)}>Отмена</button>
              </form>
            ) : null}
          </li>
        ))}
      </ul>
      {canSaveReview || canRequestRework ? (
        <div aria-label="Проверка требований">
          <label>
            Причина решения
            <textarea value={reviewReason} onChange={(event) => {
              setReviewReason(event.target.value);
              commandRef.current = null;
            }} />
          </label>
          {canSaveReview ? (
            <button disabled={submitting || !reviewReason.trim()} type="button" onClick={() => void submitReview(
              'save_review', { reason: reviewReason.trim() },
            )}>Сохранить проверку</button>
          ) : null}
          {canRequestRework ? (
            <button disabled={submitting || !reviewReason.trim()} type="button" onClick={() => void submitReview(
              'request_rework', { reason: reviewReason.trim() },
            )}>Вернуть на доработку</button>
          ) : null}
        </div>
      ) : null}
      {reviewError ? (
        <div role="alert">
          <p>{reviewError}</p>
          <button type="button" onClick={() => void refreshCard()}>Обновить версию</button>
        </div>
      ) : null}
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
