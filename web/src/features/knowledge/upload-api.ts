import type { components } from '../../../api/generated/schema';

import type { ScopedApiClient } from '../../api/client';
import type { UploadRow } from './upload';

export type BatchUploadResponse = components['schemas']['BatchIngestionUploadResponse'];
export type BatchStatusResponse = components['schemas']['BatchIngestionStatusResponse'];

export async function submitDocumentBatch(
  client: ScopedApiClient,
  rows: readonly UploadRow[],
  idempotencyKey: string,
  signal: AbortSignal,
): Promise<BatchUploadResponse> {
  const body = new FormData();
  for (const row of rows) {
    body.append(
      row.inputIdentity,
      new Blob([row.file], { type: row.file.type || 'application/octet-stream' }),
      row.file.name,
    );
  }

  const result = await client.POST('/api/v1/documents/batches', {
    params: { header: { 'Idempotency-Key': idempotencyKey } },
    body: body as unknown as Record<string, string>,
    signal,
  });
  if (!result.data) throw new Error('The batch upload response is unavailable.');
  return result.data;
}

export async function loadBatchStatus(
  client: ScopedApiClient,
  runId: string,
  signal: AbortSignal,
): Promise<BatchStatusResponse> {
  const result = await client.GET('/api/v1/ingestion/runs/{run_id}', {
    params: { path: { run_id: runId } },
    signal,
  });
  if (!result.data) throw new Error('The batch status response is unavailable.');
  return result.data;
}
