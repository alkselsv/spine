import type { components } from '../../../api/generated/schema';
import { createScopedApiClient } from '../../api/client';

const REVISION_PAGE_LIMIT = 25;

export async function loadRevisionHistory(
  client: ReturnType<typeof createScopedApiClient>,
  sourceObjectId: string,
  cursor: string | undefined,
  signal: AbortSignal,
): Promise<components['schemas']['ApiCollectionEnvelope_RevisionSnapshot_']> {
  const result = await client.GET('/api/v1/documents/{source_object_id}/revisions', {
    params: {
      path: { source_object_id: sourceObjectId },
      query: cursor ? { limit: REVISION_PAGE_LIMIT, cursor } : { limit: REVISION_PAGE_LIMIT },
    },
    signal,
  });
  if (!result.data) throw new Error('The document revision history response is unavailable.');
  return result.data;
}
