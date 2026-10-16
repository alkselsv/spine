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

export type MessageStatus =
  | 'pending'
  | 'completed'
  | 'clarification_required'
  | 'abstained'
  | 'denied'
  | 'failed'
  | 'cancelled';

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
  answerId?: string;
  sourceRevisionId: string;
  locator: {
    kind:
      | 'page'
      | 'sheet_row'
      | 'message'
      | 'characters'
      | 'character_range'
      | 'record_field'
      | 'custom';
    value: string;
    schemaVersion?: 'qa-event-citation-locator:v1';
  };
  schemaVersion?: 'qa-event-citation:v1';
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
  acceptedOutputId?: string;
  schemaVersion?: 'qa-disclosure-receipt:v1';
  validationReference?: string;
}

interface ChatEventBase {
  protocolVersion: typeof CHAT_PROTOCOL_VERSION;
  schemaVersion?: 'qa-event:v1';
  eventId: string;
  sequence: number;
  sessionId: string;
  workspaceId: string;
  environmentId: string;
  messageId: string | null;
  runId: string | null;
  traceId: string;
  correlationId?: string | null;
  causationId?: string | null;
}

export type ChatEventPayload = {
  run_started: { stage: string };
  stage_changed: { stage: string };
  answer_delta: { delta: string; disclosure: DisclosureReceipt };
  citation_added: { citation: Citation; disclosure: DisclosureReceipt };
  clarification_required: { question: string };
  abstained: {
    reason:
      | 'no_usable_evidence'
      | 'insufficient_evidence'
      | 'scope_incomplete'
      | 'clarification_required'
      | 'insufficient_support'
      | 'unresolved_conflict'
      | 'incomplete_authorized_coverage'
      | 'unmet_freshness'
      | 'evidence_invalidated'
      | 'rejected_candidate';
  };
  denied: { reason: 'authorization_denied' };
  completed: { outcome: 'answered' | 'clarification_required' };
  failed: {
    reason:
      | 'validation_failed'
      | 'stream_timeout'
      | 'stream_unavailable'
      | 'protocol_error'
      | 'internal_failure'
      | 'mandatory_processing_failed';
  };
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

export interface DecodedChatEventStream {
  events: ChatEvent[];
  retryMs: number | null;
}

const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const zeroUuid = '00000000-0000-0000-0000-000000000000';

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
  const normalized = isWireEvent(value) ? normalizeWireEvent(value) : value;
  if (!isRecord(normalized) || normalized.protocolVersion !== CHAT_PROTOCOL_VERSION) {
    throw new ChatProtocolError('The chat protocol version is not supported.');
  }
  if (
    !nonEmptyString(normalized.eventId) ||
    !positiveInteger(normalized.sequence) ||
    !nonEmptyString(normalized.sessionId) ||
    !nonEmptyString(normalized.workspaceId) ||
    !nonEmptyString(normalized.environmentId) ||
    !nullableString(normalized.messageId) ||
    !nullableString(normalized.runId) ||
    !nonEmptyString(normalized.traceId) ||
    typeof normalized.type !== 'string' ||
    !eventTypes.has(normalized.type as ChatEventType) ||
    !isRecord(normalized.payload)
  ) {
    throw new ChatProtocolError('The chat event envelope is malformed.');
  }

  validatePayload(normalized.type as ChatEventType, normalized.payload);
  return normalized as unknown as ChatEvent;
}

export function decodeChatEventStream(value: string): DecodedChatEventStream {
  if (typeof value !== 'string') {
    throw new ChatProtocolError('The chat event stream is malformed.');
  }
  const events: ChatEvent[] = [];
  let retryMs: number | null = null;
  let frameId: string | null = null;
  let frameType: string | null = null;
  let data: string[] = [];

  const dispatch = () => {
    if (data.length === 0) {
      frameId = null;
      frameType = null;
      return;
    }
    if (!nonEmptyString(frameId) || !nonEmptyString(frameType)) {
      throw new ChatProtocolError('The chat SSE frame is missing its identity.');
    }
    let wire: unknown;
    try {
      wire = JSON.parse(data.join('\n'));
    } catch {
      throw new ChatProtocolError('The chat SSE frame data is malformed.');
    }
    const event = decodeChatEvent(wire);
    if (event.eventId !== frameId || event.type !== frameType) {
      throw new ChatProtocolError('The chat SSE frame does not match its event envelope.');
    }
    events.push(event);
    frameId = null;
    frameType = null;
    data = [];
  };

  for (const line of value.replaceAll('\r\n', '\n').replaceAll('\r', '\n').split('\n')) {
    if (line === '') {
      dispatch();
      continue;
    }
    if (line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator === -1 ? line : line.slice(0, separator);
    const rawFieldValue = separator === -1 ? '' : line.slice(separator + 1);
    const fieldValue = rawFieldValue.startsWith(' ') ? rawFieldValue.slice(1) : rawFieldValue;
    switch (field) {
      case 'id':
        if (fieldValue.includes('\0')) throw new ChatProtocolError('The chat SSE event ID is malformed.');
        frameId = fieldValue;
        break;
      case 'event':
        frameType = fieldValue;
        break;
      case 'data':
        data.push(fieldValue);
        break;
      case 'retry':
        if (!/^\d+$/.test(fieldValue)) {
          throw new ChatProtocolError('The chat SSE retry value is malformed.');
        }
        retryMs = Number(fieldValue);
        if (!Number.isSafeInteger(retryMs)) {
          throw new ChatProtocolError('The chat SSE retry value is malformed.');
        }
        break;
    }
  }
  dispatch();
  return { events, retryMs };
}

export function isTerminalChatEvent(event: ChatEvent): event is ChatEvent<ChatTerminalEventType> {
  return ['abstained', 'denied', 'completed', 'failed', 'cancelled'].includes(event.type);
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
      requireOneOf(payload.reason, [
        'no_usable_evidence',
        'insufficient_evidence',
        'scope_incomplete',
        'clarification_required',
        'insufficient_support',
        'unresolved_conflict',
        'incomplete_authorized_coverage',
        'unmet_freshness',
        'evidence_invalidated',
        'rejected_candidate',
      ]);
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
        'mandatory_processing_failed',
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
    ![
      'page',
      'sheet_row',
      'message',
      'characters',
      'character_range',
      'record_field',
      'custom',
    ].includes(String(value.locator.kind)) ||
    !nonEmptyString(value.locator.value)
  ) {
    throw new ChatProtocolError('Citation payload is malformed.');
  }
}

function isWireEvent(value: unknown): value is Record<string, unknown> {
  return isRecord(value) && (
    Object.hasOwn(value, 'schema_version') || Object.hasOwn(value, 'protocol_version')
  );
}

function normalizeWireEvent(value: Record<string, unknown>): Record<string, unknown> {
  requireExactKeys(value, [
    'schema_version',
    'protocol_version',
    'event_id',
    'sequence',
    'type',
    'session_id',
    'workspace_id',
    'environment_id',
    'message_id',
    'run_id',
    'trace_id',
    'correlation_id',
    'causation_id',
    'payload',
  ]);
  if (value.schema_version !== 'qa-event:v1' || value.protocol_version !== CHAT_PROTOCOL_VERSION) {
    throw new ChatProtocolError('The chat protocol version is not supported.');
  }
  if (
    !wireUuid(value.event_id) ||
    !positiveInteger(value.sequence) ||
    !wireUuid(value.session_id) ||
    !wireUuid(value.workspace_id) ||
    !wireUuid(value.environment_id) ||
    !nullableWireUuid(value.message_id) ||
    !nullableWireUuid(value.run_id) ||
    !wireUuid(value.trace_id) ||
    !nullableWireUuid(value.correlation_id) ||
    !nullableWireUuid(value.causation_id) ||
    typeof value.type !== 'string' ||
    !eventTypes.has(value.type as ChatEventType) ||
    !isRecord(value.payload)
  ) {
    throw new ChatProtocolError('The chat event envelope is malformed.');
  }
  return {
    protocolVersion: value.protocol_version,
    schemaVersion: value.schema_version,
    eventId: value.event_id,
    sequence: value.sequence,
    sessionId: value.session_id,
    workspaceId: value.workspace_id,
    environmentId: value.environment_id,
    messageId: value.message_id,
    runId: value.run_id,
    traceId: value.trace_id,
    correlationId: value.correlation_id,
    causationId: value.causation_id,
    type: value.type,
    payload: normalizeWirePayload(value.type as ChatEventType, value.payload),
  };
}

function normalizeWirePayload(type: ChatEventType, value: unknown): unknown {
  if (!isRecord(value)) return value;
  const expectedKeys: Record<ChatEventType, readonly string[]> = {
    run_started: ['stage'],
    stage_changed: ['stage'],
    answer_delta: ['delta', 'disclosure'],
    citation_added: ['citation', 'disclosure'],
    clarification_required: ['prompt'],
    abstained: ['reason'],
    denied: ['reason'],
    completed: ['outcome'],
    failed: ['reason'],
    cancelled: ['reason'],
    heartbeat: [],
  };
  requireExactKeys(value, expectedKeys[type]);
  if (type === 'answer_delta') {
    return { delta: value.delta, disclosure: normalizeWireDisclosure(value.disclosure) };
  }
  if (type === 'citation_added') {
    const citation = normalizeWireCitation(value.citation);
    const disclosure = normalizeWireDisclosure(value.disclosure);
    if (
      !isRecord(citation) ||
      !isRecord(disclosure) ||
      citation.answerId !== disclosure.acceptedOutputId
    ) {
      throw new ChatProtocolError('Citation disclosure does not match the accepted answer.');
    }
    return {
      citation,
      disclosure,
    };
  }
  if (type === 'clarification_required') return { question: value.prompt };
  return { ...value };
}

function normalizeWireDisclosure(value: unknown): unknown {
  if (!isRecord(value)) return value;
  requireExactKeys(value, [
    'schema_version',
    'receipt_id',
    'status',
    'accepted_output_id',
    'validation_reference',
  ]);
  if (
    value.schema_version !== 'qa-disclosure-receipt:v1' ||
    value.status !== 'authorized' ||
    !wireUuid(value.receipt_id) ||
    !wireUuid(value.accepted_output_id) ||
    !wireUuid(value.validation_reference)
  ) {
    throw new ChatProtocolError('Protected chat content has no disclosure authorization.');
  }
  return {
    acceptedOutputId: value.accepted_output_id,
    receiptId: value.receipt_id,
    schemaVersion: value.schema_version,
    status: value.status,
    validationReference: value.validation_reference,
  };
}

function normalizeWireCitation(value: unknown): unknown {
  if (!isRecord(value)) return value;
  requireExactKeys(value, [
    'schema_version',
    'citation_id',
    'claim_id',
    'answer_id',
    'source_revision_id',
    'locator',
  ]);
  if (value.schema_version !== 'qa-event-citation:v1') {
    throw new ChatProtocolError('Citation payload is malformed.');
  }
  const locator = value.locator;
  if (!isRecord(locator)) throw new ChatProtocolError('Citation payload is malformed.');
  requireExactKeys(locator, ['schema_version', 'kind', 'value']);
  if (locator.schema_version !== 'qa-event-citation-locator:v1') {
    throw new ChatProtocolError('Citation payload is malformed.');
  }
  if (
    !wireUuid(value.citation_id) ||
    !wireUuid(value.claim_id) ||
    !wireUuid(value.answer_id) ||
    !wireUuid(value.source_revision_id) ||
    ![
      'page',
      'sheet_row',
      'message',
      'characters',
      'character_range',
      'record_field',
      'custom',
    ].includes(String(locator.kind)) ||
    !nonEmptyString(locator.value)
  ) {
    throw new ChatProtocolError('Citation payload is malformed.');
  }
  return {
    answerId: value.answer_id,
    citationId: value.citation_id,
    claimId: value.claim_id,
    locator: {
      kind: locator.kind,
      schemaVersion: locator.schema_version,
      value: locator.value,
    },
    schemaVersion: value.schema_version,
    sourceRevisionId: value.source_revision_id,
  };
}

function requireExactKeys(value: Record<string, unknown>, expected: readonly string[]): void {
  const actual = Object.keys(value).sort();
  const allowed = [...expected].sort();
  if (actual.length !== allowed.length || actual.some((key, index) => key !== allowed[index])) {
    throw new ChatProtocolError('The chat event contains an incompatible schema.');
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

function wireUuid(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.toLowerCase() !== zeroUuid &&
    uuidPattern.test(value)
  );
}

function nullableWireUuid(value: unknown): value is string | null {
  return value === null || wireUuid(value);
}

function positiveInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}
