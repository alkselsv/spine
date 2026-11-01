import { useRef, useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';

import type { components } from '../../../api/generated/schema';
import { apiQueryOptions } from '../../api/query';
import type { createScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import styles from '../chat/SessionBrowserPage.module.css';
import { TranscriptEvidenceInspector } from './TranscriptEvidenceInspector';

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
  canAnswerClarification = false,
  onAnswered,
  scope,
}: {
  client: Client;
  allowedCommands?: AllowedCommand[];
  onRefresh?: () => Promise<void>;
  queryKey: readonly unknown[];
  runId: string;
  runVersion?: number;
  canAnswerClarification?: boolean;
  onAnswered?: () => Promise<void>;
  scope?: Scope;
}) {
  const [editingRequirementId, setEditingRequirementId] = useState<string | null>(null);
  const [reclassifyingRequirementId, setReclassifyingRequirementId] = useState<string | null>(null);
  const [statement, setStatement] = useState('');
  const [reclassificationBasis, setReclassificationBasis] = useState<'manager_assertion' | 'explicit_assumption'>('manager_assertion');
  const [reclassificationRationale, setReclassificationRationale] = useState('');
  const [falseConsequence, setFalseConsequence] = useState('');
  const [conflictResolutionRationale, setConflictResolutionRationale] = useState('');
  const [independentAuthorshipConfirmed, setIndependentAuthorshipConfirmed] = useState(false);
  const [editReason, setEditReason] = useState('');
  const [reviewReason, setReviewReason] = useState('');
  const [acknowledgedAcceptanceKey, setAcknowledgedAcceptanceKey] = useState<string | null>(null);
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
  const canReclassify = runVersion !== undefined && allowedCommands.includes('reclassify_requirement');
  const canSaveReview = runVersion !== undefined && allowedCommands.includes('save_review');
  const canRequestRework = runVersion !== undefined && allowedCommands.includes('request_rework');
  const canAccept = runVersion !== undefined && allowedCommands.includes('accept_requirements');
  const assertionCount = artifact.requirements.filter(
    (item) => acceptanceBasis(item) === 'manager_assertion',
  ).length;
  const assumptionCount = artifact.requirements.filter(
    (item) => acceptanceBasis(item) === 'explicit_assumption',
  ).length;
  const acceptanceKey = [
    artifact.artifact_id,
    artifact.artifact_version,
    artifact.production_digest,
    assertionCount,
    assumptionCount,
  ].join(':');
  const requiresExplicitAcknowledgement = assertionCount + assumptionCount > 0;

  async function refreshCard() {
    await Promise.all([query.refetch(), onRefresh?.() ?? Promise.resolve()]);
  }

  async function submitReview(
    action: 'edit_requirement' | 'reclassify_requirement' | 'save_review' | 'request_rework' | 'accept_requirements',
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
      const stale = errorCode === 'requirements.artifact_stale'
        || errorCode === 'workflow.run_stale'
        || errorCode === 'workflow.run_terminal';
      const denied = errorCode === 'spine.authorization.denied'
        || errorCode === 'requirements.acceptance_denied';
      if (stale) commandRef.current = null;
      setReviewError(
        stale
          ? action === 'accept_requirements'
            ? 'Требования или состояние запуска изменились. Обновите карточку перед принятием.'
            : 'Версия требований устарела. Обновите карточку перед повторной проверкой.'
          : denied
            ? 'Недостаточно прав для изменения требований.'
            : 'Не удалось сохранить проверку. Команду можно безопасно повторить.',
      );
      return;
    }
    commandRef.current = null;
    setEditingRequirementId(null);
    setReclassifyingRequirementId(null);
    setEditReason('');
    setReclassificationRationale('');
    setFalseConsequence('');
    setConflictResolutionRationale('');
    setIndependentAuthorshipConfirmed(false);
    setReviewReason('');
    await refreshCard();
  }

  return (
    <section aria-label="Требования к работе" className={styles.page}>
      <h3>Требования · версия {artifact.artifact_version}</h3>
      <p role="status">Статус: {artifact.state}</p>
      {artifact.clarifications.map((clarification) => (
        <ClarificationPrompt
          key={clarification.clarification_id}
          artifact={artifact}
          canAnswer={canAnswerClarification}
          clarification={clarification}
          client={client}
          onAnswered={async () => {
            await query.refetch();
            await onAnswered?.();
          }}
          runVersion={runVersion}
        />
      ))}
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
            {requirement.source_refs.map((source) => (
              <code key={source.source_revision_id}>SourceRevision: {source.source_revision_id}</code>
            ))}
            <small>
              Подтверждения: {evidenceStatusLabel(requirement.evidence_status)} · точных ссылок {requirement.evidence_refs.length}
            </small>
            {requirement.state === 'unsupported' || requirement.state === 'unverified' ? (
              <p role="status">{requirement.state === 'unsupported' ? 'Не подтверждено источниками' : 'Не проверено'}</p>
            ) : null}
            {requirement.evidence_status === 'unverified' ? <p role="status">Источники не проверены</p> : null}
            {requirement.evidence_refs.map((evidence) => (
              evidence.transcript_locator ? scope ? (
                  <TranscriptEvidenceInspector
                    key={`${requirement.requirement_id}-${evidence.segment_id}`}
                    client={client}
                    locator={evidence.transcript_locator}
                    scope={scope}
                  />
                ) : <p key={`${requirement.requirement_id}-${evidence.segment_id}`} role="status">Рабочая область для проверки записи недоступна.</p>
              : (
                <RequirementEvidenceInspector
                  key={`${requirement.requirement_id}-${evidence.segment_id}`}
                  client={client}
                  artifactId={artifact.artifact_id}
                  artifactVersion={artifact.artifact_version}
                  requirementId={requirement.requirement_id}
                  evidence={evidence}
                />
              )
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
            {canReclassify && reclassifyingRequirementId !== requirement.requirement_id ? (
              <button type="button" onClick={() => {
                setReclassifyingRequirementId(requirement.requirement_id);
                setEditingRequirementId(null);
                setStatement('');
                setReclassificationBasis('manager_assertion');
                setReclassificationRationale('');
                setFalseConsequence('');
                setConflictResolutionRationale('');
                setIndependentAuthorshipConfirmed(false);
                commandRef.current = null;
              }}>Переклассифицировать требование</button>
            ) : null}
            {canReclassify && reclassifyingRequirementId === requirement.requirement_id ? (
              <form aria-label="Переклассификация требования" onSubmit={(event: FormEvent) => {
                event.preventDefault();
                void submitReview('reclassify_requirement', {
                  requirement_id: requirement.requirement_id,
                  statement: statement.trim(),
                  acceptance_basis: reclassificationBasis,
                  rationale: reclassificationRationale.trim(),
                  independence_attestation: 'authored_from_independent_knowledge_without_protected_text',
                  false_consequence: reclassificationBasis === 'explicit_assumption'
                    ? falseConsequence.trim() : null,
                  conflict_resolution_rationale: conflictResolutionRationale.trim() || null,
                });
              }}>
                <p>Введите новую формулировку из собственных знаний — защищённый текст источника переносить нельзя.</p>
                <label>
                  Основание
                  <select value={reclassificationBasis} onChange={(event) => {
                    setReclassificationBasis(event.target.value as 'manager_assertion' | 'explicit_assumption');
                    commandRef.current = null;
                  }}>
                    <option value="manager_assertion">Утверждение менеджера</option>
                    <option value="explicit_assumption">Явное допущение</option>
                  </select>
                </label>
                <label>
                  Новая независимая формулировка
                  <textarea value={statement} onChange={(event) => {
                    setStatement(event.target.value);
                    commandRef.current = null;
                  }} />
                </label>
                <label>
                  Обоснование
                  <textarea value={reclassificationRationale} onChange={(event) => {
                    setReclassificationRationale(event.target.value);
                    commandRef.current = null;
                  }} />
                </label>
                {reclassificationBasis === 'explicit_assumption' ? (
                  <label>
                    Последствие, если допущение неверно
                    <textarea value={falseConsequence} onChange={(event) => {
                      setFalseConsequence(event.target.value);
                      commandRef.current = null;
                    }} />
                  </label>
                ) : null}
                <label>
                  Решение конфликта (если применимо)
                  <textarea value={conflictResolutionRationale} onChange={(event) => {
                    setConflictResolutionRationale(event.target.value);
                    commandRef.current = null;
                  }} />
                </label>
                <label>
                  <input checked={independentAuthorshipConfirmed} onChange={(event) => {
                    setIndependentAuthorshipConfirmed(event.target.checked);
                    commandRef.current = null;
                  }} type="checkbox" />
                  Подтверждаю, что написал новую формулировку из собственных знаний без использования защищённого текста
                </label>
                <button disabled={
                  submitting
                  || !statement.trim()
                  || !reclassificationRationale.trim()
                  || !independentAuthorshipConfirmed
                  || (reclassificationBasis === 'explicit_assumption' && !falseConsequence.trim())
                } type="submit">{submitting ? 'Сохраняем…' : 'Создать новую версию'}</button>
                <button type="button" onClick={() => setReclassifyingRequirementId(null)}>Отмена</button>
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
      {canAccept ? (
        <div aria-label="Принятие требований">
          <p>Будет принята только показанная версия {artifact.artifact_version}.</p>
          <p>Digest версии: <code>{artifact.production_digest}</code></p>
          <p>Утверждения менеджера: {assertionCount}. Явные допущения: {assumptionCount}.</p>
          {requiresExplicitAcknowledgement ? (
            <label>
              <input
                checked={acknowledgedAcceptanceKey === acceptanceKey}
                onChange={(event) => {
                  setAcknowledgedAcceptanceKey(event.target.checked ? acceptanceKey : null);
                  commandRef.current = null;
                }}
                type="checkbox"
              />
              Подтверждаю эти количества, версию и digest, включая последствия указанных допущений
            </label>
          ) : null}
          <button disabled={
            submitting
            || (requiresExplicitAcknowledgement && acknowledgedAcceptanceKey !== acceptanceKey)
          } type="button" onClick={() => void submitReview(
            'accept_requirements', {
              artifact_digest: artifact.production_digest,
              policy_version: 'requirements-artifact-acceptance:v1',
              acknowledged_assertion_count: assertionCount,
              acknowledged_assumption_count: assumptionCount,
            },
          )}>{submitting ? 'Принимаем…' : 'Принять требования'}</button>
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

function ClarificationPrompt({
  artifact,
  canAnswer,
  clarification,
  client,
  onAnswered,
  runVersion,
}: {
  artifact: RequirementsArtifact;
  canAnswer: boolean;
  clarification: RequirementsArtifact['clarifications'][number];
  client: Client;
  onAnswered?: () => Promise<void>;
  runVersion?: number;
}) {
  const [answer, setAnswer] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const commandRef = useRef<{ commandId: string; idempotencyKey: string; answer: string } | null>(null);
  if (clarification.answer) {
    return <p role="status">Уточнение получено и учтено в версии {clarification.resulting_artifact_version}.</p>;
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!client.POST || !canAnswer || !runVersion || !answer.trim() || submitting) return;
    const command = commandRef.current ?? {
      commandId: crypto.randomUUID(),
      idempotencyKey: crypto.randomUUID(),
      answer: answer.trim(),
    };
    commandRef.current = command;
    setSubmitting(true);
    setError(null);
    try {
      const { data, error: responseError } = await client.POST('/api/v1/runs/{run_id}/commands', {
        params: {
          path: { run_id: artifact.run_id },
          header: { 'Idempotency-Key': command.idempotencyKey },
        },
        body: {
          schema_version: 1,
          command_id: command.commandId,
          command_type: 'answer_clarification',
          command_schema_version: 1,
          expected_run_version: runVersion,
          payload: {
            schema_version: 1,
            clarification_id: clarification.clarification_id,
            requirement_id: clarification.requirement_id,
            answer: command.answer,
          },
          artifact_refs: [{
            artifact_id: artifact.artifact_id,
            schema_name: 'requirements',
            schema_version: artifact.schema_version,
            artifact_version: artifact.artifact_version,
          }],
          correlation_id: artifact.run_id,
          causation_id: clarification.asked_event_id,
        },
      });
      if (responseError || !data) {
        setError('Не удалось сохранить ответ. Обновите запуск и повторите команду.');
        return;
      }
      commandRef.current = null;
      setAnswer('');
      await onAnswered?.();
    } catch {
      setError('Не удалось сохранить ответ. Обновите запуск и повторите команду.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section aria-label="Запрос уточнения">
      <h4>Нужно уточнение</h4>
      <p>{clarification.question}</p>
      {canAnswer ? (
        <form onSubmit={(event) => void submit(event)}>
          <label htmlFor={`clarification-${clarification.clarification_id}`}>Ответ менеджера</label>
          <textarea
            disabled={submitting}
            id={`clarification-${clarification.clarification_id}`}
            onChange={(event) => {
              setAnswer(event.target.value);
              commandRef.current = null;
            }}
            value={answer}
          />
          <button disabled={submitting || !answer.trim()} type="submit">
            {submitting ? 'Сохраняем…' : 'Ответить и продолжить'}
          </button>
        </form>
      ) : <p role="status">Обновите запуск, чтобы ответить на актуальную версию вопроса.</p>}
      {error ? <p role="alert">{error}</p> : null}
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

function acceptanceBasis(
  requirement: RequirementsArtifact['requirements'][number],
): 'source_validated' | 'manager_assertion' | 'explicit_assumption' {
  return 'acceptance_basis' in requirement
    ? requirement.acceptance_basis
    : 'source_validated';
}
