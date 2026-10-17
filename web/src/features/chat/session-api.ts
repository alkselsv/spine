import type { components } from '../../../api/generated/schema';
import type { ScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';

const PAGE_LIMIT = 25;

export type QASession = components['schemas']['QASessionResponse'];
export type QAMessage = components['schemas']['QAMessageResponse'];
export type QASessionPage = components['schemas']['ApiCollectionEnvelope_QASessionResponse_'];
export type QAMessagePage = components['schemas']['ApiCollectionEnvelope_QAMessageResponse_'];

export const qaSessionQueryKeys = {
  scope: (scope: Scope) =>
    ['spine', 'qa-chat', scope.workspaceId, scope.environmentId] as const,
  identity: (scope: Scope, identityVersion: string) =>
    [...qaSessionQueryKeys.scope(scope), 'identity', identityVersion] as const,
  sessions: (scope: Scope, cursor: string | null, identityVersion: string) =>
    [...qaSessionQueryKeys.identity(scope, identityVersion), 'sessions', cursor] as const,
  session: (scope: Scope, sessionId: string, identityVersion: string) =>
    [...qaSessionQueryKeys.identity(scope, identityVersion), 'session', sessionId] as const,
  messages: (scope: Scope, sessionId: string, cursor: string | null, identityVersion: string) =>
    [...qaSessionQueryKeys.identity(scope, identityVersion), 'messages', sessionId, cursor] as const,
  message: (scope: Scope, sessionId: string, messageId: string, identityVersion: string) =>
    [...qaSessionQueryKeys.identity(scope, identityVersion), 'message', sessionId, messageId] as const,
};

export async function loadQASessionPage(
  client: ScopedApiClient,
  scope: Scope,
  cursor: string | null,
  signal: AbortSignal,
): Promise<QASessionPage> {
  const response = await client.GET('/api/v1/qa/sessions', {
    params: { query: { limit: PAGE_LIMIT, ...(cursor ? { cursor } : {}) } },
    signal,
  });
  if (!response.data) throw new Error('The Q&A session page is unavailable.');
  assertEnvelopeScope(response.data, scope, 'The Q&A session page is unavailable.');
  if (response.data.items.some((item) => !isInScope(item, scope))) {
    throw new Error('The Q&A session page is unavailable.');
  }
  return response.data;
}

export async function loadQASession(
  client: ScopedApiClient,
  scope: Scope,
  sessionId: string,
  signal: AbortSignal,
): Promise<QASession> {
  const response = await client.GET('/api/v1/qa/sessions/{session_id}', {
    params: { path: { session_id: sessionId } },
    signal,
  });
  if (!response.data) throw new Error('The Q&A session is unavailable.');
  const session = response.data.data;
  if (session.session_id !== sessionId || !isInScope(session, scope)) {
    throw new Error('The Q&A session is unavailable.');
  }
  return session;
}

export async function loadQAMessagePage(
  client: ScopedApiClient,
  scope: Scope,
  sessionId: string,
  cursor: string | null,
  signal: AbortSignal,
): Promise<QAMessagePage> {
  const response = await client.GET('/api/v1/qa/sessions/{session_id}/messages', {
    params: {
      path: { session_id: sessionId },
      query: { limit: PAGE_LIMIT, ...(cursor ? { cursor } : {}) },
    },
    signal,
  });
  if (!response.data) throw new Error('The Q&A message page is unavailable.');
  assertEnvelopeScope(response.data, scope, 'The Q&A message page is unavailable.');
  if (
    response.data.items.some(
      (item) => item.session_id !== sessionId || !isInScope(item, scope),
    )
  ) {
    throw new Error('The Q&A message page is unavailable.');
  }
  return response.data;
}

export async function loadQAMessage(
  client: ScopedApiClient,
  scope: Scope,
  sessionId: string,
  messageId: string,
  signal: AbortSignal,
): Promise<QAMessage> {
  const response = await client.GET('/api/v1/qa/sessions/{session_id}/messages/{message_id}', {
    params: { path: { session_id: sessionId, message_id: messageId } },
    signal,
  });
  if (
    !response.data ||
    response.data.schema_version !== 1 ||
    response.data.workspace_id !== scope.workspaceId ||
    response.data.environment_id !== scope.environmentId
  ) {
    throw new Error('The Q&A message is unavailable.');
  }
  const message = response.data.data;
  if (
    message.message_id !== messageId ||
    message.session_id !== sessionId ||
    !isInScope(message, scope)
  ) {
    throw new Error('The Q&A message is unavailable.');
  }
  return message;
}

export async function createQASession(
  client: ScopedApiClient,
  scope: Scope,
  idempotencyKey: string,
): Promise<QASession> {
  const response = await client.POST('/api/v1/qa/sessions', {
    body: {},
    params: { header: { 'Idempotency-Key': idempotencyKey } },
  });
  if (!response.data?.result) throw new Error('The Q&A session could not be created.');
  if (!isInScope(response.data.result, scope)) {
    throw new Error('The Q&A session could not be created.');
  }
  return response.data.result;
}

export async function restoreQASession(
  client: ScopedApiClient,
  scope: Scope,
  sessionId: string,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<QASession> {
  const response = await client.POST('/api/v1/qa/sessions/{session_id}/restore', {
    params: {
      path: { session_id: sessionId },
      header: { 'Idempotency-Key': idempotencyKey },
    },
    body: { expected_version: expectedVersion },
  });
  if (!response.data?.result) throw new Error('The Q&A session could not be restored.');
  if (response.data.result.session_id !== sessionId || !isInScope(response.data.result, scope)) {
    throw new Error('The Q&A session could not be restored.');
  }
  return response.data.result;
}

function assertEnvelopeScope(
  envelope: { workspace_id: string; environment_id: string },
  scope: Scope,
  safeMessage: string,
): void {
  if (envelope.workspace_id !== scope.workspaceId || envelope.environment_id !== scope.environmentId) {
    throw new Error(safeMessage);
  }
}

function isInScope(
  resource: { workspace_id: string; environment_id: string },
  scope: Scope,
): boolean {
  return resource.workspace_id === scope.workspaceId && resource.environment_id === scope.environmentId;
}
