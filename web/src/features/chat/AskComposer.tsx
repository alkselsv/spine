import { useId, useMemo, useState, type FormEvent } from 'react';

import type { QaApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import { createAskCommandActions, type AskCommandState } from './ask-command';
import type { components } from '../../../api/generated/schema';
import styles from './SessionBrowserPage.module.css';

type Admission = components['schemas']['QAAskAdmissionResultV1'];

export function AskComposer({
  client,
  clarificationTarget,
  onAccepted,
  onCancelClarification,
  scope,
  sessionId,
}: {
  client: QaApiClient;
  clarificationTarget: string | null;
  onAccepted: (admission: Admission) => void;
  onCancelClarification: () => void;
  scope: Scope;
  sessionId: string;
}) {
  const inputId = useId();
  const actions = useMemo(
    () => createAskCommandActions(client, scope),
    [client, scope, sessionId],
  );
  const [question, setQuestion] = useState('');
  const [state, setState] = useState<AskCommandState>({ kind: 'idle' });
  const retryRequired = state.kind === 'failed' && state.retryable;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (retryRequired) return;
    await settle(actions.submit(sessionId, question, clarificationTarget ?? undefined));
  }

  async function retry() {
    await settle(actions.retry());
  }

  async function settle(resultPromise: ReturnType<typeof actions.submit>) {
    const result = await resultPromise;
    const current = result.kind === 'busy' || result.kind === 'retry-required' || result.kind === 'not-retryable'
      ? actions.getState()
      : result;
    setState(current);
    if (current.kind === 'accepted') {
      setQuestion('');
      onAccepted(current.admission);
    }
  }

  return (
    <form className={styles.askForm} onSubmit={(event) => void submit(event)}>
      <h3>{ru.chat.askTitle}</h3>
      {clarificationTarget ? (
        <div className={styles.clarificationContext} role="status">
          <span>{ru.chat.clarificationReply}</span>
          <code>{clarificationTarget}</code>
          <button className={styles.textButton} onClick={onCancelClarification} type="button">
            {ru.chat.cancelClarification}
          </button>
        </div>
      ) : null}
      <label htmlFor={inputId}>{ru.chat.inputLabel}</label>
      <textarea
        id={inputId}
        disabled={state.kind === 'submitting' || retryRequired}
        onChange={(event) => setQuestion(event.target.value)}
        placeholder={ru.chat.inputPlaceholder}
        value={question}
      />
      <div className={styles.askActions}>
        <button
          className={styles.primaryButton}
          disabled={state.kind === 'submitting' || retryRequired || !question.trim()}
          type="submit"
        >
          {state.kind === 'submitting' ? ru.chat.askSubmitting : ru.chat.send}
        </button>
        {retryRequired ? (
          <button className={styles.secondaryButton} onClick={() => void retry()} type="button">
            {ru.chat.askRetry}
          </button>
        ) : null}
      </div>
      {state.kind === 'submitting' ? <p role="status">{ru.chat.askSubmittingDescription}</p> : null}
      {state.kind === 'accepted' ? <p role="status">{ru.chat.askAccepted}</p> : null}
      {state.kind === 'failed' ? <p role="alert">{state.message}</p> : null}
    </form>
  );
}
