import type { components } from '../../../api/generated/schema';

import type { ScopedApiClient } from '../../api/client';

export type RunHistoryCollection =
  components['schemas']['ApiCollectionEnvelope_IngestionRunHistoryResponse_'];
export type ProcessingHistoryCollection =
  components['schemas']['ApiCollectionEnvelope_DocumentProcessingHistoryResponse_'];

export interface HistoryQuery {
  cursor?: string;
}

const HISTORY_PAGE_LIMIT = 25;

export async function loadRunHistory(
  client: ScopedApiClient,
  query: HistoryQuery,
  signal: AbortSignal,
): Promise<RunHistoryCollection> {
  const result = await client.GET('/api/v1/ingestion/runs', {
    params: { query: toApiQuery(query) },
    signal,
  });
  if (!result.data) throw new Error('The ingestion run history response is unavailable.');
  return result.data;
}

export async function loadProcessingHistory(
  client: ScopedApiClient,
  query: HistoryQuery,
  signal: AbortSignal,
): Promise<ProcessingHistoryCollection> {
  const result = await client.GET('/api/v1/documents/processing-history', {
    params: { query: toApiQuery(query) },
    signal,
  });
  if (!result.data) throw new Error('The processing history response is unavailable.');
  return result.data;
}

function toApiQuery(query: HistoryQuery): { limit: number; cursor?: string } {
  return query.cursor ? { limit: HISTORY_PAGE_LIMIT, cursor: query.cursor } : { limit: HISTORY_PAGE_LIMIT };
}
