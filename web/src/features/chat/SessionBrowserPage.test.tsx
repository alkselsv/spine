import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';

import { AuthProvider } from '../../app/auth';
import { ScopeContext, type Scope } from '../../app/scope';
import { qaSessionQueryKeys } from './session-api';
import { SessionBrowserPage } from './SessionBrowserPage';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const sessionId = '30000000-0000-4000-8000-000000000001';
const subjectId = '40000000-0000-4000-8000-000000000001';
const messageId = '50000000-0000-4000-8000-000000000001';
const traceId = '60000000-0000-4000-8000-000000000001';

function session(status: 'active' | 'archived' = 'active', version = 1) {
  return {
    schema_version: 1,
    session_id: sessionId,
    workspace_id: scope.workspaceId,
    environment_id: scope.environmentId,
    subject_id: subjectId,
    version,
    status,
    created_at: '2026-10-17T10:00:00Z',
    updated_at: '2026-10-17T10:00:00Z',
  };
}

function message(
  question = 'Где находится политика отпуска?',
  answer: string | null = 'В разделе HR.',
  terminalOutcome: 'answered' | 'clarification_required' | 'abstained' | 'denied' | 'failed' | 'cancelled' = 'answered',
) {
  return {
    schema_version: 1,
    message_id: messageId,
    session_id: sessionId,
    workspace_id: scope.workspaceId,
    environment_id: scope.environmentId,
    version: 1,
    ordinal: 1,
    trace_id: traceId,
    run_id: null,
    state: 'terminal',
    terminal_outcome: terminalOutcome,
    cancelled: false,
    disclosure_eligibility: 'eligible',
    question,
    answer,
    created_at: '2026-10-17T10:01:00Z',
    updated_at: '2026-10-17T10:01:00Z',
  };
}

function response(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

function collection(request: Request, items: unknown[], nextCursor: string | null) {
  return response({
    workspace_id: request.headers.get('X-Workspace-ID'),
    environment_id: request.headers.get('X-Environment-ID'),
    items,
    next_cursor: nextCursor,
    limit: 25,
  });
}

function LocationProbe() {
  const location = useLocation();
  return <output aria-label="Текущий адрес">{location.pathname + location.search}</output>;
}

function renderPage(entry: string, currentScope: Scope = scope, seedStaleContent = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  const getBearerCredential = () => 'test-token';
  const identityVersion = 'principal-a';
  if (seedStaleContent) {
    const updatedAt = Date.parse('2020-01-01T00:00:00.000Z');
    client.setQueryData(
      qaSessionQueryKeys.session(currentScope, sessionId, identityVersion),
      session(),
      { updatedAt },
    );
    client.setQueryData(
      qaSessionQueryKeys.messages(currentScope, sessionId, null, identityVersion),
      { items: [message('Cached private question', 'Cached private answer')], next_cursor: null, limit: 25 },
      { updatedAt },
    );
  }
  function PageHarness({ pageScope, authIdentity = identityVersion }: { pageScope: Scope; authIdentity?: string }) {
    return (
      <QueryClientProvider client={client}>
        <AuthProvider
          value={{ status: 'authenticated', role: 'administrator', getBearerCredential, identityVersion: authIdentity }}
        >
          <ScopeContext.Provider value={{ scope: pageScope, queryKey: () => [] }}>
            <MemoryRouter initialEntries={[entry]}>
              <Routes>
                <Route path="/chat" element={<SessionBrowserPage />} />
                <Route path="/chat/:sessionId" element={<SessionBrowserPage />} />
              </Routes>
              <LocationProbe />
            </MemoryRouter>
          </ScopeContext.Provider>
        </AuthProvider>
      </QueryClientProvider>
    );
  }
  const view = render(<PageHarness pageScope={currentScope} />);
  return {
    ...view,
    queryClient: client,
    rerenderScope: (pageScope: Scope) => view.rerender(<PageHarness pageScope={pageScope} />),
    rerenderIdentity: (authIdentity: string) =>
      view.rerender(<PageHarness pageScope={currentScope} authIdentity={authIdentity} />),
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Q&A session navigation', () => {
  it('test_create_session_opens_canonical_url_and_restores_messages_after_reload', async () => {
    const user = userEvent.setup();
    const createHeaders: Headers[] = [];
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init);
      const url = new URL(request.url);
      if (url.pathname === '/api/v1/qa/sessions' && request.method === 'GET') {
        return collection(request, [], null);
      }
      if (url.pathname === '/api/v1/qa/sessions' && request.method === 'POST') {
        createHeaders.push(request.headers);
        return response({ result: session(), replay: false, status: 'completed' });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}`) {
        return response({ data: session(), workspace_id: scope.workspaceId, environment_id: scope.environmentId, trace_id: traceId });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages`) {
        return collection(request, [message()], null);
      }
      return response({ error: 'unexpected request' });
    });
    vi.stubGlobal('fetch', fetcher);

    renderPage('/chat?workspace=10000000-0000-4000-8000-000000000001&environment=20000000-0000-4000-8000-000000000001');

    await user.click(await screen.findByRole('button', { name: 'Создать сессию' }));

    expect(await screen.findByText('Где находится политика отпуска?')).toBeVisible();
    expect(screen.queryByText('В разделе HR.')).not.toBeInTheDocument();
    expect(screen.getByText('Ответ подготовлен')).toBeVisible();
    expect(screen.getByLabelText('Текущий адрес')).toHaveTextContent(`/chat/${sessionId}`);
    expect(createHeaders[0]?.get('Idempotency-Key')).toBeTruthy();
    expect(createHeaders[0]?.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect(createHeaders[0]?.get('X-Environment-ID')).toBe(scope.environmentId);

    cleanup();
    renderPage(`/chat/${sessionId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`);
    expect(await screen.findByText('Где находится политика отпуска?')).toBeVisible();
  });

  it('test_archived_session_requires_restore_before_messages_are_loaded', async () => {
    const user = userEvent.setup();
    let messagePageRequested = false;
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init);
      const url = new URL(request.url);
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}`) {
        return response({ data: session('archived', 4), workspace_id: scope.workspaceId, environment_id: scope.environmentId, trace_id: traceId });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/restore`) {
        expect(request.headers.get('Idempotency-Key')).toBeTruthy();
        expect(await request.json()).toEqual({ expected_version: 4 });
        return response({ result: session('active', 5), replay: false, status: 'completed' });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages`) {
        messagePageRequested = true;
        return collection(request, [message()], null);
      }
      return response({ error: 'unexpected request' });
    });
    vi.stubGlobal('fetch', fetcher);

    renderPage(`/chat/${sessionId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`);

    expect(await screen.findByText('Сессия архивирована')).toBeVisible();
    expect(messagePageRequested).toBe(false);
    await user.click(screen.getByRole('button', { name: 'Восстановить сессию' }));

    expect(await screen.findByText('Где находится политика отпуска?')).toBeVisible();
  });

  it('test_failed_message_is_not_presented_as_an_answered_message', async () => {
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = new URL(new Request(input, init).url);
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}`) {
        return response({ data: session(), workspace_id: scope.workspaceId, environment_id: scope.environmentId, trace_id: traceId });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages`) {
        return collection(new Request(input, init), [message('Вопрос с ошибкой', 'Нельзя показывать ответ здесь.', 'failed')], null);
      }
      return response({ error: 'unexpected request' });
    });
    vi.stubGlobal('fetch', fetcher);

    renderPage(`/chat/${sessionId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`);

    expect(await screen.findByText('Завершилось с ошибкой')).toBeVisible();
    expect(screen.queryByText('Ответ подготовлен')).not.toBeInTheDocument();
    expect(screen.queryByText('Нельзя показывать ответ здесь.')).not.toBeInTheDocument();
  });

  it('test_scope_change_clears_session_url_and_protected_history', async () => {
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = new URL(new Request(input, init).url);
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}`) {
        return response({ data: session(), workspace_id: scope.workspaceId, environment_id: scope.environmentId, trace_id: traceId });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages`) {
        return collection(new Request(input, init), [message('Private workspace question', 'Private workspace answer')], null);
      }
      return collection(new Request(input, init), [], null);
    });
    vi.stubGlobal('fetch', fetcher);
    const view = renderPage(`/chat/${sessionId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`);

    expect(await screen.findByText('Private workspace question')).toBeVisible();
    const nextScope: Scope = { ...scope, workspaceId: '10000000-0000-4000-8000-000000000002' };
    view.rerenderScope(nextScope);

    await waitFor(() => expect(screen.queryByText('Private workspace question')).not.toBeInTheDocument());
    expect(screen.getByLabelText('Текущий адрес')).toHaveTextContent('/chat?');
    expect(screen.queryByText('Private workspace answer')).not.toBeInTheDocument();
    expect(view.queryClient.getQueriesData({ queryKey: ['spine', 'qa-chat', scope.workspaceId, scope.environmentId] })).toEqual([]);
  });

  it('test_denied_revalidation_hides_cached_protected_messages', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ code: 'spine.authorization.denied' }), {
        status: 403,
        headers: { 'content-type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetcher);
    renderPage(`/chat/${sessionId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`, scope, true);

    await waitFor(() => expect(fetcher).toHaveBeenCalled());
    expect(screen.queryByText('Cached private question')).not.toBeInTheDocument();
    expect(screen.queryByText('Cached private answer')).not.toBeInTheDocument();
    expect(await screen.findByRole('alert')).toHaveTextContent('Доступ запрещён');
  });

  it('test_auth_identity_change_clears_cached_messages_even_with_stable_credential_provider', async () => {
    let detailRequests = 0;
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = new URL(new Request(input, init).url);
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}`) {
        detailRequests += 1;
        if (detailRequests === 1) {
          return new Response(JSON.stringify({ code: 'spine.authorization.denied' }), {
            status: 403,
            headers: { 'content-type': 'application/json' },
          });
        }
        return response({ data: session(), workspace_id: scope.workspaceId, environment_id: scope.environmentId, trace_id: traceId });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages`) {
        return collection(new Request(input, init), [message('New principal private question')], null);
      }
      return new Response(JSON.stringify({ code: 'spine.authorization.denied' }), {
        status: 403,
        headers: { 'content-type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetcher);
    const view = renderPage(`/chat/${sessionId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`, scope, true);

    await waitFor(() => expect(fetcher).toHaveBeenCalled());
    view.rerenderIdentity('principal-b');

    await waitFor(() => expect(screen.queryByText('Cached private question')).not.toBeInTheDocument());
    expect(screen.queryByText('Cached private answer')).not.toBeInTheDocument();
    expect(await screen.findByText('New principal private question')).toBeVisible();
  });

  it('test_next_session_page_uses_server_cursor_without_reusing_first_page', async () => {
    const user = userEvent.setup();
    const nextId = '30000000-0000-4000-8000-000000000002';
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = new URL(new Request(input, init).url);
      if (url.pathname !== '/api/v1/qa/sessions') return response({ error: 'unexpected request' });
      return url.searchParams.has('cursor')
        ? collection(new Request(input, init), [{ ...session(), session_id: nextId }], null)
        : collection(new Request(input, init), [session()], 'page-2');
    });
    vi.stubGlobal('fetch', fetcher);

    renderPage('/chat');
    await screen.findByRole('link', { name: new RegExp(`Сессия · ${sessionId.slice(-8)}`) });
    await user.click(screen.getByRole('button', { name: 'Следующая страница' }));

    expect(await screen.findByRole('link', { name: new RegExp(`Сессия · ${nextId.slice(-8)}`) })).toBeVisible();
    expect(screen.getByLabelText('Текущий адрес')).toHaveTextContent('session_cursor=page-2');
  });
});
