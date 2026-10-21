import { describe, expect, it, vi } from 'vitest';

import type { components } from '../../../api/generated/schema';
import { createScopedApiClient } from '../../api/client';
import { loadProcessingHistory, loadRunHistory } from './run-api';

const workspaceId = '10000000-0000-4000-8000-000000000001';
const environmentId = '20000000-0000-4000-8000-000000000001';
const runId = '30000000-0000-4000-8000-000000000001';

describe('ingestion run read API adapters', () => {
  it('test_run_history_sends_cursor_and_scope_to_the_generated_route', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json(runHistoryCollection()),
    );
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch,
      getBearerCredential: () => 'test-token',
      scope: { workspaceId, environmentId },
    });

    await loadRunHistory(client, { cursor: 'history-cursor' }, new AbortController().signal);

    const request = fetch.mock.calls[0]?.[0] as Request;
    const url = new URL(request.url);
    expect(url.pathname).toBe('/api/v1/ingestion/runs');
    expect(url.searchParams.get('limit')).toBe('25');
    expect(url.searchParams.get('cursor')).toBe('history-cursor');
    expect(request.headers.get('X-Workspace-ID')).toBe(workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(environmentId);
  });

  it('test_processing_history_reads_the_published_route_without_protected_fields', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json(processingHistoryCollection()),
    );
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch,
      getBearerCredential: () => 'test-token',
      scope: { workspaceId, environmentId },
    });

    const response = await loadProcessingHistory(client, {}, new AbortController().signal);

    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(request.url).toBe('https://spine.test/api/v1/documents/processing-history?limit=25');
    expect(request.method).toBe('GET');
    expect(request.headers.get('X-Workspace-ID')).toBe(workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(environmentId);
    expect(response.items[0]?.input_identity).toBe('file-1');
    expect(response.items[0]).not.toHaveProperty('content');
  });
});

function runHistoryCollection(): components['schemas']['ApiCollectionEnvelope_IngestionRunHistoryResponse_'] {
  return {
    environment_id: environmentId,
    items: [],
    limit: 25,
    next_cursor: null,
    schema_version: 1,
    trace_id: '50000000-0000-4000-8000-000000000001',
    workspace_id: workspaceId,
  };
}

function processingHistoryCollection(): components['schemas']['ApiCollectionEnvelope_DocumentProcessingHistoryResponse_'] {
  return {
    environment_id: environmentId,
    items: [
      {
        acceptance: 'accepted',
        attempt: 1,
        failure_ids: [],
        failures: [],
        input_identity: 'file-1',
        input_ordinal: 1,
        item_id: runId,
        item_state: 'processed',
        processing: 'succeeded',
        retryable: false,
        run_id: runId,
        run_state: 'completed',
        status: 'succeeded',
        status_url: `/api/v1/ingestion/runs/${runId}`,
        terminal: true,
        warning_ids: [],
        warnings: [],
      },
    ],
    limit: 25,
    next_cursor: null,
    schema_version: 1,
    trace_id: '50000000-0000-4000-8000-000000000001',
    workspace_id: workspaceId,
  };
}
