/**
 * Controlled transport fixtures for Issue #166.
 *
 * These types intentionally model the future Issue #26 API/SSE adapter without
 * claiming that those backend endpoints exist yet. Production code can replace
 * this adapter while preserving the ChatTransport and protocol contracts.
 */

import type {
  ChatApi,
  ChatRequestContext,
  MessageStreamRequest,
} from './transport';
import type { FeedbackInput, Message, SendMessageInput, Session } from './protocol';
import type { DisclosureReceipt } from './protocol';

export type ControlledStreamPlan = (
  request: MessageStreamRequest,
) => AsyncIterable<unknown> | readonly unknown[];

export interface ControlledChatApiOptions {
  scope: { workspaceId: string; environmentId: string };
  sessions: readonly Session[];
  messages: readonly Message[];
  streamPlan: ControlledStreamPlan;
  disclosureAuthorized?: boolean | (() => boolean | Promise<boolean>);
}

export function createControlledChatApi(options: ControlledChatApiOptions): ControlledChatApi {
  return new ControlledChatApi(options);
}

export class ControlledChatApi implements ChatApi {
  readonly requestContext: ChatRequestContext;
  readonly streamCalls: MessageStreamRequest[] = [];
  readonly getMessageCalls: string[] = [];
  readonly rehydrateCalls: string[] = [];
  readonly listMessagesCalls: string[] = [];
  readonly cancelledRuns: string[] = [];
  readonly feedbackInputs: FeedbackInput[] = [];
  disclosureAuthorized = true;
  failNextFeedback = false;

  private readonly sessionsById: Map<string, Session>;
  private readonly messagesById: Map<string, Message>;
  private readonly options: ControlledChatApiOptions;

  constructor(options: ControlledChatApiOptions) {
    this.options = options;
    this.requestContext = Object.freeze({
      ...options.scope,
      actingSubjectId: 'fixture-administrator',
      traceId: 'fixture-trace',
    });
    this.sessionsById = new Map(options.sessions.map((session) => [session.id, session]));
    this.messagesById = new Map(options.messages.map((message) => [message.id, message]));
  }

  async createSession(): Promise<Session> {
    const session = [...this.sessionsById.values()].find((candidate) =>
      sameScope(candidate, this.requestContext),
    );
    if (!session) throw new Error('Controlled fixture has no session.');
    return session;
  }

  async getSession(sessionId: string): Promise<Session> {
    const session = this.sessionsById.get(sessionId);
    if (!session || !sameScope(session, this.options.scope)) throw new Error('Fixture session is unavailable.');
    return session;
  }

  async listMessages(sessionId: string): Promise<Message[]> {
    await this.getSession(sessionId);
    this.listMessagesCalls.push(sessionId);
    return [...this.messagesById.values()].filter(
      (message) => message.sessionId === sessionId && sameScope(message, this.requestContext),
    );
  }

  async getMessage(sessionId: string, messageId: string): Promise<Message> {
    await this.getSession(sessionId);
    const message = this.messagesById.get(messageId);
    if (!message || message.sessionId !== sessionId || !sameScope(message, this.requestContext)) {
      throw new Error('Fixture message is unavailable.');
    }
    this.getMessageCalls.push(`${sessionId}:${messageId}`);
    return message;
  }

  async rehydrateMessage(
    sessionId: string,
    _input: SendMessageInput,
    messageId: string | null,
    runId: string | null,
  ): Promise<Message | null> {
    await this.getSession(sessionId);
    this.rehydrateCalls.push(`${sessionId}:${messageId ?? runId ?? 'command'}`);
    return [...this.messagesById.values()].find(
      (message) =>
        message.sessionId === sessionId &&
        sameScope(message, this.requestContext) &&
        (messageId === null || message.id === messageId) &&
        (runId === null || message.runId === runId),
    ) ?? null;
  }

  async openMessageStream(request: MessageStreamRequest): Promise<AsyncIterable<unknown>> {
    await this.getSession(request.sessionId);
    const planned = this.options.streamPlan(request);
    const stream = isAsyncIterable(planned) ? planned : asAsyncIterable(planned);
    this.streamCalls.push(request);
    return stream;
  }

  async checkDisclosureAuthorization(receipt: DisclosureReceipt): Promise<boolean> {
    if (receipt.status !== 'authorized') return false;
    if (typeof this.options.disclosureAuthorized === 'function') {
      return this.options.disclosureAuthorized();
    }
    return this.disclosureAuthorized && this.options.disclosureAuthorized !== false;
  }

  async cancelRun(runId: string, requestContext: ChatRequestContext): Promise<void> {
    if (!sameScope(requestContext, this.requestContext)) throw new Error('Fixture cancellation scope is unavailable.');
    const ownedRun = [...this.messagesById.values()].some(
      (message) => message.runId === runId && sameScope(message, this.requestContext),
    );
    if (!ownedRun) throw new Error('Fixture run is unavailable.');
    this.cancelledRuns.push(runId);
  }

  async submitFeedback(input: FeedbackInput): Promise<void> {
    await this.getSession(input.sessionId);
    if (!sameScope(input, this.requestContext)) throw new Error('Fixture feedback scope is unavailable.');
    if (this.failNextFeedback) {
      this.failNextFeedback = false;
      throw new Error('Controlled feedback failure.');
    }
    this.feedbackInputs.push(input);
  }
}

function sameScope(
  value: { workspaceId: string; environmentId: string },
  scope: { workspaceId: string; environmentId: string },
): boolean {
  return value.workspaceId === scope.workspaceId && value.environmentId === scope.environmentId;
}

function isAsyncIterable(value: AsyncIterable<unknown> | readonly unknown[]): value is AsyncIterable<unknown> {
  return Symbol.asyncIterator in Object(value);
}

async function* asAsyncIterable(values: readonly unknown[]): AsyncIterable<unknown> {
  yield* values;
}

export type { FeedbackInput, Message, Session };
