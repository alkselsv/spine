/**
 * Spine-owned diagnostic Chat protocol.
 *
 * This module is deliberately independent from React, a UI library, the
 * generated API client and any runtime that may eventually produce SSE.
 */

export const CHAT_PROTOCOL_VERSION = 1 as const;

export type ChatEventType =
  | 'run_started'
  | 'stage_changed'
  | 'answer_delta'
  | 'citation_added'
  | 'clarification_required'
  | 'abstained'
  | 'denied'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'heartbeat';

export type ChatTerminalEventType =
  | 'abstained'
  | 'denied'
  | 'completed'
  | 'failed'
  | 'cancelled';

export type MessageStatus = 'pending' | 'completed' | 'abstained' | 'denied' | 'failed' | 'cancelled';

export interface ChatScope {
  workspaceId: string;
  environmentId: string;
}

export interface Session extends ChatScope {
  id: string;
  createdAt: string;
}

export interface Citation {
  citationId: string;
  claimId: string;
  sourceRevisionId: string;
  locator: {
    kind: 'page' | 'sheet_row' | 'message' | 'characters';
    value: string;
  };
}

export interface Message {
  id: string;
  sessionId: string;
  workspaceId: string;
  environmentId: string;
  runId: string;
  role: 'user' | 'assistant';
  status: MessageStatus;
  text: string;
  citations: Citation[];
  createdAt: string;
}

export interface SendMessageInput {
  text: string;
  idempotencyKey: string;
}

export interface FeedbackInput {
  sessionId: string;
  messageId: string;
  workspaceId: string;
  environmentId: string;
  rating: 'helpful' | 'not_helpful';
  citationIds?: string[];
  comment?: string;
  idempotencyKey: string;
}

export interface DisclosureReceipt {
  receiptId: string;
  status: 'authorized';
}

interface ChatEventBase {
  protocolVersion: typeof CHAT_PROTOCOL_VERSION;
  eventId: string;
  sequence: number;
  sessionId: string;
  workspaceId: string;
  environmentId: string;
  messageId: string | null;
  runId: string | null;
  traceId: string;
}

export type ChatEventPayload = {
  run_started: { stage: string };
  stage_changed: { stage: string };
  answer_delta: { delta: string; disclosure: DisclosureReceipt };
  citation_added: { citation: Citation; disclosure: DisclosureReceipt };
  clarification_required: { question: string };
  abstained: { reason: 'insufficient_evidence' | 'scope_incomplete' | 'clarification_required' };
  denied: { reason: 'authorization_denied' };
  completed: { outcome: 'answered' | 'clarification_required' };
  failed: { reason: 'validation_failed' | 'stream_timeout' | 'stream_unavailable' | 'protocol_error' | 'internal_failure' };
  cancelled: { reason: 'request_cancelled' };
  heartbeat: Record<never, never>;
};

export type ChatEvent<Type extends ChatEventType = ChatEventType> = ChatEventBase & {
  type: Type;
  payload: ChatEventPayload[Type];
};

export class ChatProtocolError extends Error {
  constructor(message = 'The chat stream contains an invalid event.') {
    super(message);
    this.name = 'ChatProtocolError';
  }
}

const eventTypes = new Set<ChatEventType>([
  'run_started',
  'stage_changed',
  'answer_delta',
  'citation_added',
  'clarification_required',
  'abstained',
  'denied',
  'completed',
  'failed',
  'cancelled',
  'heartbeat',
]);

const forbiddenKeys = new Set([
  'candidate_output',
  'candidateoutput',
  'chain_of_thought',
  'chainofthought',
  'credentials',
  'provider_error',
  'providererror',
  'raw_provider_payload',
  'rawproviderpayload',
]);

export function decodeChatEvent(value: unknown): ChatEvent {
  rejectForbiddenFields(value);
  if (!isRecord(value) || value.protocolVersion !== CHAT_PROTOCOL_VERSION) {
    throw new ChatProtocolError('The chat protocol version is not supported.');
  }
  if (
    !nonEmptyString(value.eventId) ||
    !positiveInteger(value.sequence) ||
    !nonEmptyString(value.sessionId) ||
    !nonEmptyString(value.workspaceId) ||
    !nonEmptyString(value.environmentId) ||
    !nullableString(value.messageId) ||
    !nullableString(value.runId) ||
    !nonEmptyString(value.traceId) ||
    typeof value.type !== 'string' ||
    !eventTypes.has(value.type as ChatEventType) ||
    !isRecord(value.payload)
  ) {
    throw new ChatProtocolError('The chat event envelope is malformed.');
  }

  validatePayload(value.type as ChatEventType, value.payload);
  return value as unknown as ChatEvent;
}

export function isTerminalChatEvent(event: ChatEvent): event is ChatEvent<ChatTerminalEventType> {
  return ['abstained', 'denied', 'completed', 'failed', 'cancelled'].includes(event.type);
}

/**
 * Convert canonical Python code-point boundaries to JavaScript UTF-16 DOM
 * offsets without normalizing or otherwise changing the accepted text.
 */
export function codePointSpanToUtf16(
  text: string,
  startOffset: number,
  endOffset: number,
): { startOffset: number; endOffset: number } {
  const codePoints = Array.from(text);
  if (
    !Number.isSafeInteger(startOffset) ||
    !Number.isSafeInteger(endOffset) ||
    startOffset < 0 ||
    endOffset <= startOffset ||
    endOffset > codePoints.length
  ) {
    throw new ChatProtocolError('The accepted claim span is malformed.');
  }
  return {
    startOffset: codePoints.slice(0, startOffset).join('').length,
    endOffset: codePoints.slice(0, endOffset).join('').length,
  };
}

function validatePayload(type: ChatEventType, payload: Record<string, unknown>): void {
  switch (type) {
    case 'run_started':
    case 'stage_changed':
      requireNonEmptyString(payload.stage);
      return;
    case 'answer_delta':
      requireNonEmptyString(payload.delta);
      requireDisclosure(payload.disclosure);
      return;
    case 'citation_added':
      requireDisclosure(payload.disclosure);
      if (!isRecord(payload.citation)) throw new ChatProtocolError('Citation payload is malformed.');
      validateCitation(payload.citation);
      return;
    case 'clarification_required':
      requireNonEmptyString(payload.question);
      return;
    case 'abstained':
      requireOneOf(payload.reason, ['insufficient_evidence', 'scope_incomplete', 'clarification_required']);
      return;
    case 'denied':
      requireOneOf(payload.reason, ['authorization_denied']);
      return;
    case 'completed':
      requireOneOf(payload.outcome, ['answered', 'clarification_required']);
      return;
    case 'failed':
      requireOneOf(payload.reason, [
        'validation_failed',
        'stream_timeout',
        'stream_unavailable',
        'protocol_error',
        'internal_failure',
      ]);
      return;
    case 'cancelled':
      requireOneOf(payload.reason, ['request_cancelled']);
      return;
    case 'heartbeat':
      if (Object.keys(payload).length !== 0) throw new ChatProtocolError('Heartbeat payload is not empty.');
  }
}

function validateCitation(value: Record<string, unknown>): void {
  if (
    !nonEmptyString(value.citationId) ||
    !nonEmptyString(value.claimId) ||
    !nonEmptyString(value.sourceRevisionId) ||
    !isRecord(value.locator) ||
    !['page', 'sheet_row', 'message', 'characters'].includes(String(value.locator.kind)) ||
    !nonEmptyString(value.locator.value)
  ) {
    throw new ChatProtocolError('Citation payload is malformed.');
  }
}

function requireDisclosure(value: unknown): asserts value is DisclosureReceipt {
  if (!isRecord(value) || value.status !== 'authorized' || !nonEmptyString(value.receiptId)) {
    throw new ChatProtocolError('Protected chat content has no disclosure authorization.');
  }
}

function requireNonEmptyString(value: unknown): asserts value is string {
  if (!nonEmptyString(value)) throw new ChatProtocolError('A required chat field is malformed.');
}

function requireOneOf<T extends string>(value: unknown, allowed: readonly T[]): asserts value is T {
  if (typeof value !== 'string' || !allowed.includes(value as T)) {
    throw new ChatProtocolError('A chat outcome is malformed.');
  }
}

function rejectForbiddenFields(value: unknown): void {
  if (Array.isArray(value)) {
    value.forEach(rejectForbiddenFields);
    return;
  }
  if (!isRecord(value)) return;
  for (const [key, nested] of Object.entries(value)) {
    const normalizedKey = key.toLowerCase().replaceAll('-', '').replaceAll('_', '');
    if (forbiddenKeys.has(key) || forbiddenKeys.has(normalizedKey)) {
      throw new ChatProtocolError('The chat event contains undisclosable provider content.');
    }
    rejectForbiddenFields(nested);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}

function nullableString(value: unknown): value is string | null {
  return value === null || nonEmptyString(value);
}

function positiveInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}
