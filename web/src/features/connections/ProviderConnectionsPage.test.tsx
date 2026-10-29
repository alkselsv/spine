import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AuthProvider } from '../../app/auth';
import { ScopeContext, type Scope } from '../../app/scope';
import { ProviderConnectionsPage } from './ProviderConnectionsPage';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Provider connection control plane', () => {
  it('test_loading_then_empty_state_is_explicit', async () => {
    let release!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve) => { release = resolve; })));
    renderPage();

    expect(screen.getByRole('status')).toHaveTextContent('Загружаем подключения');
    await waitFor(() => expect(release).toBeTypeOf('function'));
    release(Response.json(collection([])));

    expect(await screen.findByText('Подключения не настроены')).toBeVisible();
  });

  it('test_stale_state_is_explicit_while_live_inspection_is_pending', async () => {
    let releaseInspection!: (response: Response) => void;
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(collection([connection()])))
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { releaseInspection = resolve; }));
    vi.stubGlobal('fetch', fetch);
    renderPage();

    expect(await screen.findByText(/показано последнее сохранённое состояние/i)).toBeVisible();
    releaseInspection(Response.json({ data: connection() }));
    await waitFor(() => expect(screen.queryByText(/показано последнее сохранённое состояние/i)).not.toBeInTheDocument());
  });

  it('test_partial_state_keeps_safe_persisted_card_when_inspection_fails', async () => {
    vi.stubGlobal('fetch', vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(collection([connection()])))
      .mockResolvedValueOnce(Response.json({ code: 'spine.provider.unavailable' }, { status: 503 })));
    renderPage();

    expect(await screen.findByText(/часть актуальных проверок недоступна/i)).toBeVisible();
    expect(screen.getByText('Подключение исправно')).toBeVisible();
  });

  it.each([
    ['active', 'healthy', 'Подключение исправно'],
    ['active', 'degraded', 'Подключение работает с ограничениями'],
    ['revoked', 'revoked', 'Доступ отозван'],
    ['active', 'reconciliation_required', 'Требуется сверка'],
  ])('test_%s_%s_state_has_distinct_operator_copy', async (status, health, expected) => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockResolvedValue(
        Response.json(collection([connection({ status, health })])),
      ),
    );
    renderPage();

    expect(await screen.findByText(expected)).toBeVisible();
    expect(screen.getByText('binary_read')).toBeVisible();
    expect(document.body).not.toHaveTextContent('credential-secret');
    expect(document.body).not.toHaveTextContent('protected-name.pdf');
  });

  it('test_permission_denied_state_does_not_render_provider_payload', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockResolvedValue(
        Response.json({
          schema_version: 1,
          code: 'spine.authorization.denied',
          safe_message: 'Authorization is denied.',
          retryability: 'never',
          trace_id: '50000000-0000-4000-8000-000000000001',
          provider_detail: 'protected-name.pdf',
        }, { status: 403 }),
      ),
    );
    renderPage();

    expect(await screen.findByText('Доступ запрещён')).toBeVisible();
    expect(document.body).not.toHaveTextContent('protected-name.pdf');
  });

  it('test_create_uses_approved_reference_and_reports_explicit_version_conflict', async () => {
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(collection([])))
      .mockResolvedValueOnce(Response.json({
        schema_version: 1,
        code: 'spine.persistence.optimistic_conflict',
        safe_message: 'The operation conflicts with a newer version.',
        retryability: 'never',
        trace_id: '50000000-0000-4000-8000-000000000001',
      }, { status: 409 }));
    vi.stubGlobal('fetch', fetch);
    renderPage();
    await screen.findByText('Подключения не настроены');

    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Approval ID'), '30000000-0000-4000-8000-000000000001');
    await user.type(screen.getByLabelText('Connection ID'), '40000000-0000-4000-8000-000000000001');
    await user.click(screen.getByRole('button', { name: 'Создать версию' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Конфигурация уже изменилась');
    const request = fetch.mock.calls[1]?.[0];
    expect(request).toBeInstanceOf(Request);
    const body = JSON.parse(await (request as Request).clone().text());
    expect(body.approval_id).toBe('30000000-0000-4000-8000-000000000001');
    expect(body).not.toHaveProperty('credential');
    expect((request as Request).headers.get('Idempotency-Key')).toBeTruthy();
  });

  it('test_inspection_refreshes_health_and_successor_posts_current_version_fence', async () => {
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(collection([connection()])))
      .mockResolvedValueOnce(Response.json({
        schema_version: 1,
        workspace_id: scope.workspaceId,
        environment_id: scope.environmentId,
        trace_id: '50000000-0000-4000-8000-000000000001',
        data: connection({ health: 'degraded', diagnostic_codes: ['provider_degraded'] }),
      }))
      .mockResolvedValueOnce(Response.json({
        schema_version: 1,
        code: 'spine.persistence.optimistic_conflict',
        safe_message: 'The operation conflicts with a newer version.',
        retryability: 'never',
        trace_id: '50000000-0000-4000-8000-000000000001',
      }, { status: 409 }));
    vi.stubGlobal('fetch', fetch);
    renderPage();

    expect(await screen.findByText('Подключение работает с ограничениями')).toBeVisible();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Новая версия' }));
    await user.type(screen.getByLabelText('Approval ID'), '30000000-0000-4000-8000-000000000002');
    await user.click(screen.getByRole('button', { name: 'Создать новую версию' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Конфигурация уже изменилась');
    const request = fetch.mock.calls[2]?.[0] as Request;
    const body = JSON.parse(await request.clone().text());
    expect(body.expected_version).toBe(1);
    expect(body.expected_predecessor_version_id).toBe('41000000-0000-4000-8000-000000000001');
  });

  it('test_collection_exposes_and_follows_next_cursor', async () => {
    const second = connection({
      connection_id: '40000000-0000-4000-8000-000000000002',
      connection_version_id: '41000000-0000-4000-8000-000000000002',
    });
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json({ ...collection([connection()]), next_cursor: 'signed-next' }))
      .mockResolvedValueOnce(Response.json({ data: connection() }))
      .mockResolvedValueOnce(Response.json(collection([second])))
      .mockResolvedValueOnce(Response.json({ data: second }));
    vi.stubGlobal('fetch', fetch);
    renderPage();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: 'Показать ещё' }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));

    const pageRequest = fetch.mock.calls[2]?.[0] as Request;
    expect(new URL(pageRequest.url).searchParams.get('cursor')).toBe('signed-next');
    expect(screen.getAllByText('Подключение исправно')).toHaveLength(2);
  });
});

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider value={{ status: 'authenticated', role: 'administrator', getBearerCredential: () => 'test-token' }}>
        <ScopeContext.Provider value={{ scope, queryKey: (resource) => ['spine', resource, scope.workspaceId, scope.environmentId] }}>
          <ProviderConnectionsPage />
        </ScopeContext.Provider>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

function collection(items: ReturnType<typeof connection>[]) {
  return {
    schema_version: 1,
    workspace_id: scope.workspaceId,
    environment_id: scope.environmentId,
    trace_id: '50000000-0000-4000-8000-000000000001',
    items,
    limit: 100,
    next_cursor: null,
  };
}

function connection(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    connection_id: '40000000-0000-4000-8000-000000000001',
    connection_version_id: '41000000-0000-4000-8000-000000000001',
    approval_id: '30000000-0000-4000-8000-000000000001',
    version: 1,
    predecessor_version_id: null,
    provider_kind: 'yandex_disk',
    status: 'active',
    authorization_generation: 2,
    configuration_generation: 3,
    approved_resource_scope_references: ['42000000-0000-4000-8000-000000000001'],
    capabilities: [{ kind: 'binary_read', status: 'supported', qualification_reference: '43000000-0000-4000-8000-000000000001' }],
    health: 'healthy',
    diagnostic_codes: ['connection_ready'],
    observed_at: '2026-10-29T17:00:00Z',
    cursor_lag_seconds: 4,
    last_successful_reconciliation_at: '2026-10-29T16:00:00Z',
    ...overrides,
  };
}
