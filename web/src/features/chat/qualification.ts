import { createControlledChatApi, type ControlledChatApi } from './fixtures';
import {
  CHAT_PROTOCOL_VERSION,
  type ChatEvent,
  type Message,
  type Session,
} from './protocol';
import { SpineChatTransport, type ChatTransport } from './transport';

export interface QualificationChatHarness {
  transport: ChatTransport;
  api: ControlledChatApi;
}

/**
 * A deterministic browser fixture for Issue #167. It exercises the same
 * ChatTransport boundary as production while keeping Issue #26 unavailable.
 */
export function createQualificationChatHarness(
  scope: { workspaceId: string; environmentId: string },
  onReconnect: (attempt: number) => void,
): QualificationChatHarness {
  const session: Session = {
    id: 'demo-session',
    ...scope,
    createdAt: '2026-10-13T10:00:00.000Z',
  };
  const canonicalMessage: Message = {
    id: `message-${scope.workspaceId}-${scope.environmentId}`,
    sessionId: session.id,
    ...scope,
    runId: 'run-demo',
    role: 'assistant',
    status: 'completed',
    text: 'Канонический ответ из controlled fixture.',
    citations: [],
    createdAt: '2026-10-13T10:00:02.000Z',
  };
  const attempts = new Map<string, number>();

  const api = createControlledChatApi({
    scope,
    sessions: [session],
    messages: [canonicalMessage],
    streamPlan: (request) => {
      const attempt = (attempts.get(request.input.idempotencyKey) ?? 0) + 1;
      attempts.set(request.input.idempotencyKey, attempt);
      const started = event(session, 'run_started', { stage: 'accepted' }, 1, canonicalMessage);

      if (request.input.text.toLowerCase().includes('отмен')) {
        return cancellationStream(started);
      }
      if (attempt === 1) return failingStream([started]);

      return [
        started,
        event(
          session,
          'answer_delta',
          {
            delta: 'Скрытый потоковый фрагмент',
            disclosure: { receiptId: 'fixture-disclosure', status: 'authorized' },
          },
          2,
          canonicalMessage,
        ),
        event(session, 'completed', { outcome: 'answered' }, 3, canonicalMessage),
      ];
    },
  });

  return {
    api,
    transport: new SpineChatTransport(api, {
      heartbeatTimeoutMs: 1_000,
      maxReconnectAttempts: 2,
      onReconnect,
    }),
  };
}

function event<Type extends ChatEvent['type']>(
  session: Session,
  type: Type,
  payload: ChatEvent<Type>['payload'],
  sequence: number,
  message: Message,
): ChatEvent<Type> {
  return {
    protocolVersion: CHAT_PROTOCOL_VERSION,
    eventId: `fixture-event-${sequence}`,
    sequence,
    sessionId: session.id,
    workspaceId: session.workspaceId,
    environmentId: session.environmentId,
    messageId: message.id,
    runId: message.runId,
    traceId: 'fixture-trace',
    type,
    payload,
  } as ChatEvent<Type>;
}

async function* failingStream(events: readonly ChatEvent[]): AsyncIterable<ChatEvent> {
  yield* events;
  throw new Error('controlled disconnect');
}

async function* cancellationStream(started: ChatEvent): AsyncIterable<ChatEvent> {
  yield started;
  await new Promise<void>(() => undefined);
}
