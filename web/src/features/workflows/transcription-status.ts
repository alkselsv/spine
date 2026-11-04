import type { components } from '../../../api/generated/schema';

import type { ApiScope, ScopedApiClient } from '../../api/client';

export type TranscriptionStatus = components['schemas']['TranscriptionStatusSnapshot'];

const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function isValidTranscriptionJobId(value: string): boolean {
  return uuidPattern.test(value);
}

export async function loadTranscriptionStatus(
  client: ScopedApiClient,
  jobId: string,
  signal: AbortSignal,
  expectedScope: ApiScope,
): Promise<TranscriptionStatus> {
  if (!isValidTranscriptionJobId(jobId)) throw new Error('Invalid transcription job identity.');
  const { data, error } = await client.GET('/api/v1/transcriptions/{job_id}', {
    params: { path: { job_id: jobId } },
    signal,
  });
  if (error || !data) throw new Error('Transcription status is unavailable.');
  if (
    data.workspace_id !== expectedScope.workspaceId ||
    data.environment_id !== expectedScope.environmentId
  ) {
    throw new Error('Transcription status is outside the selected scope.');
  }
  if (data.data.job_id && data.data.job_id !== jobId) {
    throw new Error('Transcription status does not match the selected job.');
  }
  return data.data;
}
