import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';

import type { components } from '../../../api/generated/schema';
import { SpineApiError, type ApiScope, type ScopedApiClient } from '../../api/client';
import { ApiErrorPanel } from '../../app/status';
import { ru } from '../../i18n/resources';
import { DocumentReplacementUpload } from './DocumentReplacementUpload';
import {
  createLifecycleIdempotencyKey,
  loadLifecycleOperation,
  submitDocumentTombstone,
  type DocumentLifecycleReceipt,
  type TombstoneDocumentRequest,
} from './lifecycle-api';
import styles from './DocumentDetailPage.module.css';

type LifecycleOperationStatus = components['schemas']['LifecycleOperationStatus'];
type LifecyclePhase =
  | 'idle'
  | 'submitting'
  | 'retrying'
  | 'accepted'
  | 'running'
  | 'completed'
  | 'conflict'
  | 'denied'
  | 'failed'
  | 'unavailable';

type LifecycleCommand = { kind: 'tombstone'; body: TombstoneDocumentRequest; idempotencyKey: string };

interface LifecycleFeedback {
  command: LifecycleCommand | null;
  error: unknown | null;
  phase: LifecyclePhase;
  receipt: DocumentLifecycleReceipt | null;
}

export function DocumentLifecycleActions({
  client,
  currentRevisionId,
  onRefresh,
  queryKey,
  scope,
  sourceObjectId,
}: {
  client: ScopedApiClient;
  currentRevisionId: string | null | undefined;
  onRefresh: () => void | Promise<unknown>;
  queryKey: (resource: string) => readonly unknown[];
  scope: ApiScope;
  sourceObjectId: string;
}) {
  const queryClient = useQueryClient();
  const [deletionReason, setDeletionReason] = useState<TombstoneDocumentRequest['deletion_reason']>('source_deleted');
  const [feedback, setFeedback] = useState<LifecycleFeedback>({
    command: null,
    error: null,
    phase: 'idle',
    receipt: null,
  });
  const completedOperation = useRef<string | null>(null);
  const operationId = feedback.receipt?.operation_id;
  const shouldPoll = Boolean(operationId && isPollable(feedback.receipt?.status));
  const operationQuery = useQuery({
    queryKey: [
      'spine',
      'document-lifecycle-operation',
      scope.workspaceId,
      scope.environmentId,
      sourceObjectId,
      operationId,
    ],
    queryFn: ({ signal }) => loadLifecycleOperation(client, operationId!, signal),
    enabled: shouldPoll,
    refetchInterval: (query) => (isPollable(query.state.data?.status) ? 750 : false),
    retry: false,
  });

  useEffect(() => {
    if (!operationQuery.data) return;
    applyReceipt(operationQuery.data, setFeedback);
    if (operationQuery.data.status === 'completed') completeOnce(operationQuery.data.operation_id);
  }, [operationQuery.data]);

  useEffect(() => {
    if (!operationQuery.error) return;
    setFeedback((current) => ({ ...current, error: operationQuery.error, phase: phaseFromError(operationQuery.error) }));
  }, [operationQuery.error]);

  function completeOnce(completedOperationId: string) {
    if (completedOperation.current === completedOperationId) return;
    completedOperation.current = completedOperationId;
    void queryClient.invalidateQueries({ queryKey: [...queryKey('document-detail'), sourceObjectId] });
    void queryClient.invalidateQueries({
      queryKey: ['spine', 'document-revisions', scope.workspaceId, scope.environmentId, sourceObjectId],
    });
    void queryClient.invalidateQueries({ queryKey: queryKey('projection-status') });
    void queryClient.invalidateQueries({ queryKey: queryKey('health-summary') });
    void onRefresh();
  }

  async function execute(command: LifecycleCommand, retrying: boolean) {
    setFeedback({ command, error: null, phase: retrying ? 'retrying' : 'submitting', receipt: null });
    try {
      const receipt = await submitDocumentTombstone(
        client, sourceObjectId, command.body, command.idempotencyKey, new AbortController().signal,
      );
      applyReceipt(receipt, setFeedback);
      if (receipt.status === 'completed') completeOnce(receipt.operation_id);
    } catch (error) {
      setFeedback({ command, error, phase: phaseFromError(error), receipt: null });
    }
  }

  function submitTombstone(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!currentRevisionId || isBusy(feedback.phase)) return;
    const command: LifecycleCommand = {
      kind: 'tombstone',
      body: { deletion_reason: deletionReason, expected_current_revision_id: currentRevisionId },
      idempotencyKey: createLifecycleIdempotencyKey('tombstone'),
    };
    void execute(command, false);
  }

  const canRetry = Boolean(
    feedback.command &&
      (feedback.receipt?.retryable || isExplicitlyRetryableError(feedback.error)),
  );
  const completedTombstone = feedback.receipt?.operation_kind === 'tombstone' && feedback.phase === 'completed';
  const feedbackIsError = feedback.phase === 'conflict' || feedback.phase === 'denied' || feedback.phase === 'failed' || feedback.phase === 'unavailable';

  return (
    <section className={`${styles.card} ${styles.cardWide}`} aria-labelledby="document-actions-heading">
      <header className={styles.statusHeader}>
        <div>
          <h3 id="document-actions-heading">{ru.documents.documentActions.title}</h3>
          <p className={styles.cardDescription}>{ru.documents.documentActions.description}</p>
        </div>
        <span className={styles.statusPill}>{currentRevisionId ?? ru.documents.unknown}</span>
      </header>
      <p className={styles.protectedNotice} role="note">
        {currentRevisionId ? `${ru.documents.documentActions.currentRevision}: ${currentRevisionId}` : ru.documents.documentActions.noRevision}
      </p>
      <div className={styles.lifecycleGrid}>
        <DocumentReplacementUpload
          client={client}
          currentRevisionId={currentRevisionId}
          onRefresh={onRefresh}
          queryKey={queryKey}
          scope={scope}
          sourceObjectId={sourceObjectId}
        />

        <form className={styles.lifecycleForm} onSubmit={submitTombstone}>
          <h4>{ru.documents.documentActions.tombstoneTitle}</h4>
          <p className={styles.cardDescription}>{ru.documents.documentActions.tombstoneDescription}</p>
          <label className={styles.lifecycleField}>
            {ru.documents.documentActions.deletionReason}
            <select
              name="deletion_reason"
              onChange={(event) => setDeletionReason(event.target.value as TombstoneDocumentRequest['deletion_reason'])}
              value={deletionReason}
            >
              <option value="source_deleted">{ru.documents.documentActions.sourceDeleted}</option>
              <option value="legal_erasure">{ru.documents.documentActions.legalErasure}</option>
              <option value="provider_deleted">{ru.documents.documentActions.providerDeleted}</option>
              <option value="retention_expired">{ru.documents.documentActions.retentionExpired}</option>
            </select>
          </label>
          <button className={styles.lifecycleDangerButton} disabled={!currentRevisionId || isBusy(feedback.phase)} type="submit">
            {ru.documents.documentActions.submitTombstone}
          </button>
        </form>
      </div>

      {feedback.phase !== 'idle' ? (
        <div className={feedbackIsError ? styles.lifecycleError : styles.lifecycleStatus} role={feedbackIsError && !feedback.error ? 'alert' : 'status'}>
          <span>{phaseLabel(feedback.phase)}</span>
          {feedback.command?.idempotencyKey ? <span>Idempotency-Key: <code>{feedback.command.idempotencyKey}</code></span> : null}
          {feedback.receipt?.operation_id ? <span>{ru.documents.documentActions.receipt}: <code>{feedback.receipt.operation_id}</code></span> : null}
          {feedback.receipt?.processing_run_id ? <span>{ru.documents.documentActions.processingRun}: <code>{feedback.receipt.processing_run_id}</code></span> : null}
          {feedback.receipt?.accepted_revision_id ? <span>{ru.documents.documentActions.acceptedRevision}: <code>{feedback.receipt.accepted_revision_id}</code></span> : null}
          {feedback.receipt?.canonical_acceptance_id ? <span>{ru.documents.documentActions.canonicalAcceptance}: <code>{feedback.receipt.canonical_acceptance_id}</code></span> : null}
          {feedback.receipt?.idempotency_receipt_id ? <span>{ru.documents.documentActions.idempotencyReceipt}: <code>{feedback.receipt.idempotency_receipt_id}</code></span> : null}
          {feedback.receipt?.status_url ? (
            <>
              <span>{ru.documents.documentActions.statusUrl}: <code>{feedback.receipt.status_url}</code></span>
              <button
                className={styles.buttonLink}
                disabled={!operationId || isBusy(feedback.phase)}
                onClick={() => void operationQuery.refetch()}
                type="button"
              >
                {ru.documents.documentActions.refreshReceipt}
              </button>
            </>
          ) : null}
          {completedTombstone && feedback.command?.kind === 'tombstone' ? (
            <>
              <span>{ru.documents.documentActions.tombstoneCreated}</span>
              <span>{ru.documents.documentActions.tombstoneReason}: <code>{feedback.command.body.deletion_reason}</code></span>
            </>
          ) : null}
          {feedback.error ? <ApiErrorPanel error={feedback.error} /> : null}
          {canRetry && !isBusy(feedback.phase) ? (
            <button className={styles.buttonLink} onClick={() => void execute(feedback.command!, true)} type="button">
              {ru.documents.documentActions.retry}
            </button>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function applyReceipt(receipt: DocumentLifecycleReceipt, setFeedback: Dispatch<SetStateAction<LifecycleFeedback>>) {
  setFeedback((current) => ({ ...current, error: null, phase: phaseFromReceipt(receipt), receipt }));
}

function phaseFromReceipt(receipt: DocumentLifecycleReceipt): LifecyclePhase {
  if (receipt.status === 'completed') return 'completed';
  if (receipt.status === 'conflict') return 'conflict';
  if (receipt.status === 'failed' || receipt.status === 'reconciliation_required') return 'failed';
  if (receipt.status === 'processing_dispatched') return 'running';
  return 'accepted';
}

function phaseFromError(error: unknown): LifecyclePhase {
  if (error instanceof SpineApiError && error.status === 409) return 'conflict';
  if (error instanceof SpineApiError && error.kind === 'authorization') return 'denied';
  if (error instanceof SpineApiError && error.kind === 'unavailable') return 'unavailable';
  return 'failed';
}

function phaseLabel(phase: LifecyclePhase): string {
  if (phase === 'accepted' || phase === 'submitting') return ru.documents.documentActions.accepted;
  if (phase === 'retrying') return ru.documents.documentActions.retrying;
  if (phase === 'running') return ru.documents.documentActions.running;
  if (phase === 'completed') return ru.documents.documentActions.completed;
  if (phase === 'conflict') return ru.documents.documentActions.conflict;
  if (phase === 'denied') return ru.documents.documentActions.denied;
  if (phase === 'unavailable') return ru.documents.documentActions.unavailable;
  return ru.documents.documentActions.failed;
}

function isBusy(phase: LifecyclePhase): boolean {
  return phase === 'submitting' || phase === 'retrying' || phase === 'accepted' || phase === 'running';
}

function isPollable(status: LifecycleOperationStatus | null | undefined): boolean {
  return status === 'accepted_processing_pending' || status === 'processing_dispatched';
}

function isExplicitlyRetryableError(error: unknown): boolean {
  return (
    error instanceof SpineApiError &&
    !error.code.startsWith('spine.client.') &&
    error.retryability !== 'never'
  );
}
