import type { QaApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import {
  ChatProtocolError,
  decodeChatEventStream,
  isTerminalChatEvent,
  type ChatEvent,
} from './protocol';

export interface ObserveQAMessageEventsOptions {
  client: QaApiClient;
  maxReconnectAttempts?: number;
  messageId: string;
  onEvent: (event: ChatEvent) => void;
  onReconnect?: (attempt: number) => void;
  pollIntervalMs?: number;
  runId: string;
  scope: Scope;
  sessionId: string;
  signal: AbortSignal;
}

/** Observe the persisted production event stream without making it workflow state. */
export async function observeQAMessageEvents({
  client,
  maxReconnectAttempts = 8,
  messageId,
  onEvent,
  onReconnect = () => undefined,
  pollIntervalMs = 750,
  runId,
  scope,
  sessionId,
  signal,
}: ObserveQAMessageEventsOptions): Promise<void> {
  let lastEventId: string | undefined;
  let lastSequence = 0;
  let reconnectAttempts = 0;
  const observed = new Map<string, string>();

  while (!signal.aborted) {
    try {
      const response = await client.GET(
        '/api/v1/qa/sessions/{session_id}/messages/{message_id}/events',
        {
          params: {
            path: { session_id: sessionId, message_id: messageId },
            query: { run_id: runId, last_sequence: lastSequence, limit: 100, poll: true },
            header: { 'Last-Event-ID': lastEventId },
          },
          signal,
        },
      );
      if (typeof response.data !== 'string') throw new Error('The Q&A event stream is unavailable.');
      const { events } = decodeChatEventStream(response.data);
      for (const event of events) {
        assertEventScope(event, { messageId, runId, scope, sessionId });
        const fingerprint = eventFingerprint(event);
        const previous = observed.get(event.eventId);
        if (previous && previous !== fingerprint) throw new Error('The Q&A event stream is inconsistent.');
        if (previous) continue;
        if (event.sequence <= lastSequence) throw new Error('The Q&A event stream is out of order.');
        observed.set(event.eventId, fingerprint);
        lastEventId = event.eventId;
        lastSequence = event.sequence;
        onEvent(event);
        if (isTerminalChatEvent(event)) return;
      }
      reconnectAttempts = 0;
      await abortableDelay(pollIntervalMs, signal);
    } catch (error) {
      if (signal.aborted || isAbortError(error)) return;
      if (error instanceof ChatProtocolError) throw error;
      reconnectAttempts += 1;
      if (reconnectAttempts > maxReconnectAttempts) {
        throw new Error('The Q&A event stream could not be resumed.');
      }
      onReconnect(reconnectAttempts);
      await abortableDelay(pollIntervalMs, signal);
    }
  }
}

function assertEventScope(
  event: ChatEvent,
  expected: Pick<ObserveQAMessageEventsOptions, 'messageId' | 'runId' | 'scope' | 'sessionId'>,
): void {
  if (
    event.workspaceId !== expected.scope.workspaceId ||
    event.environmentId !== expected.scope.environmentId ||
    event.sessionId !== expected.sessionId ||
    event.messageId !== expected.messageId ||
    event.runId !== expected.runId
  ) {
    throw new ChatProtocolError('The Q&A event stream scope is invalid.');
  }
}

function eventFingerprint(event: ChatEvent): string {
  const serialized = JSON.stringify(event);
  let hash = 2166136261;
  for (let index = 0; index < serialized.length; index += 1) {
    hash ^= serialized.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `${event.sequence}:${event.type}:${(hash >>> 0).toString(16)}`;
}

function abortableDelay(delayMs: number, signal: AbortSignal): Promise<void> {
  if (delayMs <= 0 || signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const timeout = window.setTimeout(resolve, delayMs);
    signal.addEventListener('abort', () => {
      window.clearTimeout(timeout);
      resolve();
    }, { once: true });
  });
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}
