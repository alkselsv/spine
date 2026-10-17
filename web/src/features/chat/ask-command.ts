import type { components, paths } from '../../../api/generated/schema';
import { SpineApiError, type QaApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import { ru } from '../../i18n/resources';

type AdmissionPost = paths['/api/v1/ask/admissions']['post'];
type AdmissionRequest = NonNullable<AdmissionPost['requestBody']>['content']['application/json'];
type Admission = components['schemas']['QAAskAdmissionResultV1'];

interface AskCommand {
  body: AdmissionRequest;
  idempotencyKey: string;
}

export type AskCommandState =
  | { kind: 'idle' }
  | { kind: 'submitting' }
  | { kind: 'accepted'; admission: Admission }
  | { kind: 'failed'; message: string; retryable: boolean };

export type AskCommandResult =
  | AskCommandState
  | { kind: 'busy' }
  | { kind: 'retry-required' }
  | { kind: 'not-retryable' };

export interface AskCommandActions {
  getState(): AskCommandState;
  submit(
    sessionId: string,
    question: string,
    clarificationOfMessageId?: string,
  ): Promise<AskCommandResult>;
  retry(): Promise<AskCommandResult>;
}

export function createAskCommandActions(
  client: QaApiClient,
  scope: Scope,
  createIdempotencyKey: () => string = () => `ask-${globalThis.crypto.randomUUID()}`,
): AskCommandActions {
  let state: AskCommandState = { kind: 'idle' };
  let command: AskCommand | null = null;
  let inFlight = false;

  const actions: AskCommandActions = {
    getState: () => state,
    submit: async (sessionId, question, clarificationOfMessageId) => {
      if (inFlight) return { kind: 'busy' };
      if (state.kind === 'failed' && state.retryable) return { kind: 'retry-required' };

      const normalizedQuestion = question.trim();
      if (!normalizedQuestion) {
        state = { kind: 'failed', message: ru.chat.askEmpty, retryable: false };
        return state;
      }
      if (!isUuid(sessionId) || (clarificationOfMessageId && !isUuid(clarificationOfMessageId))) {
        state = { kind: 'failed', message: ru.chat.askFailed, retryable: false };
        return state;
      }

      const key = createIdempotencyKey();
      if (!key.trim()) {
        state = { kind: 'failed', message: ru.chat.askFailed, retryable: false };
        return state;
      }
      command = {
        body: {
          schema_version: 1,
          session_id: sessionId,
          question: normalizedQuestion,
          ...(clarificationOfMessageId
            ? { clarification_of_message_id: clarificationOfMessageId }
            : {}),
        },
        idempotencyKey: key,
      };
      return perform(command);
    },
    retry: async () => {
      if (inFlight) return { kind: 'busy' };
      if (!command || state.kind !== 'failed' || !state.retryable) {
        return { kind: 'not-retryable' };
      }
      return perform(command);
    },
  };
  return Object.freeze(actions);

  async function perform(nextCommand: AskCommand): Promise<AskCommandResult> {
    inFlight = true;
    state = { kind: 'submitting' };
    try {
      const response = await client.POST('/api/v1/ask/admissions', {
        body: nextCommand.body,
        params: { header: { 'Idempotency-Key': nextCommand.idempotencyKey } },
      });
      const envelope = response.data;
      const admission = envelope?.result;
      if (
        !envelope ||
        envelope.workspace_id !== scope.workspaceId ||
        envelope.environment_id !== scope.environmentId ||
        !['accepted', 'completed'].includes(envelope.status) ||
        !admission ||
        admission.schema_version !== 1 ||
        envelope.command_id !== admission.command_id ||
        admission.session_id !== nextCommand.body.session_id ||
        !isUuid(admission.command_id) ||
        !isUuid(admission.message_id) ||
        !isUuid(admission.run_id) ||
        !['admitted', 'running', 'terminal'].includes(admission.status)
      ) {
        state = { kind: 'failed', message: ru.chat.askFailed, retryable: false };
      } else {
        state = { kind: 'accepted', admission };
        command = null;
      }
    } catch (error) {
      state = failureState(error);
    } finally {
      inFlight = false;
    }
    return state;
  }
}

function failureState(error: unknown): AskCommandState {
  if (!(error instanceof SpineApiError)) {
    return { kind: 'failed', message: ru.chat.askFailed, retryable: false };
  }
  if (error.kind === 'authorization' || error.kind === 'authentication') {
    return { kind: 'failed', message: ru.chat.askDenied, retryable: false };
  }
  if (error.status === 409 || error.code === 'spine.persistence.idempotency_conflict') {
    return { kind: 'failed', message: ru.chat.askConflict, retryable: false };
  }
  if (error.retryability === 'after_delay') {
    return { kind: 'failed', message: ru.chat.askUnavailable, retryable: true };
  }
  if (error.retryability === 'same_command') {
    return { kind: 'failed', message: ru.chat.askFailed, retryable: true };
  }
  return { kind: 'failed', message: ru.chat.askFailed, retryable: false };
}

function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}
