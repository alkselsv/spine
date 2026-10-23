import type { components } from '../../../api/generated/schema';
import type { ScopedApiClient } from '../../api/client';
import { resolveUploadMediaType } from './upload';

export type ReplacementUploadResponse = components['schemas']['IngestionUploadResponse'];
export type ReplacementUploadStatus = components['schemas']['IngestionStatusResponse'];

export async function submitDocumentRevisionUpload(
  client: ScopedApiClient,
  sourceObjectId: string,
  expectedRevisionId: string,
  file: File,
  idempotencyKey: string,
  signal: AbortSignal,
): Promise<ReplacementUploadResponse> {
  const result = await client.POST('/api/v1/documents/{source_object_id}/replacement-upload', {
    params: {
      header: {
        'Idempotency-Key': idempotencyKey,
        'If-Match': expectedRevisionId,
        'X-Filename': file.name,
        'Content-Type': resolveUploadMediaType(file),
      } as never,
      path: { source_object_id: sourceObjectId },
    },
    body: file as unknown as string,
    bodySerializer: () => file,
    signal,
  });
  if (!result.data) throw new Error('The replacement upload response is unavailable.');
  return result.data;
}

export async function loadReplacementUploadStatus(
  client: ScopedApiClient,
  runId: string,
  itemId: string,
  signal: AbortSignal,
): Promise<ReplacementUploadStatus> {
  const result = await client.GET('/api/v1/ingestion/runs/{run_id}/items/{item_id}', {
    params: { path: { run_id: runId, item_id: itemId } },
    signal,
  });
  if (!result.data) throw new Error('The replacement upload status is unavailable.');
  return result.data;
}
