import { describe, expect, it, vi } from 'vitest';

import type { components } from '../../../api/generated/schema';
import { createScopedApiClient } from '../../api/client';
import { createUploadRows } from './upload';
import { loadBatchStatus, submitDocumentBatch } from './upload-api';

const workspaceId = '10000000-0000-4000-8000-000000000001';
const environmentId = '20000000-0000-4000-8000-000000000001';
const runId = '30000000-0000-4000-8000-000000000001';

describe('document batch upload API adapter', () => {
  it('test_batch_submission_sends_multipart_files_and_stable_idempotency_key', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(uploadResponse()));
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch,
      getBearerCredential: () => 'test-token',
      scope: { workspaceId, environmentId },
    });
    const rows = createUploadRows([
      new File(['notes'], 'notes.txt', { type: 'text/plain' }),
      new File(['# readme'], 'readme.md', { type: 'text/markdown' }),
    ]);

    await submitDocumentBatch(client, rows, 'batch-upload-stable-key', new AbortController().signal);

    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(request.method).toBe('POST');
    expect(request.headers.get('Idempotency-Key')).toBe('batch-upload-stable-key');
    expect(request.headers.get('X-Workspace-ID')).toBe(workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(environmentId);
    const body = await request.text();
    expect(body).toContain('name="file-1"');
    expect(body).toContain('name="file-2"');
  });

  it('test_batch_status_reads_the_published_run_endpoint', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(statusResponse()));
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch,
      getBearerCredential: () => 'test-token',
      scope: { workspaceId, environmentId },
    });

    await loadBatchStatus(client, runId, new AbortController().signal);

    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(request.url).toBe(`https://spine.test/api/v1/ingestion/runs/${runId}`);
    expect(request.method).toBe('GET');
  });
});

function uploadResponse(): components['schemas']['BatchIngestionUploadResponse'] {
  return {
    items: [],
    replay: false,
    run_id: runId,
    run_state: 'running',
    schema_version: 1,
    status: 'processing',
    status_url: `https://spine.test/api/v1/ingestion/runs/${runId}`,
    terminal: false,
  };
}

function statusResponse(): components['schemas']['BatchIngestionStatusResponse'] {
  const { status_url: _statusUrl, ...response } = uploadResponse();
  return response;
}
