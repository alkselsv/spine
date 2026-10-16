import { describe, expect, it, vi } from 'vitest';

import type { components } from '../../../api/generated/schema';
import { createScopedApiClient } from '../../api/client';
import {
  DEFAULT_PROCESSING_PROFILE,
  loadProcessingCommand,
  reprocessDocument,
  retryDocumentProcessing,
} from './processing-api';

const workspaceId = '10000000-0000-4000-8000-000000000001';
const environmentId = '20000000-0000-4000-8000-000000000001';
const sourceObjectId = '30000000-0000-4000-8000-000000000001';
const sourceRevisionId = '40000000-0000-4000-8000-000000000001';
const operationId = '50000000-0000-4000-8000-000000000001';

describe('document processing command API', () => {
  it('test_retry_sends_exact_failure_and_revision_with_idempotency_and_scope', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(commandResponse()));
    const client = createClient(fetch);

    await retryDocumentProcessing(
      client,
      {
        expected_current_revision_id: sourceRevisionId,
        parent_failure_id: '60000000-0000-4000-8000-000000000001',
        parent_processing_receipt_id: null,
        source_object_id: sourceObjectId,
        source_revision_id: sourceRevisionId,
      },
      'retry-command-key',
      new AbortController().signal,
    );

    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(request.url).toBe('https://spine.test/api/v1/documents/processing/retry');
    expect(request.method).toBe('POST');
    expect(request.headers.get('Idempotency-Key')).toBe('retry-command-key');
    expect(request.headers.get('X-Workspace-ID')).toBe(workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(environmentId);
    expect(await request.json()).toEqual({
      expected_current_revision_id: sourceRevisionId,
      parent_failure_id: '60000000-0000-4000-8000-000000000001',
      parent_processing_receipt_id: null,
      source_object_id: sourceObjectId,
      source_revision_id: sourceRevisionId,
    });
  });

  it('test_reprocess_sends_exact_revision_and_approved_profile', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(commandResponse()));
    const client = createClient(fetch);

    await reprocessDocument(
      client,
      {
        expected_current_revision_id: sourceRevisionId,
        parent_processing_receipt_id: '70000000-0000-4000-8000-000000000001',
        processing_profile_reference: DEFAULT_PROCESSING_PROFILE,
        source_object_id: sourceObjectId,
        source_revision_id: sourceRevisionId,
      },
      'reprocess-command-key',
      new AbortController().signal,
    );

    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(request.url).toBe('https://spine.test/api/v1/documents/processing/reprocess');
    expect(request.headers.get('Idempotency-Key')).toBe('reprocess-command-key');
    expect(await request.json()).toMatchObject({
      expected_current_revision_id: sourceRevisionId,
      processing_profile_reference: DEFAULT_PROCESSING_PROFILE,
      source_object_id: sourceObjectId,
      source_revision_id: sourceRevisionId,
    });
  });

  it('test_status_polling_uses_operation_identity_from_the_accepted_receipt', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(commandResponse()));
    const client = createClient(fetch);

    await loadProcessingCommand(client, operationId, new AbortController().signal);

    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(request.url).toBe(`https://spine.test/api/v1/documents/processing/operations/${operationId}`);
    expect(request.method).toBe('GET');
  });
});

function createClient(fetch: typeof globalThis.fetch) {
  return createScopedApiClient({
    baseUrl: 'https://spine.test',
    fetch,
    getBearerCredential: () => 'test-token',
    scope: { workspaceId, environmentId },
  });
}

function commandResponse(): components['schemas']['ProcessingCommandResponse'] {
  return {
    failure_ids: [],
    item_id: '80000000-0000-4000-8000-000000000001',
    next_action: 'poll',
    operation_id: operationId,
    processing_receipt_id: null,
    replay: false,
    run_id: '90000000-0000-4000-8000-000000000001',
    schema_version: 1,
    source_object_id: sourceObjectId,
    source_revision_id: sourceRevisionId,
    status: 'accepted',
    status_url: `https://spine.test/api/v1/documents/processing/${operationId}`,
    trigger_kind: 'retry',
  };
}
