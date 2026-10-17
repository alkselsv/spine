import { useMemo, useState } from 'react';

import type { ScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import { createAskCancellationActions, type AskCancellationState } from './cancellation-api';
import styles from './AskCancellationControl.module.css';

export function AskCancellationControl({
  client,
  onCanonicalRefresh,
  runId,
  scope,
}: {
  client: ScopedApiClient;
  onCanonicalRefresh: () => void;
  runId: string;
  scope: Scope;
}) {
  const actions = useMemo(() => createAskCancellationActions(client, scope), [client, scope, runId]);
  const [snapshot, setSnapshot] = useState<{ runId: string; state: AskCancellationState }>({
    runId,
    state: { kind: 'idle' },
  });
  const state = snapshot.runId === runId ? snapshot.state : actions.getState();

  async function cancel() {
    const result = actions.cancel(runId);
    setSnapshot({ runId, state: actions.getState() });
    await settle(result);
  }

  async function retry() {
    const result = actions.retry();
    setSnapshot({ runId, state: actions.getState() });
    await settle(result);
  }

  async function settle(resultPromise: ReturnType<typeof actions.cancel>) {
    const result = await resultPromise;
    const nextState = result.kind === 'busy' ? actions.getState() : result;
    setSnapshot({ runId, state: nextState });
    if (nextState.kind === 'requested' || nextState.kind === 'terminal') onCanonicalRefresh();
  }

  if (state.kind === 'requested') {
    return <p className={styles.status} role="status">{ru.chat.cancelRequested}</p>;
  }
  if (state.kind === 'terminal') {
    return (
      <p className={styles.status} role="status">
        {ru.chat.cancelTerminal(cancellationOutcomeLabel(state.outcome))}
      </p>
    );
  }
  if (state.kind === 'failed' && !state.retryable) {
    return <p className={styles.error} role="alert">{state.message}</p>;
  }

  const requesting = state.kind === 'requesting';
  return (
    <div className={styles.control}>
      <button
        className={styles.button}
        disabled={requesting}
        onClick={() => void (state.kind === 'failed' ? retry() : cancel())}
        type="button"
      >
        {requesting
          ? ru.chat.cancelRequesting
          : state.kind === 'failed'
            ? ru.chat.cancelRetry
            : ru.chat.cancelRequest}
      </button>
      {state.kind === 'failed' ? <p className={styles.error} role="alert">{state.message}</p> : null}
    </div>
  );
}

function cancellationOutcomeLabel(outcome: Parameters<typeof ru.chat.cancelTerminal>[0]): string {
  switch (outcome) {
    case 'answered': return ru.chat.messageAnswered;
    case 'clarification_required': return ru.chat.messageClarificationRequired;
    case 'abstained': return ru.chat.messageAbstained;
    case 'denied': return ru.chat.messageDenied;
    case 'failed': return ru.chat.messageFailed;
    case 'cancelled': return ru.chat.messageCancelled;
    default: return ru.chat.messageOutcomeUnknown;
  }
}
