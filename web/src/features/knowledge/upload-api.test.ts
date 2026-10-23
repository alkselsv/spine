// @vitest-environment node

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
    let submittedForm: FormData | undefined;
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async (input) => {
      submittedForm = await (input as Request).formData();
      return Response.json(uploadResponse());
    });
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
    expect(request.headers.get('Content-Type')).toMatch(
      /^multipart\/form-data; boundary=/,
    );
    expect([...submittedForm!.keys()]).toEqual(['file-1', 'file-2']);
    expect((submittedForm!.get('file-1') as File).name).toBe('notes.txt');
    expect((submittedForm!.get('file-2') as File).name).toBe('readme.md');
  });

  it('test_supported_docx_without_browser_media_type_uses_canonical_multipart_type', async () => {
    let submittedForm: FormData | undefined;
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async (input) => {
      submittedForm = await (input as Request).formData();
      return Response.json(uploadResponse());
    });
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch,
      getBearerCredential: () => 'test-token',
      scope: { workspaceId, environmentId },
    });
    const rows = createUploadRows([new File(['synthetic'], 'handbook.docx')]);

    await submitDocumentBatch(client, rows, 'batch-upload-docx', new AbortController().signal);

    const submitted = submittedForm!.get('file-1') as File;
    expect(submitted.name).toBe('handbook.docx');
    expect(submitted.type).toBe(
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    );
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
