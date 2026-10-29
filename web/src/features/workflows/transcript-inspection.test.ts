import { afterEach, describe, expect, it, vi } from 'vitest';

import { createScopedApiClient } from '../../api/client';
import type { components } from '../../../api/generated/schema';
import type { Scope } from '../../app/scope';
import { inspectTranscriptEvidence } from './transcript-inspection';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const locator: components['schemas']['TimeRangeLocator'] = {
  schema_version: 'time-range-locator:v1',
  source_object_id: '30000000-0000-4000-8000-000000000001',
  source_revision_id: '40000000-0000-4000-8000-000000000001',
  source_revision_digest: 'a'.repeat(64),
  transcript_artifact_id: '50000000-0000-4000-8000-000000000001',
  transcript_artifact_digest: 'b'.repeat(64),
  transcript_artifact_schema_version: 'transcription-artifact:v1',
  projection_version: 'transcript-projection:v1',
  start_ms: 1_000,
  end_ms: 2_500,
  segment_ids: ['60000000-0000-4000-8000-000000000001'],
} as const;

function currentResult(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 'transcript-inspection-result:v1',
    status: 'current',
    locator,
    source_object_id: locator.source_object_id,
    source_revision_id: locator.source_revision_id,
    transcript_artifact_id: locator.transcript_artifact_id,
    transcript_artifact_digest: locator.transcript_artifact_digest,
    transcript_artifact_schema_version: 'transcription-artifact:v1',
    publication_id: '70000000-0000-4000-8000-000000000001',
    publication_state: 'active',
    review_state: 'approved',
    partial: false,
    recording_duration_ms: 10_000,
    text: 'Синтетический фрагмент.',
    ...overrides,
  };
}

function clientFor(fetch: typeof globalThis.fetch) {
  return createScopedApiClient({
    baseUrl: 'https://spine.test',
    fetch,
    getBearerCredential: () => 'test-token',
    scope,
  });
}

function response(data: unknown, workspaceId = scope.workspaceId, environmentId = scope.environmentId) {
  return new Response(JSON.stringify({
    schema_version: 1,
    trace_id: '80000000-0000-4000-8000-000000000001',
    workspace_id: workspaceId,
    environment_id: environmentId,
    data,
  }), { status: 200, headers: { 'content-type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

describe('exact transcript evidence inspection', () => {
  it('posts_the_exact_locator_with_scope_and_checks_recording_bounds', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(currentResult()));
    const result = await inspectTranscriptEvidence(
      clientFor(fetcher), scope, locator, new AbortController().signal,
    );

    expect(result.status).toBe('current');
    expect(result.text).toBe('Синтетический фрагмент.');
    const request = fetcher.mock.calls[0]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe('/api/v1/transcript-evidence/inspect');
    expect(request.headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(scope.environmentId);
    expect(request.headers.get('Authorization')).toBe('Bearer test-token');
    expect(await request.json()).toEqual({ schema_version: 1, locator });
  });

  it('returns_a_content_free_permission_denied_state', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({
      schema_version: 'transcript-inspection-result:v1',
      status: 'permission_denied',
    }));

    const result = await inspectTranscriptEvidence(
      clientFor(fetcher), scope, locator, new AbortController().signal,
    );

    expect(result).toEqual({
      schema_version: 'transcript-inspection-result:v1', status: 'permission_denied',
    });
  });

  it('rejects_foreign_scope_fabricated_locator_and_out_of_bounds_result', async () => {
    const foreign = vi.fn<typeof fetch>().mockResolvedValue(response(currentResult(), '10000000-0000-4000-8000-000000000099'));
    await expect(inspectTranscriptEvidence(
      clientFor(foreign), scope, locator, new AbortController().signal,
    )).rejects.toThrow('unavailable');

    const wrongLocator = vi.fn<typeof fetch>().mockResolvedValue(response(currentResult({
      locator: { ...locator, transcript_artifact_digest: 'c'.repeat(64) },
    })));
    await expect(inspectTranscriptEvidence(
      clientFor(wrongLocator), scope, locator, new AbortController().signal,
    )).rejects.toThrow('unavailable');

    const outsideBounds = vi.fn<typeof fetch>().mockResolvedValue(response(currentResult({
      recording_duration_ms: 2_000,
    })));
    await expect(inspectTranscriptEvidence(
      clientFor(outsideBounds), scope, locator, new AbortController().signal,
    )).rejects.toThrow('unavailable');
  });
});
