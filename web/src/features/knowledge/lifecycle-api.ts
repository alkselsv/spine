import type { components } from '../../../api/generated/schema';

import type { ScopedApiClient } from '../../api/client';

export type DocumentLifecycleReceipt = components['schemas']['SourceLifecycleOperationResponse'];
export type ReplaceDocumentRequest = components['schemas']['ReplaceDocumentRequest'];
export type TombstoneDocumentRequest = components['schemas']['TombstoneDocumentRequest'];

export function createLifecycleIdempotencyKey(kind: 'replacement' | 'tombstone'): string {
  const crypto = globalThis.crypto;
  if (crypto?.randomUUID) return `document-${kind}-${crypto.randomUUID()}`;
  return `document-${kind}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export async function submitDocumentReplacement(
  client: ScopedApiClient,
  sourceObjectId: string,
  body: ReplaceDocumentRequest,
  idempotencyKey: string,
  signal: AbortSignal,
): Promise<DocumentLifecycleReceipt> {
  const result = await client.POST('/api/v1/documents/{source_object_id}/replacement', {
    params: { header: { 'Idempotency-Key': idempotencyKey }, path: { source_object_id: sourceObjectId } },
    body,
    signal,
  });
  if (!result.data) throw new Error('The document replacement receipt is unavailable.');
  return result.data;
}

export async function submitDocumentTombstone(
  client: ScopedApiClient,
  sourceObjectId: string,
  body: TombstoneDocumentRequest,
  idempotencyKey: string,
  signal: AbortSignal,
): Promise<DocumentLifecycleReceipt> {
  const result = await client.POST('/api/v1/documents/{source_object_id}/tombstone', {
    params: { header: { 'Idempotency-Key': idempotencyKey }, path: { source_object_id: sourceObjectId } },
    body,
    signal,
  });
  if (!result.data) throw new Error('The document tombstone receipt is unavailable.');
  return result.data;
}

export async function loadLifecycleOperation(
  client: ScopedApiClient,
  operationId: string,
  signal: AbortSignal,
): Promise<DocumentLifecycleReceipt> {
  const result = await client.GET('/api/v1/documents/lifecycle/operations/{operation_id}', {
    params: { path: { operation_id: operationId } },
    signal,
  });
  if (!result.data) throw new Error('The document lifecycle status is unavailable.');
  return result.data;
}
