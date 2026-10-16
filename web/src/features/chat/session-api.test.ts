import { afterEach, describe, expect, it, vi } from 'vitest';

import { createScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import {
  createQASession,
  loadQAMessagePage,
  loadQASession,
  loadQASessionPage,
  qaSessionQueryKeys,
  restoreQASession,
} from './session-api';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};

const session = {
  schema_version: 1 as const,
  session_id: '30000000-0000-4000-8000-000000000001',
  workspace_id: scope.workspaceId,
  environment_id: scope.environmentId,
  subject_id: '40000000-0000-4000-8000-000000000001',
  version: 2,
  status: 'active' as const,
  created_at: '2026-10-17T10:00:00Z',
  updated_at: '2026-10-17T10:00:00Z',
};

function clientFor(fetch: typeof globalThis.fetch) {
  return createScopedApiClient({
    baseUrl: 'https://spine.test',
    fetch,
    getBearerCredential: () => 'test-token',
    scope,
  });
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    headers: { 'content-type': 'application/json' },
    status: 200,
  });
}

afterEach(() => vi.unstubAllGlobals());

describe('Q&A session resource boundary', () => {
  it('test_session_page_uses_canonical_scoped_identity_and_cursor', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        workspace_id: scope.workspaceId,
        environment_id: scope.environmentId,
        items: [session],
        next_cursor: 'next cursor',
        limit: 25,
      }),
    );
    const client = clientFor(fetcher);

    const page = await loadQASessionPage(client, scope, 'next cursor', new AbortController().signal);

    expect(page.items[0]?.session_id).toBe(session.session_id);
    expect(page.next_cursor).toBe('next cursor');
    const request = fetcher.mock.calls[0]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe('/api/v1/qa/sessions');
    expect(new URL(request.url).searchParams.get('cursor')).toBe('next cursor');
    expect(request.headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(scope.environmentId);
    expect(request.headers.get('Authorization')).toBe('Bearer test-token');
  });

  it('test_session_create_sends_idempotency_key_and_returns_canonical_session', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({ result: session, replay: false, status: 'completed' }),
    );
    const client = clientFor(fetcher);

    const created = await createQASession(client, scope, 'create-411-1');

    expect(created.session_id).toBe(session.session_id);
    const request = fetcher.mock.calls[0]?.[0] as Request;
    expect(request.method).toBe('POST');
    expect(request.headers.get('Idempotency-Key')).toBe('create-411-1');
    expect(await request.json()).toEqual({});
  });

  it('test_archived_session_restore_uses_expected_version_and_idempotency', async () => {
    const restored = { ...session, status: 'active' as const, version: 3 };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({ result: restored, replay: false, status: 'completed' }),
    );
    const client = clientFor(fetcher);

    const result = await restoreQASession(client, scope, session.session_id, 2, 'restore-411-1');

    expect(result.version).toBe(3);
    const request = fetcher.mock.calls[0]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe(
      `/api/v1/qa/sessions/${session.session_id}/restore`,
    );
    expect(request.headers.get('Idempotency-Key')).toBe('restore-411-1');
    expect(await request.json()).toEqual({ expected_version: 2 });
  });

  it('test_message_page_is_bound_to_session_and_uses_canonical_cursor', async () => {
    const message = {
      schema_version: 1 as const,
      message_id: '50000000-0000-4000-8000-000000000001',
      session_id: session.session_id,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      version: 1,
      ordinal: 1,
      trace_id: '60000000-0000-4000-8000-000000000001',
      run_id: null,
      state: 'terminal' as const,
      terminal_outcome: 'answered' as const,
      cancelled: false,
      disclosure_eligibility: 'eligible' as const,
      question: 'Synthetic question',
      answer: 'Synthetic answer',
      created_at: '2026-10-17T10:01:00Z',
      updated_at: '2026-10-17T10:01:00Z',
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        workspace_id: scope.workspaceId,
        environment_id: scope.environmentId,
        items: [message],
        next_cursor: null,
        limit: 25,
      }),
    );
    const client = clientFor(fetcher);

    const page = await loadQAMessagePage(
      client,
      scope,
      session.session_id,
      'message cursor',
      new AbortController().signal,
    );

    expect(page.items).toEqual([message]);
    const request = fetcher.mock.calls[0]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe(
      `/api/v1/qa/sessions/${session.session_id}/messages`,
    );
    expect(new URL(request.url).searchParams.get('limit')).toBe('25');
    expect(new URL(request.url).searchParams.get('cursor')).toBe('message cursor');
  });

  it('test_query_key_changes_when_workspace_or_environment_changes', () => {
    const otherWorkspace: Scope = { ...scope, workspaceId: '10000000-0000-4000-8000-000000000002' };
    const otherEnvironment: Scope = { ...scope, environmentId: '20000000-0000-4000-8000-000000000002' };

    expect(qaSessionQueryKeys.sessions(scope, null, 'principal-a')).not.toEqual(
      qaSessionQueryKeys.sessions(otherWorkspace, null, 'principal-a'),
    );
    expect(qaSessionQueryKeys.messages(scope, session.session_id, null, 'principal-a')).not.toEqual(
      qaSessionQueryKeys.messages(otherEnvironment, session.session_id, null, 'principal-a'),
    );
    expect(qaSessionQueryKeys.scope(scope)).toEqual([
      'spine',
      'qa-chat',
      scope.workspaceId,
      scope.environmentId,
    ]);
  });

  it('test_query_key_changes_when_authenticated_identity_changes', () => {
    expect(qaSessionQueryKeys.messages(scope, session.session_id, null, 'principal-a')).not.toEqual(
      qaSessionQueryKeys.messages(scope, session.session_id, null, 'principal-b'),
    );
  });

  it('test_session_detail_resolves_url_identity_without_session_list_position', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({ data: session, trace_id: '70000000-0000-4000-8000-000000000001' }),
    );

    const detail = await loadQASession(
      clientFor(fetcher),
      scope,
      session.session_id,
      new AbortController().signal,
    );

    expect(detail.session_id).toBe(session.session_id);
    const request = fetcher.mock.calls[0]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe(`/api/v1/qa/sessions/${session.session_id}`);
  });

  it('test_message_page_rejects_a_response_from_another_workspace_without_exposing_content', async () => {
    const privateQuestion = 'foreign workspace protected question';
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        workspace_id: '10000000-0000-4000-8000-000000000002',
        environment_id: scope.environmentId,
        items: [{ session_id: session.session_id, question: privateQuestion }],
        next_cursor: null,
        limit: 25,
      }),
    );

    await expect(
      loadQAMessagePage(clientFor(fetcher), scope, session.session_id, null, new AbortController().signal),
    ).rejects.toThrow('The Q&A message page is unavailable.');
  });
});
