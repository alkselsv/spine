import type { components, paths } from '../../../api/generated/schema';
import { SpineApiError, type ScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import { ru } from '../../i18n/resources';

type CancelPost = paths['/api/v1/ask/{run_id}/cancel']['post'];
type CancelRequest = NonNullable<CancelPost['requestBody']>['content']['application/json'];
type CancelResult = components['schemas']['QACancelResultV1'];
type Outcome = components['schemas']['AnswerQuestionOutcome'];

export type AskCancellationState =
  | { kind: 'idle' }
  | { kind: 'requesting' }
  | { kind: 'requested'; runId: string }
  | { kind: 'terminal'; runId: string; outcome: Outcome }
  | { kind: 'failed'; message: string; retryable: boolean };

export type AskCancellationResult = AskCancellationState | { kind: 'busy' };

export interface AskCancellationActions {
  getState(): AskCancellationState;
  cancel(runId: string): Promise<AskCancellationResult>;
  retry(): Promise<AskCancellationResult>;
}

export function createAskCancellationActions(
  client: ScopedApiClient,
  scope: Scope,
  createIdempotencyKey: () => string = () => `qa-cancel-${globalThis.crypto.randomUUID()}`,
): AskCancellationActions {
  let state: AskCancellationState = { kind: 'idle' };
  let command: { runId: string; idempotencyKey: string; body: CancelRequest } | null = null;
  let inFlight = false;

  const actions: AskCancellationActions = {
    getState: () => state,
    cancel: async (runId) => {
      if (inFlight) return { kind: 'busy' };
      if (state.kind === 'requested' || state.kind === 'terminal') return state;
      if (state.kind === 'failed' && state.retryable) return state;
      if (!isUuid(runId)) {
        state = { kind: 'failed', message: ru.chat.cancelFailed, retryable: false };
        return state;
      }
      const idempotencyKey = createIdempotencyKey();
      if (!idempotencyKey.trim()) {
        state = { kind: 'failed', message: ru.chat.cancelFailed, retryable: false };
        return state;
      }
      command = { runId, idempotencyKey, body: { schema_version: 1 } };
      return perform(command);
    },
    retry: async () => {
      if (inFlight) return { kind: 'busy' };
      if (!command || state.kind !== 'failed' || !state.retryable) return state;
      return perform(command);
    },
  };
  return Object.freeze(actions);

  async function perform(nextCommand: NonNullable<typeof command>): Promise<AskCancellationResult> {
    inFlight = true;
    state = { kind: 'requesting' };
    try {
      const response = await client.POST('/api/v1/ask/{run_id}/cancel', {
        params: {
          path: { run_id: nextCommand.runId },
          header: {
            Accept: 'application/vnd.spine.qa-cancel+json;version=1',
            'Idempotency-Key': nextCommand.idempotencyKey,
          },
        },
        body: nextCommand.body,
      });
      const envelope = response.data;
      const result = envelope?.result as CancelResult | undefined;
      const httpStatus = response.response.status;
      const expectedStatus = httpStatus === 202 ? 'accepted' : 'completed';
      if (
        !envelope ||
        (httpStatus !== 200 && httpStatus !== 202) ||
        envelope.schema_version !== 1 ||
        envelope.workspace_id !== scope.workspaceId ||
        envelope.environment_id !== scope.environmentId ||
        envelope.status !== expectedStatus ||
        !result ||
        result.schema_version !== 1 ||
        !isUuid(envelope.command_id) ||
        envelope.command_id !== result.command_id ||
        !isUuid(result.command_id) ||
        result.run_id.toLowerCase() !== nextCommand.runId.toLowerCase() ||
        (response.response.status === 202 && result.status !== 'cancellation_requested') ||
        (response.response.status === 200 && result.status !== 'terminal') ||
        (result.status === 'terminal' && !isOutcome(result.outcome))
      ) {
        state = { kind: 'failed', message: ru.chat.cancelFailed, retryable: false };
      } else if (result.status === 'cancellation_requested') {
        state = { kind: 'requested', runId: result.run_id };
        command = null;
      } else {
        state = { kind: 'terminal', runId: result.run_id, outcome: result.outcome as Outcome };
        command = null;
      }
    } catch (error) {
      state = cancellationFailureState(error);
    } finally {
      inFlight = false;
    }
    return state;
  }
}

function cancellationFailureState(error: unknown): AskCancellationState {
  if (!(error instanceof SpineApiError)) {
    return { kind: 'failed', message: ru.chat.cancelFailed, retryable: false };
  }
  if (error.kind === 'authorization' || error.kind === 'authentication') {
    return { kind: 'failed', message: ru.chat.cancelDenied, retryable: false };
  }
  if (error.status === 404 || error.status === 409) {
    return { kind: 'failed', message: ru.chat.cancelUnavailable, retryable: false };
  }
  if (error.retryability === 'after_delay') {
    return { kind: 'failed', message: ru.chat.cancelUnavailable, retryable: true };
  }
  return { kind: 'failed', message: ru.chat.cancelFailed, retryable: error.retryability === 'same_command' };
}

function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function isOutcome(value: unknown): value is Outcome {
  return (
    value === 'answered' ||
    value === 'clarification_required' ||
    value === 'abstained' ||
    value === 'denied' ||
    value === 'failed' ||
    value === 'cancelled'
  );
}
