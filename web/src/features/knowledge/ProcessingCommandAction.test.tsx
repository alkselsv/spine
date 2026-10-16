import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createMemoryRouter, RouterProvider } from 'react-router';

import { createScopedApiClient } from '../../api/client';
import { ProcessingCommandAction } from './ProcessingCommandAction';

const workspaceId = '10000000-0000-4000-8000-000000000001';
const environmentId = '20000000-0000-4000-8000-000000000001';
const sourceObjectId = '30000000-0000-4000-8000-000000000001';
const sourceRevisionId = '40000000-0000-4000-8000-000000000001';
const operationId = '50000000-0000-4000-8000-000000000001';
const runId = '60000000-0000-4000-8000-000000000001';

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
});

describe('document processing command action', () => {
  it('test_retry_posts_server_marked_failure_polls_status_and_preserves_scope_in_run_link', async () => {
    const requests: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async (input) => {
      const request = input as Request;
      requests.push(request);
      return Response.json(request.method === 'POST' ? command('accepted') : command('ready'));
    });
    vi.stubGlobal('crypto', { randomUUID: () => 'stable-retry-key' });
    renderAction(fetch, {
      kind: 'retry',
      failureId: '70000000-0000-4000-8000-000000000001',
      retryable: true,
    });

    await userEvent.setup().click(screen.getByRole('button', { name: 'Повторить обработку файла' }));

    expect(await screen.findByText('Обработка завершена')).toBeVisible();
    const post = requests.find((request) => request.method === 'POST');
    expect(post?.url).toBe('https://spine.test/api/v1/documents/processing/retry');
    expect(post?.headers.get('Idempotency-Key')).toBe('stable-retry-key');
    expect(post?.headers.get('X-Workspace-ID')).toBe(workspaceId);
    expect(post?.headers.get('X-Environment-ID')).toBe(environmentId);
    expect(await post?.json()).toMatchObject({
      expected_current_revision_id: sourceRevisionId,
      parent_failure_id: '70000000-0000-4000-8000-000000000001',
      source_object_id: sourceObjectId,
      source_revision_id: sourceRevisionId,
    });
    expect(screen.getByRole('region', { name: 'Результат команды обработки' })).toHaveAttribute('aria-live', 'polite');
    expect(screen.getByRole('link', { name: 'Открыть запуск обработки' })).toHaveAttribute(
      'href',
      expect.stringContaining(`workspace=${workspaceId}&environment=${environmentId}`),
    );
    expect(requests.some((request) => request.url.includes('/processing/operations/'))).toBe(true);
  });

  it('test_retry_after_retryable_transport_error_reuses_the_same_idempotency_key', async () => {
    const keys: string[] = [];
    let postCount = 0;
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async (input) => {
      const request = input as Request;
      if (request.method === 'POST') {
        keys.push(request.headers.get('Idempotency-Key') ?? '');
        postCount += 1;
        if (postCount === 1) return Response.json(safeError('after_delay'), { status: 503 });
        return Response.json(command('accepted'));
      }
      return Response.json(command('ready'));
    });
    vi.stubGlobal('crypto', { randomUUID: () => 'one-logical-command' });
    renderAction(fetch, { kind: 'retry', failureId: '70000000-0000-4000-8000-000000000001', retryable: true });
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Повторить обработку файла' }));
    await screen.findByRole('button', { name: 'Повторить ту же команду' });
    await user.click(screen.getByRole('button', { name: 'Повторить ту же команду' }));

    await screen.findByText('Обработка завершена');
    expect(keys).toEqual(['one-logical-command', 'one-logical-command']);
  });

  it('test_retry_after_reload_reuses_the_pending_idempotency_key', async () => {
    const keys: string[] = [];
    let postCount = 0;
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async (input) => {
      const request = input as Request;
      if (request.method === 'POST') {
        keys.push(request.headers.get('Idempotency-Key') ?? '');
        postCount += 1;
        if (postCount === 1) return Response.json(safeError('after_delay'), { status: 503 });
        return Response.json(command('ready'));
      }
      return Response.json(command('ready'));
    });
    vi.stubGlobal('crypto', { randomUUID: () => 'persisted-logical-command' });
    const mode = { kind: 'retry' as const, failureId: '70000000-0000-4000-8000-000000000001', retryable: true };
    const firstPage = renderAction(fetch, mode);

    await userEvent.setup().click(screen.getByRole('button', { name: 'Повторить обработку файла' }));
    await screen.findByRole('button', { name: 'Повторить ту же команду' });
    firstPage.unmount();
    renderAction(fetch, mode);
    await userEvent.setup().click(screen.getByRole('button', { name: 'Повторить обработку файла' }));

    expect(await screen.findByText('Обработка завершена')).toBeVisible();
    expect(keys).toEqual(['persisted-logical-command', 'persisted-logical-command']);
  });

  it('test_retry_is_hidden_when_failure_is_not_server_marked_retryable', () => {
    renderAction(vi.fn<typeof globalThis.fetch>(), {
      kind: 'retry',
      failureId: '70000000-0000-4000-8000-000000000001',
      retryable: false,
    });

    expect(screen.queryByRole('button', { name: 'Повторить обработку файла' })).not.toBeInTheDocument();
  });

  it('test_denial_shows_safe_message_and_does_not_offer_command_replay', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json(safeError('never'), { status: 403 }),
    );
    renderAction(fetch, { kind: 'reprocess' });

    await userEvent.setup().click(screen.getByRole('button', { name: 'Обработать текущую ревизию заново' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Доступ запрещён');
    expect(screen.queryByRole('button', { name: 'Повторить ту же команду' })).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('provider diagnostic secret');
  });

  it.each([
    ['partial', 'Обработка завершена частично'],
    ['failed', 'Ошибка обработки'],
    ['cancelled', 'Обработка отменена'],
    ['unavailable', 'Состояние обработки недоступно'],
  ] as const)('test_%s_terminal_command_state_is_visible', async (status, label) => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(command(status)));
    renderAction(fetch, { kind: 'reprocess' });

    await userEvent.setup().click(screen.getByRole('button', { name: 'Обработать текущую ревизию заново' }));

    expect(await screen.findByText(label)).toBeVisible();
    expect(screen.getByText(operationId)).toBeVisible();
  });

  it('test_replayed_command_explains_that_the_receipt_was_reused', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(command('ready', true)));
    renderAction(fetch, { kind: 'reprocess' });

    await userEvent.setup().click(screen.getByRole('button', { name: 'Обработать текущую ревизию заново' }));

    expect(await screen.findByText('Показан сохранённый результат этой команды.')).toBeVisible();
  });

  it('test_conflict_offers_refresh_instead_of_replaying_stale_revision_command', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json(safeError('never'), { status: 409 }),
    );
    renderAction(fetch, { kind: 'reprocess' });

    await userEvent.setup().click(screen.getByRole('button', { name: 'Обработать текущую ревизию заново' }));

    expect(await screen.findByRole('button', { name: 'Обновить состояние ревизии' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Повторить ту же команду' })).not.toBeInTheDocument();
  });
});

function renderAction(
  fetch: typeof globalThis.fetch,
  mode: { kind: 'retry'; failureId: string; retryable: boolean } | { kind: 'reprocess' },
) {
  const client = createScopedApiClient({
    baseUrl: 'https://spine.test',
    fetch,
    getBearerCredential: () => 'test-token',
    scope: { workspaceId, environmentId },
  });
  const router = createMemoryRouter(
    [
      {
        path: '*',
        element: (
          <ProcessingCommandAction
            client={client}
            currentRevisionId={sourceRevisionId}
            mode={mode}
            processingReceiptId="80000000-0000-4000-8000-000000000001"
            queryKey={['spine', 'history', workspaceId, environmentId]}
            sourceObjectId={sourceObjectId}
            sourceRevisionId={sourceRevisionId}
          />
        ),
      },
    ],
    { initialEntries: [`/runs?workspace=${workspaceId}&environment=${environmentId}`] },
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

function command(
  status: 'accepted' | 'ready' | 'partial' | 'failed' | 'cancelled' | 'unavailable',
  replay = false,
) {
  return {
    failure_ids: [],
    item_id: '90000000-0000-4000-8000-000000000001',
    next_action: status === 'ready' ? 'none' : 'poll',
    operation_id: operationId,
    processing_receipt_id: status === 'ready' ? 'a0000000-0000-4000-8000-000000000001' : null,
    replay,
    run_id: runId,
    schema_version: 1,
    source_object_id: sourceObjectId,
    source_revision_id: sourceRevisionId,
    status,
    status_url: `https://spine.test/api/v1/documents/processing/operations/${operationId}`,
    trigger_kind: 'retry',
  };
}

function safeError(retryability: 'never' | 'after_delay') {
  return {
    code: 'spine.processing.denied',
    retryability,
    safe_message: 'The operation is not available for this item.',
    schema_version: 1,
    trace_id: 'b0000000-0000-4000-8000-000000000001',
    provider_detail: 'provider diagnostic secret',
  };
}
