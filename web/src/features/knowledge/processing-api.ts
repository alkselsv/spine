import type { components } from '../../../api/generated/schema';

import type { ScopedApiClient } from '../../api/client';

export type ProcessingCommand = components['schemas']['ProcessingCommandResponse'];
export type RetryProcessingRequest = components['schemas']['RetryProcessingRequest'];
export type ReprocessProcessingRequest = components['schemas']['ReprocessProcessingRequest'];

export const DEFAULT_PROCESSING_PROFILE = 'document-processing:r1';

export async function retryDocumentProcessing(
  client: ScopedApiClient,
  request: RetryProcessingRequest,
  idempotencyKey: string,
  signal: AbortSignal,
): Promise<ProcessingCommand> {
  const result = await client.POST('/api/v1/documents/processing/retry', {
    params: { header: { 'Idempotency-Key': idempotencyKey } },
    body: request,
    signal,
  });
  if (!result.data) throw new Error('The document processing retry response is unavailable.');
  return result.data;
}

export async function reprocessDocument(
  client: ScopedApiClient,
  request: ReprocessProcessingRequest,
  idempotencyKey: string,
  signal: AbortSignal,
): Promise<ProcessingCommand> {
  const result = await client.POST('/api/v1/documents/processing/reprocess', {
    params: { header: { 'Idempotency-Key': idempotencyKey } },
    body: request,
    signal,
  });
  if (!result.data) throw new Error('The document reprocess response is unavailable.');
  return result.data;
}

export async function loadProcessingCommand(
  client: ScopedApiClient,
  operationId: string,
  signal: AbortSignal,
): Promise<ProcessingCommand> {
  const result = await client.GET('/api/v1/documents/processing/operations/{operation_id}', {
    params: { path: { operation_id: operationId } },
    signal,
  });
  if (!result.data) throw new Error('The document processing status is unavailable.');
  return result.data;
}
