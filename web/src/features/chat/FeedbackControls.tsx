import { useEffect, useRef, useState } from 'react';

import type { components } from '../../../api/generated/schema';
import { SpineApiError, type ScopedApiClient } from '../../api/client';
import { ru } from '../../i18n/resources';
import { submitQaFeedback, type QaFeedbackRequest } from './feedback-api';
import styles from './FeedbackControls.module.css';

type FeedbackTargetKind = components['schemas']['FeedbackTargetKind'];
type FeedbackOutcome = components['schemas']['FeedbackOutcome'];
type FeedbackUiStatus = 'idle' | 'submitting' | 'recorded' | 'denied' | 'unavailable' | 'conflict';

const FEEDBACK_STATUS_COPY: Record<FeedbackUiStatus, string | null> = {
  idle: null,
  submitting: ru.chat.feedbackSubmitting,
  recorded: ru.chat.feedbackRecorded,
  denied: ru.chat.feedbackDenied,
  unavailable: ru.chat.feedbackUnavailable,
  conflict: ru.chat.feedbackConflict,
};

export function FeedbackControls({
  client,
  targetKind,
  targetId,
  runId,
  messageId,
}: {
  client: ScopedApiClient;
  targetKind: FeedbackTargetKind;
  targetId: string;
  runId: string;
  messageId: string;
}) {
  const [comment, setComment] = useState('');
  const [status, setStatus] = useState<FeedbackUiStatus>('idle');
  const [pendingCommand, setPendingCommand] = useState<{
    idempotencyKey: string;
    request: QaFeedbackRequest;
  } | null>(null);
  const requestRef = useRef<AbortController | null>(null);
  const submittingRef = useRef(false);

  useEffect(() => () => requestRef.current?.abort(), []);

  function startFeedbackSubmission(outcome: FeedbackOutcome) {
    if (submittingRef.current || pendingCommand || status !== 'idle') return;
    const normalizedComment = comment.trim() ? comment : undefined;
    const request: QaFeedbackRequest = {
      target_kind: targetKind,
      target_id: targetId,
      outcome,
      run_id: runId,
      message_id: messageId,
      ...(targetKind === 'citation' ? { citation_id: targetId } : {}),
      ...(normalizedComment ? { comment: normalizedComment } : {}),
    };
    const command = { idempotencyKey: crypto.randomUUID(), request };
    setPendingCommand(command);
    void sendFeedbackCommand(command);
  }

  async function sendFeedbackCommand(command: { idempotencyKey: string; request: QaFeedbackRequest }) {
    if (submittingRef.current) return;
    submittingRef.current = true;
    const controller = new AbortController();
    requestRef.current = controller;
    setStatus('submitting');
    try {
      await submitQaFeedback(client, {
        ...command,
        signal: controller.signal,
      });
      setPendingCommand(null);
      setComment('');
      setStatus('recorded');
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof SpineApiError && (error.status === 401 || error.status === 403 || error.kind === 'authorization' || error.kind === 'authentication')) {
        setStatus('denied');
      } else if (error instanceof SpineApiError && error.status === 409) {
        setStatus('conflict');
      } else {
        setStatus('unavailable');
      }
    } finally {
      if (requestRef.current === controller) requestRef.current = null;
      submittingRef.current = false;
    }
  }

  const isSubmitting = status === 'submitting';
  const isTerminal = status === 'recorded' || status === 'denied' || status === 'conflict';
  const isFailure = status === 'denied' || status === 'unavailable' || status === 'conflict';
  const isDisabled = isSubmitting || isTerminal || Boolean(pendingCommand);
  const canRetry = Boolean(pendingCommand) && status === 'unavailable';
  const helpfulOutcome: FeedbackOutcome = targetKind === 'answer' ? 'answer.helpful' : 'citation.helpful';
  const incorrectOutcome: FeedbackOutcome = targetKind === 'answer' ? 'answer.incorrect' : 'citation.incorrect';
  const statusCopy = FEEDBACK_STATUS_COPY[status];

  return (
    <fieldset className={styles.controls} disabled={isSubmitting || isTerminal}>
      <legend>{targetKind === 'answer' ? ru.chat.feedbackAnswerLabel : ru.chat.feedbackCitationLabel}</legend>
      <label className={styles.commentLabel}>
        {ru.chat.feedbackCommentLabel}
        <textarea
          disabled={Boolean(pendingCommand)}
          maxLength={1_000}
          onChange={(event) => setComment(event.target.value)}
          value={comment}
          aria-describedby={`feedback-comment-hint-${targetKind}-${targetId}`}
        />
      </label>
      <span className={styles.hint} id={`feedback-comment-hint-${targetKind}-${targetId}`}>
        {ru.chat.feedbackCommentHint}
      </span>
      <div className={styles.actions}>
        {!pendingCommand ? (
          <>
            <button className={styles.action} disabled={isDisabled} onClick={() => startFeedbackSubmission(helpfulOutcome)} type="button">
              {ru.chat.feedbackHelpful}
            </button>
            <button className={styles.action} disabled={isDisabled} onClick={() => startFeedbackSubmission(incorrectOutcome)} type="button">
              {ru.chat.feedbackIncorrect}
            </button>
          </>
        ) : canRetry ? (
          <button className={styles.action} onClick={() => void sendFeedbackCommand(pendingCommand)} type="button">
            {ru.chat.feedbackRetry}
          </button>
        ) : null}
      </div>
      {statusCopy ? (
        <p aria-live="polite" className={isFailure ? styles.error : styles.status} role={isFailure ? 'alert' : 'status'}>
          {statusCopy}
        </p>
      ) : null}
    </fieldset>
  );
}
