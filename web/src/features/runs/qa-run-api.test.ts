import { afterEach, describe, expect, it, vi } from 'vitest';

import { createScopedApiClient } from '../../api/client';
import {
  loadQARuns,
  parseQARunQuery,
  updateQARunQuery,
  type QARunCollection,
} from './qa-run-api';

const scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Q&A run query boundary', () => {
  it('test_parse_qa_run_query_accepts_bounded_filters_and_discards_unsafe_values', () => {
    expect(
      parseQARunQuery(
        '?workspace=protected&outcome=abstained&subject_id=30000000-0000-4000-8000-000000000001&created_after=2026-10-01T00:00:00Z&created_before=2026-10-17T00:00:00Z&cursor=next&prompt=secret',
      ),
    ).toEqual({
      outcome: 'abstained',
      subjectId: '30000000-0000-4000-8000-000000000001',
      createdAfter: '2026-10-01T00:00:00Z',
      createdBefore: '2026-10-17T00:00:00Z',
      cursor: 'next',
    });

    expect(
      parseQARunQuery(
        '?outcome=not-a-real-outcome&subject_id=not-a-uuid&created_after=not-a-date&cursor=' +
          'x'.repeat(769),
      ),
    ).toEqual({});

    expect(
      parseQARunQuery(
        '?created_after=2026-10-17T00:00:00Z&created_before=2026-10-01T00:00:00Z&outcome=failed',
      ),
    ).toEqual({ outcome: 'failed' });
  });

  it('test_update_qa_run_query_preserves_scope_and_replaces_filter_cursor_state', () => {
    expect(
      updateQARunQuery(
        '?workspace=workspace-a&environment=environment-a&outcome=answered&cursor=old&fixture=controlled',
        {
          outcome: 'failed',
          createdAfter: '2026-10-16T10:00:00Z',
        },
      ),
    ).toBe(
      'workspace=workspace-a&environment=environment-a&fixture=controlled&outcome=failed&created_after=2026-10-16T10%3A00%3A00Z',
    );
  });

  it('test_load_qa_runs_sends_generated_filters_page_limit_and_scope_headers', async () => {
    const response: QARunCollection = {
      environment_id: scope.environmentId,
      items: [],
      limit: 25,
      next_cursor: null,
      schema_version: 1,
      trace_id: '50000000-0000-4000-8000-000000000001',
      workspace_id: scope.workspaceId,
    };
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(response));
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch,
      getBearerCredential: () => 'test-token',
      scope,
    });

    await expect(
      loadQARuns(
        client,
        {
          cursor: 'cursor-next',
          outcome: 'answered',
          subjectId: '30000000-0000-4000-8000-000000000001',
          createdAfter: '2026-10-01T00:00:00Z',
          createdBefore: '2026-10-17T00:00:00Z',
        },
        new AbortController().signal,
      ),
    ).resolves.toEqual(response);

    const request = fetch.mock.calls[0]?.[0];
    expect(request).toBeInstanceOf(Request);
    const requestUrl = new URL((request as Request).url);
    expect(Object.fromEntries(requestUrl.searchParams)).toEqual({
      limit: '25',
      cursor: 'cursor-next',
      subject_id: '30000000-0000-4000-8000-000000000001',
      outcome: 'answered',
      created_after: '2026-10-01T00:00:00Z',
      created_before: '2026-10-17T00:00:00Z',
    });
    expect((request as Request).headers.get('Authorization')).toBe('Bearer test-token');
    expect((request as Request).headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect((request as Request).headers.get('X-Environment-ID')).toBe(scope.environmentId);
  });

  it('test_load_qa_runs_rejects_a_collection_from_another_scope', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json({
        environment_id: '20000000-0000-4000-8000-000000000002',
        items: [],
        limit: 25,
        next_cursor: null,
        schema_version: 1,
        trace_id: '50000000-0000-4000-8000-000000000001',
        workspace_id: '10000000-0000-4000-8000-000000000002',
      }),
    );
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch,
      getBearerCredential: () => 'test-token',
      scope,
    });

    await expect(
      loadQARuns(client, {}, new AbortController().signal, scope),
    ).rejects.toThrow('outside the selected scope');
  });

  it('test_load_qa_runs_keeps_a_second_workspace_isolated', async () => {
    const otherScope = {
      workspaceId: '10000000-0000-4000-8000-000000000002',
      environmentId: '20000000-0000-4000-8000-000000000002',
    };
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json({
        environment_id: otherScope.environmentId,
        items: [],
        limit: 25,
        next_cursor: null,
        schema_version: 1,
        trace_id: '50000000-0000-4000-8000-000000000001',
        workspace_id: otherScope.workspaceId,
      }),
    );
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch,
      getBearerCredential: () => 'test-token',
      scope: otherScope,
    });

    await loadQARuns(client, {}, new AbortController().signal, otherScope);

    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(request.headers.get('X-Workspace-ID')).toBe(otherScope.workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(otherScope.environmentId);
  });
});
