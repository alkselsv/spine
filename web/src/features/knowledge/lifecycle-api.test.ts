import { describe, expect, it, vi } from 'vitest';

import { createScopedApiClient } from '../../api/client';
import {
  loadLifecycleOperation,
  submitDocumentReplacement,
  submitDocumentTombstone,
} from './lifecycle-api';

const workspaceId = '10000000-0000-4000-8000-000000000001';
const environmentId = '20000000-0000-4000-8000-000000000001';
const sourceObjectId = '30000000-0000-4000-8000-000000000001';
const revisionId = '40000000-0000-4000-8000-000000000001';
const operationId = '50000000-0000-4000-8000-000000000001';

const response = {
  accepted_at: '2026-10-16T10:00:00Z',
  accepted_revision_id: revisionId,
  canonical_acceptance_id: operationId,
  completed_at: null,
  expected_current_revision_id: revisionId,
  idempotency_receipt_id: '60000000-0000-4000-8000-000000000001',
  operation_id: operationId,
  operation_kind: 'replacement',
  processing_run_id: null,
  replay: false,
  retryable: false,
  safe_failure_code: null,
  schema_version: 1,
  source_object_id: sourceObjectId,
  status: 'processing_dispatched',
  status_url: `https://spine.test/api/v1/documents/lifecycle/operations/${operationId}`,
};

function client(fetch: typeof globalThis.fetch) {
  return createScopedApiClient({
    baseUrl: 'https://spine.test',
    fetch,
    getBearerCredential: () => 'test-token',
    scope: { workspaceId, environmentId },
  });
}

describe('document lifecycle API adapter', () => {
  it('test_replacement_sends_server_bounded_intent_and_stable_scope', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(response, { status: 202 }));

    await submitDocumentReplacement(
      client(fetch),
      sourceObjectId,
      {
        expected_current_revision_id: revisionId,
        upload_intent: {
          declared_filename: 'handbook.md',
          declared_media_type: 'text/markdown',
          storage_request_reference: 'staged-upload:request-001',
          upload_identity: 'handbook',
        },
      },
      'document-replacement-stable-key',
      new AbortController().signal,
    );

    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(request.method).toBe('POST');
    expect(new URL(request.url).pathname).toBe(`/api/v1/documents/${sourceObjectId}/replacement`);
    expect(request.headers.get('Idempotency-Key')).toBe('document-replacement-stable-key');
    expect(request.headers.get('X-Workspace-ID')).toBe(workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(environmentId);
    await expect(request.json()).resolves.toEqual({
      expected_current_revision_id: revisionId,
      upload_intent: {
        declared_filename: 'handbook.md',
        declared_media_type: 'text/markdown',
        storage_request_reference: 'staged-upload:request-001',
        upload_identity: 'handbook',
      },
    });
  });

  it('test_tombstone_sends_expected_revision_and_reason_without_physical_delete', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json({ ...response, operation_kind: 'tombstone', status: 'completed' }, { status: 202 }),
    );

    await submitDocumentTombstone(
      client(fetch),
      sourceObjectId,
      { expected_current_revision_id: revisionId, deletion_reason: 'source_deleted' },
      'document-tombstone-stable-key',
      new AbortController().signal,
    );

    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe(`/api/v1/documents/${sourceObjectId}/tombstone`);
    expect(request.method).toBe('POST');
    expect(request.headers.get('Idempotency-Key')).toBe('document-tombstone-stable-key');
    await expect(request.json()).resolves.toEqual({
      expected_current_revision_id: revisionId,
      deletion_reason: 'source_deleted',
    });
  });

  it('test_lifecycle_status_reads_server_receipt_by_operation_id', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(response));

    const result = await loadLifecycleOperation(client(fetch), operationId, new AbortController().signal);

    expect(result.operation_id).toBe(operationId);
    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(request.method).toBe('GET');
    expect(new URL(request.url).pathname).toBe(`/api/v1/documents/lifecycle/operations/${operationId}`);
    expect(request.headers.get('X-Workspace-ID')).toBe(workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(environmentId);
  });

  it('test_same_command_is_scoped_to_each_workspace_without_reusing_scope_headers', async () => {
    const workspaceB = '10000000-0000-4000-8000-000000000002';
    const environmentB = '20000000-0000-4000-8000-000000000002';
    const fetchA = vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(response, { status: 202 }));
    const fetchB = vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(response, { status: 202 }));

    await submitDocumentTombstone(
      client(fetchA),
      sourceObjectId,
      { expected_current_revision_id: revisionId, deletion_reason: 'source_deleted' },
      'same-stable-key',
      new AbortController().signal,
    );
    await submitDocumentTombstone(
      createScopedApiClient({
        baseUrl: 'https://spine.test',
        fetch: fetchB,
        getBearerCredential: () => 'test-token',
        scope: { workspaceId: workspaceB, environmentId: environmentB },
      }),
      sourceObjectId,
      { expected_current_revision_id: revisionId, deletion_reason: 'source_deleted' },
      'same-stable-key',
      new AbortController().signal,
    );

    expect((fetchA.mock.calls[0]?.[0] as Request).headers.get('X-Workspace-ID')).toBe(workspaceId);
    expect((fetchA.mock.calls[0]?.[0] as Request).headers.get('X-Environment-ID')).toBe(environmentId);
    expect((fetchB.mock.calls[0]?.[0] as Request).headers.get('X-Workspace-ID')).toBe(workspaceB);
    expect((fetchB.mock.calls[0]?.[0] as Request).headers.get('X-Environment-ID')).toBe(environmentB);
  });
});
