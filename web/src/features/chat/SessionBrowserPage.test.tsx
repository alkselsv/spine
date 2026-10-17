import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createHash } from 'node:crypto';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';

import { AuthProvider } from '../../app/auth';
import { ScopeContext, type Scope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import { qaSessionQueryKeys } from './session-api';
import { SessionBrowserPage } from './SessionBrowserPage';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const sessionId = '30000000-0000-4000-8000-000000000001';
const subjectId = '40000000-0000-4000-8000-000000000001';
const messageId = '50000000-0000-4000-8000-000000000001';
const admittedMessageId = '50000000-0000-4000-8000-000000000002';
const askCommandId = 'd0000000-0000-4000-8000-000000000001';
const traceId = '60000000-0000-4000-8000-000000000001';
const runId = '70000000-0000-4000-8000-000000000001';
const answerId = '80000000-0000-4000-8000-000000000001';
const attemptId = '90000000-0000-4000-8000-000000000001';
const claimId = 'a0000000-0000-4000-8000-000000000001';
const relationshipId = 'b0000000-0000-4000-8000-000000000001';
const citationId = 'c0000000-0000-4000-8000-000000000001';

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
  overrides: Record<string, unknown> = {},
) {
  const acceptedPresentation = answer && terminalOutcome === 'answered'
    ? {
        schema_version: 'accepted-answer-presentation:v1',
        answer_id: answerId,
        answer_text_sha256: createHash('sha256').update(answer, 'utf8').digest('hex'),
        attempt_id: attemptId,
        message_id: messageId,
        run_id: runId,
        claims: [{
          schema_version: 'accepted-answer-claim-presentation:v1',
          claim_id: claimId,
          kind: 'factual',
          start_offset: 0,
          end_offset: answer.length,
        }],
        relationships: [{
          schema_version: 'accepted-answer-relationship-presentation:v1',
          relationship_id: relationshipId,
          claim_id: claimId,
          kind: 'independent',
          citation_ids: [citationId],
          related_claim_id: null,
        }],
        citation_ids: [citationId],
      }
    : null;
  return {
    schema_version: 1,
    message_id: messageId,
    session_id: sessionId,
    workspace_id: scope.workspaceId,
    environment_id: scope.environmentId,
    version: 1,
    ordinal: 1,
    trace_id: traceId,
    run_id: runId,
    state: 'terminal',
    terminal_outcome: terminalOutcome,
    cancelled: false,
    disclosure_eligibility: 'eligible',
    question,
    answer,
    accepted_presentation: acceptedPresentation,
    created_at: '2026-10-17T10:01:00Z',
    updated_at: '2026-10-17T10:01:00Z',
    ...overrides,
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

function renderSessionMessages(messages: unknown[]) {
  const fetcher = vi.fn<typeof fetch>(async (input, init) => {
    const request = new Request(input, init);
    const url = new URL(request.url);
    if (url.pathname === `/api/v1/qa/sessions/${sessionId}`) {
      return response({ data: session(), workspace_id: scope.workspaceId, environment_id: scope.environmentId, trace_id: traceId });
    }
    if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages`) {
      return collection(request, messages, null);
    }
    return response({ error: 'unexpected request' });
  });
  vi.stubGlobal('fetch', fetcher);
  return renderPage(`/chat/${sessionId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`);
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
    expect((await screen.findAllByText('В разделе HR.'))[0]).toBeVisible();
    expect(screen.getByText('Ответ подготовлен')).toBeVisible();
    expect(screen.getByRole('link', { name: ru.chat.openRun })).toHaveAttribute(
      'href',
      `/qa/runs/${runId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&return_session_id=${sessionId}&return_message_id=${messageId}`,
    );
    expect(screen.getByLabelText('Текущий адрес')).toHaveTextContent(`/chat/${sessionId}`);
    expect(createHeaders[0]?.get('Idempotency-Key')).toBeTruthy();
    expect(createHeaders[0]?.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect(createHeaders[0]?.get('X-Environment-ID')).toBe(scope.environmentId);

    cleanup();
    renderPage(`/chat/${sessionId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`);
    expect(await screen.findByText('Где находится политика отпуска?')).toBeVisible();
  });

  it('test_message_deep_link_reloads_the_exact_scoped_message_and_return_run', async () => {
    const currentMessage = message();
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init);
      const url = new URL(request.url);
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}`) {
        return response({ data: session(), workspace_id: scope.workspaceId, environment_id: scope.environmentId, trace_id: traceId });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages` && request.method === 'GET') {
        return collection(request, [], null);
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages/${messageId}`) {
        return response({
          schema_version: 1,
          trace_id: traceId,
          workspace_id: scope.workspaceId,
          environment_id: scope.environmentId,
          data: currentMessage,
        });
      }
      return response({ error: 'unexpected request' });
    }));

    renderPage(
      `/chat/${sessionId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&message_id=${messageId}&run_id=${runId}&return_run_id=${runId}`,
    );

    expect(await screen.findByText('Где находится политика отпуска?')).toBeVisible();
    expect(screen.getByRole('link', { name: ru.chat.backToRun })).toHaveAttribute(
      'href',
      `/qa/runs/${runId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&return_session_id=${sessionId}&return_message_id=${messageId}`,
    );
  });

  it('test_message_deep_link_retries_exact_message_when_history_is_unavailable', async () => {
    const user = userEvent.setup();
    let exactMessageRequests = 0;
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init);
      const url = new URL(request.url);
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}`) {
        return response({ data: session(), workspace_id: scope.workspaceId, environment_id: scope.environmentId, trace_id: traceId });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages` && request.method === 'GET') {
        return response({ error: 'history unavailable' });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages/${messageId}`) {
        exactMessageRequests += 1;
        if (exactMessageRequests === 1) return response({ error: 'temporarily unavailable' });
        return response({
          schema_version: 1,
          trace_id: traceId,
          workspace_id: scope.workspaceId,
          environment_id: scope.environmentId,
          data: message(),
        });
      }
      return response({ error: 'unexpected request' });
    }));

    renderPage(
      `/chat/${sessionId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&message_id=${messageId}&run_id=${runId}`,
    );

    expect(await screen.findByRole('button', { name: ru.chat.retryMessages })).toBeVisible();
    await user.click(screen.getByRole('button', { name: ru.chat.retryMessages }));

    expect(await screen.findByText('Где находится политика отпуска?')).toBeVisible();
    expect(exactMessageRequests).toBe(2);
  });

  it('test_message_deep_link_hides_a_run_mismatch', async () => {
    const otherRunId = '70000000-0000-4000-8000-000000000099';
    const currentMessage = message();
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init);
      const url = new URL(request.url);
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}`) {
        return response({ data: session(), workspace_id: scope.workspaceId, environment_id: scope.environmentId, trace_id: traceId });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages` && request.method === 'GET') {
        return collection(request, [], null);
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages/${messageId}`) {
        return response({
          schema_version: 1,
          trace_id: traceId,
          workspace_id: scope.workspaceId,
          environment_id: scope.environmentId,
          data: currentMessage,
        });
      }
      return response({ error: 'unexpected request' });
    }));

    renderPage(
      `/chat/${sessionId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&message_id=${messageId}&run_id=${otherRunId}`,
    );

    expect(await screen.findByRole('region', { name: ru.status.failed.title })).toBeVisible();
    expect(screen.queryByText('Где находится политика отпуска?')).toBeNull();
    expect(screen.queryByText('В разделе HR.')).toBeNull();
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

  it.each([
    ['clarification_required', 'Нужно уточнение'],
    ['abstained', 'Недостаточно данных для ответа'],
    ['denied', 'Доступ запрещён'],
    ['failed', 'Завершилось с ошибкой'],
    ['cancelled', 'Отменён'],
  ] as const)(
    'test_%s_outcome_hides_unaccepted_candidate_text',
    async (outcome, label) => {
      renderSessionMessages([message('Вопрос', 'Кандидат ответа не должен появиться.', outcome)]);

      expect(await screen.findByText(label)).toBeVisible();
      expect(screen.queryByText('Кандидат ответа не должен появиться.')).not.toBeInTheDocument();
    },
  );

  it('test_answered_message_hides_answer_when_accepted_digest_does_not_match', async () => {
    const accepted = message('Вопрос', 'Чужой или изменённый ответ.');
    accepted.accepted_presentation!.answer_text_sha256 = '0'.repeat(64);
    renderSessionMessages([accepted]);

    expect(await screen.findByText(ru.chat.answerUnavailable)).toBeVisible();
    expect(screen.queryByText('Чужой или изменённый ответ.')).not.toBeInTheDocument();
  });

  it('test_raw_html_in_accepted_answer_is_rejected_without_creating_elements', async () => {
    renderSessionMessages([message('Вопрос', '<img src=x onerror="alert(1)">')]);

    expect(await screen.findByText(ru.chat.answerUnsafe)).toBeVisible();
    expect(document.querySelector('img')).toBeNull();
    expect(screen.queryByText(/alert\(1\)/)).not.toBeInTheDocument();
  });

  it('test_safe_markdown_formats_text_and_rejects_unsafe_links', async () => {
    renderSessionMessages([message('Вопрос', '**Принятый факт** и [ссылка](https://example.org), [опасная](javascript:alert(1))')]);

    expect(await screen.findByText('Принятый факт')).toBeVisible();
    expect(screen.getByText('Принятый факт').tagName).toBe('STRONG');
    expect(screen.getByRole('link', { name: 'ссылка' })).toHaveAttribute('href', 'https://example.org/');
    expect(screen.queryByRole('link', { name: 'опасная' })).not.toBeInTheDocument();
    expect(within(screen.getByRole('link', { name: 'ссылка' }).closest('p')!).getByText(/опасная/)).toBeVisible();
  });

  it('test_ineligible_message_hides_question_and_answer_even_if_payload_contains_them', async () => {
    renderSessionMessages([message('Защищённый вопрос', 'Защищённый ответ.', 'answered', { disclosure_eligibility: 'ineligible' })]);

    expect(await screen.findByText(ru.chat.messageContentHidden)).toBeVisible();
    expect(screen.queryByText('Защищённый вопрос')).not.toBeInTheDocument();
    expect(screen.queryByText('Защищённый ответ.')).not.toBeInTheDocument();
  });

  it('test_ask_admission_uses_scoped_command_and_hydrates_the_canonical_message', async () => {
    const user = userEvent.setup();
    const askRequests: Array<{ body: unknown; headers: Headers }> = [];
    let hydratedMessageRequested = false;
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init);
      const url = new URL(request.url);
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}`) {
        return response({ data: session(), workspace_id: scope.workspaceId, environment_id: scope.environmentId, trace_id: traceId });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages`) {
        return collection(request, [], null);
      }
      if (url.pathname === '/api/v1/ask/admissions' && request.method === 'POST') {
        askRequests.push({ body: await request.json(), headers: request.headers });
        return response({
          schema_version: 1,
          command_id: askCommandId,
          workspace_id: scope.workspaceId,
          environment_id: scope.environmentId,
          trace_id: traceId,
          status: 'accepted',
          replay: false,
          result: {
            schema_version: 1,
            command_id: askCommandId,
            session_id: sessionId,
            message_id: admittedMessageId,
            run_id: runId,
            status: 'admitted',
          },
        });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages/${admittedMessageId}`) {
        hydratedMessageRequested = true;
        return response({
          data: message('Где лежит отпускная политика?', null, 'answered', {
            message_id: admittedMessageId,
            state: 'pending',
            terminal_outcome: null,
            accepted_presentation: null,
          }),
          schema_version: 1,
          workspace_id: scope.workspaceId,
          environment_id: scope.environmentId,
          trace_id: traceId,
        });
      }
      return response({ error: 'unexpected request' });
    });
    vi.stubGlobal('fetch', fetcher);
    renderPage(`/chat/${sessionId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`);

    await user.type(await screen.findByRole('textbox', { name: 'Вопрос' }), 'Где лежит отпускная политика?');
    await user.click(screen.getByRole('button', { name: 'Отправить' }));

    expect(await screen.findByText('Ожидает выполнения')).toBeVisible();
    expect(screen.getByText('Где лежит отпускная политика?')).toBeVisible();
    expect(askRequests).toHaveLength(1);
    expect(askRequests[0]?.body).toEqual({ schema_version: 1, session_id: sessionId, question: 'Где лежит отпускная политика?' });
    expect(askRequests[0]?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(askRequests[0]?.headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect(askRequests[0]?.headers.get('X-Environment-ID')).toBe(scope.environmentId);
    expect(hydratedMessageRequested).toBe(true);
  });

  it('test_clarification_reply_links_to_the_canonical_message', async () => {
    const user = userEvent.setup();
    let askBody: unknown;
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init);
      const url = new URL(request.url);
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}`) {
        return response({ data: session(), workspace_id: scope.workspaceId, environment_id: scope.environmentId, trace_id: traceId });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages`) {
        return collection(request, [message('Какой отпуск?', null, 'clarification_required')], null);
      }
      if (url.pathname === '/api/v1/ask/admissions' && request.method === 'POST') {
        askBody = await request.json();
        return response({
          schema_version: 1,
          command_id: askCommandId,
          workspace_id: scope.workspaceId,
          environment_id: scope.environmentId,
          trace_id: traceId,
          status: 'accepted',
          replay: false,
          result: {
            schema_version: 1,
            command_id: askCommandId,
            session_id: sessionId,
            message_id: admittedMessageId,
            run_id: runId,
            status: 'admitted',
          },
        });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages/${admittedMessageId}`) {
        return response({
          data: message('За какой период?', null, 'answered', {
            message_id: admittedMessageId,
            state: 'pending',
            terminal_outcome: null,
            accepted_presentation: null,
          }),
          schema_version: 1,
          workspace_id: scope.workspaceId,
          environment_id: scope.environmentId,
          trace_id: traceId,
        });
      }
      return response({ error: 'unexpected request' });
    });
    vi.stubGlobal('fetch', fetcher);
    renderPage(`/chat/${sessionId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`);

    await user.click(await screen.findByRole('button', { name: 'Ответить на уточнение' }));
    expect(screen.getByText(ru.chat.clarificationReply).closest('[role="status"]')).toHaveTextContent(messageId);
    await user.type(screen.getByRole('textbox', { name: 'Вопрос' }), 'За какой период?');
    await user.click(screen.getByRole('button', { name: 'Отправить' }));

    await screen.findByText('Ожидает выполнения');
    expect(askBody).toEqual({
      schema_version: 1,
      session_id: sessionId,
      question: 'За какой период?',
      clarification_of_message_id: messageId,
    });
  });

  it('test_current_message_disclosure_failure_hides_the_session_history', async () => {
    const user = userEvent.setup();
    let admissionAccepted = false;
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init);
      const url = new URL(request.url);
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}`) {
        return response({ data: session(), workspace_id: scope.workspaceId, environment_id: scope.environmentId, trace_id: traceId });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages`) {
        return collection(request, [message('Старая защищённая история', 'Старый ответ')], null);
      }
      if (url.pathname === '/api/v1/ask/admissions' && request.method === 'POST') {
        admissionAccepted = true;
        return response({
          schema_version: 1,
          command_id: askCommandId,
          workspace_id: scope.workspaceId,
          environment_id: scope.environmentId,
          trace_id: traceId,
          status: 'accepted',
          replay: false,
          result: {
            schema_version: 1,
            command_id: askCommandId,
            session_id: sessionId,
            message_id: admittedMessageId,
            run_id: runId,
            status: 'admitted',
          },
        });
      }
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages/${admittedMessageId}` && admissionAccepted) {
        return new Response(JSON.stringify({ code: 'spine.authorization.denied', protected_text: 'customer secret' }), {
          status: 403,
          headers: { 'content-type': 'application/json' },
        });
      }
      return response({ error: 'unexpected request' });
    });
    vi.stubGlobal('fetch', fetcher);
    renderPage(`/chat/${sessionId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`);

    expect(await screen.findByText('Старая защищённая история')).toBeVisible();
    await user.type(await screen.findByRole('textbox', { name: 'Вопрос' }), 'Новый вопрос');
    await user.click(screen.getByRole('button', { name: 'Отправить' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Доступ запрещён');
    expect(screen.queryByText('Старая защищённая история')).not.toBeInTheDocument();
    expect(screen.queryByText('Старый ответ')).not.toBeInTheDocument();
    expect(screen.queryByText('customer secret')).not.toBeInTheDocument();
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
