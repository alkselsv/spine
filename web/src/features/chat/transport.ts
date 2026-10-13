import {
  ChatProtocolError,
  decodeChatEvent,
  isTerminalChatEvent,
  type ChatEvent,
  type ChatScope,
  type DisclosureReceipt,
  type FeedbackInput,
  type Message,
  type SendMessageInput,
  type Session,
} from './protocol';

export interface MessageStreamRequest {
  sessionId: string;
  requestContext: ChatRequestContext;
  input: SendMessageInput;
  resumeFromEventId: string | null;
  resumeFromSequence: number;
  signal: AbortSignal;
}

/**
 * Adapter boundary for the future Issue #26 API and SSE endpoints. The
 * transport owns protocol and lifecycle semantics; adapters own HTTP/SSE.
 */
export interface ChatApi {
  readonly requestContext: ChatRequestContext;
  createSession(): Promise<Session>;
  getSession(sessionId: string): Promise<Session>;
  listMessages(sessionId: string): Promise<Message[]>;
  getMessage(sessionId: string, messageId: string): Promise<Message>;
  rehydrateMessage(
    sessionId: string,
    input: SendMessageInput,
    messageId: string | null,
    runId: string | null,
  ): Promise<Message | null>;
  openMessageStream(request: MessageStreamRequest): Promise<AsyncIterable<unknown>>;
  checkDisclosureAuthorization(receipt: DisclosureReceipt): Promise<boolean>;
  cancelRun(runId: string, requestContext: ChatRequestContext): Promise<void>;
  submitFeedback(input: FeedbackInput): Promise<void>;
}

export interface ChatTransport {
  createSession(): Promise<Session>;
  getSession(sessionId: string): Promise<Session>;
  listMessages(sessionId: string): Promise<Message[]>;
  sendMessage(
    sessionId: string,
    input: SendMessageInput,
    signal: AbortSignal,
  ): AsyncIterable<ChatEvent>;
  cancelRun(runId: string): Promise<void>;
  submitFeedback(input: FeedbackInput): Promise<void>;
}

export interface ChatTransportOptions {
  heartbeatTimeoutMs?: number;
  maxReconnectAttempts?: number;
  onReconnect?: (attempt: number) => void;
}

export interface ChatRequestContext extends ChatScope {
  actingSubjectId: string;
  traceId: string;
}

export class SpineChatTransport implements ChatTransport {
  private readonly commands = new Map<string, ReplayableCommand>();
  private readonly feedbackCommands = new Map<string, Promise<void>>();
  private readonly canonicalMessages = new Map<string, Message>();
  private readonly heartbeatTimeoutMs: number;
  private readonly maxReconnectAttempts: number;
  private readonly onReconnect: (attempt: number) => void;

  constructor(
    private readonly api: ChatApi,
    options: ChatTransportOptions = {},
  ) {
    this.heartbeatTimeoutMs = options.heartbeatTimeoutMs ?? 30_000;
    this.maxReconnectAttempts = options.maxReconnectAttempts ?? 2;
    this.onReconnect = options.onReconnect ?? (() => undefined);
    if (!Number.isFinite(this.heartbeatTimeoutMs) || this.heartbeatTimeoutMs <= 0) {
      throw new Error('heartbeatTimeoutMs must be positive.');
    }
    if (!Number.isInteger(this.maxReconnectAttempts) || this.maxReconnectAttempts < 0) {
      throw new Error('maxReconnectAttempts must be a non-negative integer.');
    }
  }

  createSession(): Promise<Session> {
    return this.api.createSession();
  }

  getSession(sessionId: string): Promise<Session> {
    return this.api.getSession(sessionId);
  }

  listMessages(sessionId: string): Promise<Message[]> {
    return this.api.listMessages(sessionId);
  }

  cancelRun(runId: string): Promise<void> {
    return this.api.cancelRun(runId, this.api.requestContext);
  }

  submitFeedback(input: FeedbackInput): Promise<void> {
    const key = `${input.sessionId}:${input.idempotencyKey}`;
    const existing = this.feedbackCommands.get(key);
    if (existing) return existing;
    const result = this.api.submitFeedback(input);
    this.feedbackCommands.set(key, result);
    void result.catch(() => {
      if (this.feedbackCommands.get(key) === result) this.feedbackCommands.delete(key);
    });
    return result;
  }

  getCanonicalMessage(messageId: string): Message | undefined {
    return this.canonicalMessages.get(messageId);
  }

  sendMessage(
    sessionId: string,
    input: SendMessageInput,
    signal: AbortSignal,
  ): AsyncIterable<ChatEvent> {
    const key = `${sessionId}:${input.idempotencyKey}`;
    let command = this.commands.get(key);
    if (!command) {
      command = new ReplayableCommand((commandSignal, publish) =>
        this.runMessage(sessionId, input, commandSignal, publish),
      );
      this.commands.set(key, command);
    }
    return command.consume(signal);
  }

  private async runMessage(
    sessionId: string,
    input: SendMessageInput,
    signal: AbortSignal,
    publish: (event: ChatEvent) => void,
  ): Promise<void> {
    let lastEventId: string | null = null;
    let lastSequence = 0;
    let runId: string | null = null;
    let messageId: string | null = null;
    let traceId = `local-${input.idempotencyKey}`;
    let reconnectAttempts = 0;
    let terminal = false;
    const seenEventIds = new Map<string, string>();
    const seenSequences = new Map<number, string>();
    const pending = new Map<number, ChatEvent>();

    while (!terminal) {
      if (signal.aborted) {
        await this.emitCancellation(sessionId, runId, messageId, traceId, ++lastSequence, input, publish);
        return;
      }

      try {
        const stream = await this.api.openMessageStream({
          sessionId,
          requestContext: this.api.requestContext,
          input,
          resumeFromEventId: lastEventId,
          resumeFromSequence: lastSequence,
          signal,
        });
        const iterator = stream[Symbol.asyncIterator]();
        try {
          while (!terminal) {
            const next = await nextWithTimeout(
              iterator,
              this.heartbeatTimeoutMs,
              signal,
            );
            if (next.done) break;

            const event = decodeChatEvent(next.value);
            if (
              event.sessionId !== sessionId ||
              event.workspaceId !== this.api.requestContext.workspaceId ||
              event.environmentId !== this.api.requestContext.environmentId
            ) {
              throw new ChatProtocolError('The chat event belongs to another session.');
            }
            if (event.messageId) messageId = event.messageId;
            if (event.runId) runId = event.runId;
            traceId = event.traceId;

            if (isProtectedChatEvent(event)) {
              let authorized = false;
              try {
                authorized = await this.api.checkDisclosureAuthorization(event.payload.disclosure);
              } catch {
                authorized = false;
              }
              if (!authorized) {
                terminal = true;
                const denied = this.localEvent(
                  'denied',
                  { reason: 'authorization_denied' },
                  sessionId,
                  messageId,
                  runId,
                  traceId,
                  event.sequence,
                );
                await this.rehydrateCanonicalMessage(sessionId, messageId, runId, input);
                publish(denied);
                return;
              }
            }

            const fingerprint = JSON.stringify(event);
            const previousEvent = seenEventIds.get(event.eventId);
            if (previousEvent) {
              if (previousEvent !== fingerprint) {
                throw new ChatProtocolError('An event ID was reused with different content.');
              }
              continue;
            }
            const previousSequence = seenSequences.get(event.sequence);
            if (previousSequence && previousSequence !== event.eventId) {
              throw new ChatProtocolError('An event sequence was reused with different content.');
            }
            seenEventIds.set(event.eventId, fingerprint);
            seenSequences.set(event.sequence, event.eventId);
            pending.set(event.sequence, event);

            while (pending.has(lastSequence + 1)) {
              const ordered = pending.get(lastSequence + 1);
              if (!ordered) break;
              pending.delete(ordered.sequence);
              lastSequence = ordered.sequence;
              lastEventId = ordered.eventId;
              if (isTerminalChatEvent(ordered)) {
                terminal = true;
                await this.rehydrateCanonicalMessage(sessionId, ordered.messageId, runId, input);
              }
              publish(ordered);
              if (terminal) break;
            }
          }
        } finally {
          if (!terminal) void iterator.return?.();
        }
      } catch (error) {
        if (signal.aborted) {
          await this.emitCancellation(sessionId, runId, messageId, traceId, ++lastSequence, input, publish);
          return;
        }
        if (error instanceof ChatProtocolError) {
          const failed = this.localEvent('failed', {
            reason: 'protocol_error',
          }, sessionId, messageId, runId, traceId, ++lastSequence);
          await this.rehydrateCanonicalMessage(sessionId, messageId, runId, input);
          publish(failed);
          return;
        }
        if (reconnectAttempts >= this.maxReconnectAttempts) {
          const reason = error instanceof StreamTimeoutError
            ? 'stream_timeout'
            : 'stream_unavailable';
          const failed = this.localEvent('failed', {
            reason,
          }, sessionId, messageId, runId, traceId, ++lastSequence);
          await this.rehydrateCanonicalMessage(sessionId, messageId, runId, input);
          publish(failed);
          return;
        }
        reconnectAttempts += 1;
        this.onReconnect(reconnectAttempts);
        continue;
      }

      if (terminal) return;
      if (reconnectAttempts >= this.maxReconnectAttempts) {
        const failed = this.localEvent('failed', {
          reason: 'stream_unavailable',
        }, sessionId, messageId, runId, traceId, ++lastSequence);
        await this.rehydrateCanonicalMessage(sessionId, messageId, runId, input);
        publish(failed);
        return;
      }
      reconnectAttempts += 1;
      this.onReconnect(reconnectAttempts);
    }
  }

  private async emitCancellation(
    sessionId: string,
    runId: string | null,
    messageId: string | null,
    traceId: string,
    sequence: number,
    input: SendMessageInput,
    publish: (event: ChatEvent) => void,
  ): Promise<void> {
    if (runId) {
      try {
        await this.api.cancelRun(runId, this.api.requestContext);
      } catch {
        // Cancellation is fail-safe: the public outcome remains cancellation.
      }
    }
    const event = this.localEvent(
      'cancelled',
      { reason: 'request_cancelled' },
      sessionId,
      messageId,
      runId,
      traceId,
      sequence,
    );
    await this.rehydrateCanonicalMessage(sessionId, messageId, runId, input);
    publish(event);
  }

  private async rehydrateCanonicalMessage(
    sessionId: string,
    messageId: string | null,
    runId: string | null,
    input: SendMessageInput,
  ): Promise<void> {
    try {
      if (messageId) {
        const message = await this.api.getMessage(sessionId, messageId);
        if (message.sessionId === sessionId) this.canonicalMessages.set(message.id, message);
        return;
      }
      const message = await this.api.rehydrateMessage(sessionId, input, null, runId);
      if (message) this.canonicalMessages.set(message.id, message);
    } catch {
      // The terminal event is already disclosure-safe; hydration failure is a
      // partial transport failure surfaced by the query layer on a later read.
    }
  }

  private localEvent<Type extends ChatEvent['type']>(
    type: Type,
    payload: ChatEvent<Type>['payload'],
    sessionId: string,
    messageId: string | null,
    runId: string | null,
    traceId: string,
    sequence: number,
  ): ChatEvent<Type> {
    return {
      protocolVersion: 1,
      eventId: `local-${type}-${sequence}`,
      sequence,
      sessionId,
      workspaceId: this.api.requestContext.workspaceId,
      environmentId: this.api.requestContext.environmentId,
      messageId,
      runId,
      traceId,
      type,
      payload,
    } as ChatEvent<Type>;
  }
}

class ReplayableCommand {
  private readonly abortController = new AbortController();
  private readonly events: ChatEvent[] = [];
  private readonly waiters: Array<() => void> = [];
  private started = false;
  private completed = false;
  private failure: unknown;

  constructor(
    private readonly run: (
      signal: AbortSignal,
      publish: (event: ChatEvent) => void,
    ) => Promise<void>,
  ) {}

  consume(signal: AbortSignal): AsyncIterable<ChatEvent> {
    return this.consumeEvents(signal);
  }

  private async *consumeEvents(signal: AbortSignal): AsyncIterable<ChatEvent> {
    this.start();
    const abort = () => this.abortController.abort();
    signal.addEventListener('abort', abort, { once: true });
    try {
      let index = 0;
      while (true) {
        if (index < this.events.length) {
          const event = this.events[index++];
          if (event) yield event;
          continue;
        }
        if (this.completed) {
          if (this.failure) throw this.failure;
          return;
        }
        await new Promise<void>((resolve) => this.waiters.push(resolve));
      }
    } finally {
      signal.removeEventListener('abort', abort);
    }
  }

  private start(): void {
    if (this.started) return;
    this.started = true;
    void this.run(this.abortController.signal, (event) => {
      this.events.push(event);
      this.waiters.splice(0).forEach((resolve) => resolve());
    })
      .catch((error: unknown) => {
        this.failure = error;
      })
      .finally(() => {
        this.completed = true;
        this.waiters.splice(0).forEach((resolve) => resolve());
      });
  }

}

class StreamTimeoutError extends Error {
  constructor() {
    super('The chat stream heartbeat timed out.');
    this.name = 'StreamTimeoutError';
  }
}

async function nextWithTimeout<T>(
  iterator: AsyncIterator<T>,
  timeoutMs: number,
  signal: AbortSignal,
): Promise<IteratorResult<T>> {
  let timeout: ReturnType<typeof setTimeout> | undefined;
  let abort: (() => void) | undefined;
  try {
    const next = iterator.next();
    const timeoutPromise = new Promise<never>((_, reject) => {
      timeout = setTimeout(() => reject(new StreamTimeoutError()), timeoutMs);
    });
    const abortPromise = new Promise<never>((_, reject) => {
      abort = () => reject(new AbortError());
      signal.addEventListener('abort', abort, { once: true });
    });
    return await Promise.race([next, timeoutPromise, abortPromise]);
  } finally {
    if (timeout) clearTimeout(timeout);
    if (abort) signal.removeEventListener('abort', abort);
  }
}

class AbortError extends Error {
  constructor() {
    super('The chat stream was cancelled.');
    this.name = 'AbortError';
  }
}

export type { ChatEvent, FeedbackInput, Message, SendMessageInput, Session } from './protocol';

function isProtectedChatEvent(
  event: ChatEvent,
): event is ChatEvent<'answer_delta' | 'citation_added'> {
  return event.type === 'answer_delta' || event.type === 'citation_added';
}
