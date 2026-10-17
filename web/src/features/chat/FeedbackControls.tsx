import { useEffect, useRef, useState } from 'react';

import type { components } from '../../../api/generated/schema';
import { SpineApiError, type ScopedApiClient } from '../../api/client';
import { ru } from '../../i18n/resources';
import { submitQaFeedback, type QaFeedbackRequest } from './feedback-api';
import styles from './FeedbackControls.module.css';

type FeedbackTargetKind = components['schemas']['FeedbackTargetKind'];
type FeedbackOutcome = components['schemas']['FeedbackOutcome'];

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
  const [status, setStatus] = useState<'idle' | 'submitting' | 'recorded' | 'denied' | 'unavailable' | 'conflict'>('idle');
  const [pendingCommand, setPendingCommand] = useState<{
    idempotencyKey: string;
    request: QaFeedbackRequest;
  } | null>(null);
  const requestRef = useRef<AbortController | null>(null);
  const submittingRef = useRef(false);

  useEffect(() => () => requestRef.current?.abort(), []);

  function begin(outcome: FeedbackOutcome) {
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
    void send(command);
  }

  async function send(command: { idempotencyKey: string; request: QaFeedbackRequest }) {
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

  const isDisabled = status === 'submitting' || status === 'recorded' || status === 'denied' || status === 'conflict' || Boolean(pendingCommand);
  const helpfulOutcome: FeedbackOutcome = targetKind === 'answer' ? 'answer.helpful' : 'citation.helpful';
  const incorrectOutcome: FeedbackOutcome = targetKind === 'answer' ? 'answer.incorrect' : 'citation.incorrect';
  const statusCopy = status === 'submitting'
    ? ru.chat.feedbackSubmitting
    : status === 'recorded'
      ? ru.chat.feedbackRecorded
      : status === 'denied'
        ? ru.chat.feedbackDenied
        : status === 'conflict'
          ? ru.chat.feedbackConflict
          : status === 'unavailable'
            ? ru.chat.feedbackUnavailable
            : null;

  return (
    <fieldset className={styles.controls} disabled={status === 'submitting' || status === 'recorded' || status === 'denied' || status === 'conflict'}>
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
            <button className={styles.action} disabled={isDisabled} onClick={() => begin(helpfulOutcome)} type="button">
              {ru.chat.feedbackHelpful}
            </button>
            <button className={styles.action} disabled={isDisabled} onClick={() => begin(incorrectOutcome)} type="button">
              {ru.chat.feedbackIncorrect}
            </button>
          </>
        ) : status !== 'submitting' && status !== 'denied' && status !== 'conflict' ? (
          <button className={styles.action} onClick={() => void send(pendingCommand)} type="button">
            {ru.chat.feedbackRetry}
          </button>
        ) : null}
      </div>
      {statusCopy ? (
        <p aria-live="polite" className={status === 'denied' || status === 'unavailable' || status === 'conflict' ? styles.error : styles.status} role={status === 'denied' || status === 'unavailable' || status === 'conflict' ? 'alert' : 'status'}>
          {statusCopy}
        </p>
      ) : null}
    </fieldset>
  );
}
