import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AuthProvider } from '../../app/auth';
import { ScopeContext, type Scope } from '../../app/scope';
import { ProviderConnectionsPage } from './ProviderConnectionsPage';

const scope: Scope = { workspaceId: '10000000-0000-4000-8000-000000000001', environmentId: '20000000-0000-4000-8000-000000000001' };
const otherScope: Scope = { workspaceId: '10000000-0000-4000-8000-000000000002', environmentId: '20000000-0000-4000-8000-000000000001' };

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Approved mailbox connection control plane', () => {
  it('test_catalog_shows_only_approved_mailbox_connections_and_safe_scope_contracts', async () => {
    const gmail = connection({ connection_id: id(10), connection_version_id: id(11), provider_kind: 'google', approved_resource_scope_references: ['mailbox-scope:gmail:approved:10'], permitted_operations: ['enumerate_mailbox', 'read_raw_message', 'read_attachment'], capabilities: [{ kind: 'mailbox_enumeration', status: 'supported', qualification_reference: 'offline-gmail-v1' }, { kind: 'raw_message_read', status: 'supported', qualification_reference: 'offline-gmail-v1' }], mailbox_sync: { state: 'complete', last_complete_at: '2026-11-03T05:42:00Z', lag_seconds: 248 } });
    const yandexMail = connection({ connection_id: id(20), connection_version_id: id(21), provider_kind: 'yandex_mail', approved_resource_scope_references: ['mailbox-scope:yandex:approved:20'], permitted_operations: ['enumerate_folder_uids', 'read_message_metadata'], capabilities: [{ kind: 'message_metadata_read', status: 'supported', qualification_reference: 'offline-imap-v1' }], mailbox_sync: { state: 'partial', last_complete_at: '2026-11-02T20:15:00Z', lag_seconds: 35280 } });
    const drive = connection({ provider_kind: 'yandex_disk', mailbox_sync: null, permitted_operations: [] });
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(collection([gmail, yandexMail, drive]))));
    renderPage();

    expect(await screen.findByRole('heading', { name: 'Gmail' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Yandex Mail' })).toBeVisible();
    expect(screen.queryByText('yandex_disk')).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button', { name: /Открыть Gmail/ }));
    expect(screen.getByTestId('route-location')).toHaveTextContent(`connection=${id(10)}`);
    expect(screen.getByText('mailbox-scope:gmail:approved:10')).toBeVisible();
    expect(screen.getByText('Чтение исходного сообщения')).toBeVisible();
    expect(screen.getByText('Чтение вложений')).toBeVisible();
    expect(screen.getByText('Последняя полная синхронизация')).toBeVisible();
    expect(screen.getByText('5 мин')).toBeVisible();
    expect(document.body).not.toHaveTextContent('protected@example.test');
  });

  it('test_loading_then_mailbox_empty_state_is_explicit', async () => {
    let release!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve) => { release = resolve; })));
    renderPage();

    expect(screen.getByRole('status')).toHaveTextContent('Загружаем почтовые подключения');
    await waitFor(() => expect(release).toBeTypeOf('function'));
    release(Response.json(collection([])));
    expect(await screen.findByText('Почтовые подключения не настроены')).toBeVisible();
  });

  it('test_stale_snapshot_remains_visible_while_live_inspection_is_pending', async () => {
    let releaseInspection!: (response: Response) => void;
    const item = connection();
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(Response.json(collection([item]))).mockImplementationOnce(() => new Promise<Response>((resolve) => { releaseInspection = resolve; })));
    renderPage();

    expect(await screen.findByText(/показано последнее безопасное состояние/i)).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Yandex Mail' })).toBeVisible();
    releaseInspection(Response.json(envelope(item)));
    await waitFor(() => expect(screen.queryByText(/показано последнее безопасное состояние/i)).not.toBeInTheDocument());
  });

  it('test_partial_inspection_failure_keeps_only_safe_persisted_fields', async () => {
    const protectedValues = ['Quarterly acquisition', 'person@example.test', 'proposal-secret.docx', 'INBOX/private', 'socket ECONNRESET at provider', '{"raw":"provider payload"}'];
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(Response.json(collection([connection()]))).mockResolvedValueOnce(structuredError(503, 'spine.provider.unavailable', {
      subject: protectedValues[0], address: protectedValues[1], filename: protectedValues[2], label_name: protectedValues[3], raw_error: protectedValues[4], provider_payload: protectedValues[5],
    })));
    renderPage();

    expect(await screen.findByText(/часть актуальных проверок недоступна/i)).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Yandex Mail' })).toBeVisible();
    for (const protectedValue of protectedValues) expect(document.body).not.toHaveTextContent(protectedValue);
  });

  it.each([
    ['partial', 'healthy', 'Получены частичные данные', 'Не считайте выборку полной'],
    ['stale', 'healthy', 'Данные устарели', 'Сверьте время последней полной синхронизации'],
    ['complete', 'degraded', 'Работает с ограничениями', 'Обновите статус позднее или следуйте операционному регламенту'],
    ['revoked', 'revoked', 'Доступ отозван', 'запросите новое одобрение подключения'],
    ['reconciliation_required', 'reconciliation_required', 'Требуется сверка', 'Дождитесь безопасной полной сверки'],
  ] as const)('test_%s_sync_state_has_distinct_accessible_operator_guidance', async (state, health, expected, guidance) => {
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(collection([connection({ health, mailbox_sync: { state, last_complete_at: null, lag_seconds: null } })]))));
    renderPage();

    await userEvent.setup().click(await screen.findByRole('button', { name: /Открыть Yandex Mail/ }));
    const diagnostic = screen.getByRole('status', { name: expected });
    expect(diagnostic).toHaveTextContent(guidance);
    expect(document.body).not.toHaveTextContent('oauth-token-secret');
    expect(document.body).not.toHaveTextContent('INBOX/private');
  });

  it('test_synthetic_status_is_identified_as_ui_qualification_not_provider_pass', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(collection([connection()]))));
    renderPage();

    await userEvent.setup().click(await screen.findByRole('button', { name: /Открыть Yandex Mail/ }));
    expect(screen.getByText(/квалифицирует только отображение безопасного API-контракта/i)).toBeVisible();
    expect(screen.getByText(/не подтверждает live-доступ или работоспособность Gmail и Yandex Mail/i)).toBeVisible();
    expect(document.body).not.toHaveTextContent(/provider pass/i);
  });

  it('test_sync_metrics_are_omitted_when_mailbox_contract_does_not_supply_them', async () => {
    const item = connection({ capabilities: [], cursor_lag_seconds: 75, last_successful_reconciliation_at: '2026-11-03T16:00:00Z', mailbox_sync: { state: 'complete', last_complete_at: null, lag_seconds: null } });
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(collection([item]))));
    renderPage();
    expect(await screen.findByText('Yandex Mail')).toBeVisible();
    expect(screen.queryByText('2 мин')).not.toBeInTheDocument();

    await userEvent.setup().click(await screen.findByRole('button', { name: /Открыть Yandex Mail/ }));
    expect(screen.queryByText('Последняя полная синхронизация')).not.toBeInTheDocument();
    expect(screen.queryByText('Отставание')).not.toBeInTheDocument();
    expect(screen.queryByText('2 мин')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Возможности подключения' })).toBeVisible();
    expect(screen.queryByText('mailbox_enumeration')).not.toBeInTheDocument();
  });

  it('test_permission_denied_state_never_renders_provider_or_mailbox_payload', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>().mockResolvedValue(structuredError(403, 'spine.authorization.denied', { provider_detail: 'person@example.test · proposal-secret.docx' })));
    renderPage();

    expect(await screen.findByText('Доступ запрещён')).toBeVisible();
    expect(document.body).not.toHaveTextContent('person@example.test');
    expect(document.body).not.toHaveTextContent('proposal-secret.docx');
  });

  it('test_invalid_approval_submits_only_server_references_and_safe_version_fields', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(Response.json(collection([]))).mockResolvedValueOnce(structuredError(404, 'spine.source_connections.not_found', { credential: 'oauth-token-secret' }));
    vi.stubGlobal('fetch', fetch);
    renderPage();
    await screen.findByText('Почтовые подключения не настроены');

    await openAndSubmitConfiguration();
    expect(await screen.findByRole('alert')).toHaveTextContent('Approval недействителен');
    const request = fetch.mock.calls[1]?.[0] as Request;
    const body = JSON.parse(await request.clone().text());
    expect(body).toEqual({ approval_id: id(3), connection_id: id(2), expected_version: 0, expected_predecessor_version_id: null });
    expect(request.headers.get('Idempotency-Key')).toBeTruthy();
    expect(document.body).not.toHaveTextContent('oauth-token-secret');
    expect(screen.queryByLabelText(/OAuth|токен|пароль|ящик|папк/i)).not.toBeInTheDocument();
  });

  it('test_successor_configuration_posts_exact_current_version_fence', async () => {
    const item = connection({ version: 4, connection_version_id: id(44) });
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(Response.json(collection([item]))).mockResolvedValueOnce(Response.json(envelope(item))).mockResolvedValueOnce(structuredError(409, 'spine.persistence.optimistic_conflict'));
    vi.stubGlobal('fetch', fetch);
    renderPage();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /Открыть Yandex Mail/ }));
    await user.click(screen.getByRole('button', { name: 'Изменить конфигурацию' }));
    await user.clear(screen.getByLabelText(/Approval ID/));
    await user.type(screen.getByLabelText(/Approval ID/), id(33));
    await user.click(screen.getByRole('button', { name: 'Сохранить ссылки' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Версия подключения уже изменилась');
    const body = JSON.parse(await (fetch.mock.calls[2]?.[0] as Request).clone().text());
    expect(body).toMatchObject({ expected_version: 4, expected_predecessor_version_id: id(44), connection_id: id(2), approval_id: id(33) });
  });

  it('test_conflict_refresh_updates_version_fence_before_retry', async () => {
    const stale = connection({ version: 4, connection_version_id: id(44) });
    const current = connection({ version: 5, connection_version_id: id(45) });
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(collection([stale])))
      .mockResolvedValueOnce(Response.json(envelope(stale)))
      .mockResolvedValueOnce(structuredError(409, 'spine.persistence.optimistic_conflict'))
      .mockResolvedValueOnce(Response.json(collection([current])))
      .mockResolvedValueOnce(Response.json(envelope(current)))
      .mockResolvedValueOnce(Response.json(commandResult(current, false), { status: 201 }))
      .mockResolvedValueOnce(Response.json(collection([current])))
      .mockResolvedValueOnce(Response.json(envelope(current)));
    vi.stubGlobal('fetch', fetch);
    renderPage();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /Открыть Yandex Mail/ }));
    await user.click(screen.getByRole('button', { name: 'Изменить конфигурацию' }));
    await user.click(screen.getByRole('button', { name: 'Сохранить ссылки' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Версия подключения уже изменилась');
    await user.click(screen.getByRole('button', { name: 'Обновить статус' }));
    expect(await screen.findByText('Текущая версия:')).toHaveTextContent('5');
    await user.click(screen.getByRole('button', { name: 'Сохранить ссылки' }));

    expect(await screen.findByText('Одобренная конфигурация сохранена.')).toBeVisible();
    const retryBody = JSON.parse(await (fetch.mock.calls[5]?.[0] as Request).clone().text());
    expect(retryBody).toMatchObject({ expected_version: 5, expected_predecessor_version_id: id(45) });
    expect((fetch.mock.calls[2]?.[0] as Request).headers.get('Idempotency-Key')).not.toBe((fetch.mock.calls[5]?.[0] as Request).headers.get('Idempotency-Key'));
  });

  it('test_fresh_success_reports_non_replayed_result', async () => {
    const created = connection({ approval_id: id(3), connection_id: id(2) });
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(collection([])))
      .mockResolvedValueOnce(Response.json(commandResult(created, false), { status: 201 }))
      .mockResolvedValueOnce(Response.json(collection([created])))
      .mockResolvedValueOnce(Response.json(envelope(created)));
    vi.stubGlobal('fetch', fetch);
    renderPage();
    await screen.findByText('Почтовые подключения не настроены');

    await openAndSubmitConfiguration();
    expect(await screen.findByText('Одобренная конфигурация сохранена.')).toBeVisible();
    expect(screen.queryByText(/Повтор команды распознан/)).not.toBeInTheDocument();
  });

  it('test_retry_after_lost_response_reuses_idempotency_key_and_reports_replay', async () => {
    const created = connection({ approval_id: id(3), connection_id: id(2) });
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(collection([])))
      .mockResolvedValueOnce(structuredError(503, 'spine.provider.unavailable'))
      .mockResolvedValueOnce(Response.json(commandResult(created, true), { status: 201 }))
      .mockResolvedValueOnce(Response.json(collection([created])))
      .mockResolvedValueOnce(Response.json(envelope(created)));
    vi.stubGlobal('fetch', fetch);
    renderPage();
    await screen.findByText('Почтовые подключения не настроены');

    const user = userEvent.setup();
    await user.click(screen.getAllByRole('button', { name: 'Настроить по ссылкам' })[0]!);
    await user.type(screen.getByLabelText(/Approval ID/), id(3));
    await user.type(screen.getByLabelText(/Connection ID/), id(2));
    await user.click(screen.getByRole('button', { name: 'Сохранить ссылки' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Повтор отправит ту же идемпотентную команду');
    await user.click(screen.getByRole('button', { name: 'Сохранить ссылки' }));

    expect(await screen.findByText(/Повтор команды распознан/)).toBeVisible();
    const firstKey = (fetch.mock.calls[1]?.[0] as Request).headers.get('Idempotency-Key');
    const retryKey = (fetch.mock.calls[2]?.[0] as Request).headers.get('Idempotency-Key');
    expect(retryKey).toBe(firstKey);
  });

  it('test_workspace_switch_removes_previous_scope_state_and_uses_new_headers', async () => {
    const first = connection({ approved_resource_scope_references: ['scope:first-workspace'] });
    const second = connection({ connection_id: id(82), connection_version_id: id(83), approved_resource_scope_references: ['scope:second-workspace'] });
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(Response.json(collection([first], scope))).mockResolvedValueOnce(Response.json(envelope(first, scope))).mockResolvedValueOnce(Response.json(collection([second], otherScope))).mockResolvedValueOnce(Response.json(envelope(second, otherScope)));
    vi.stubGlobal('fetch', fetch);
    const view = renderPage();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /Открыть Yandex Mail/ }));
    expect(screen.getByText('scope:first-workspace')).toBeVisible();
    view.rerender(<TestPage queryClient={view.queryClient} scopeValue={otherScope} />);
    expect(await screen.findByRole('button', { name: /Открыть Yandex Mail/ })).toBeVisible();
    expect(screen.queryByText('scope:first-workspace')).not.toBeInTheDocument();
    expect((fetch.mock.calls[2]?.[0] as Request).headers.get('X-Workspace-ID')).toBe(otherScope.workspaceId);
  });

  it('test_collection_follows_opaque_next_cursor', async () => {
    const first = connection();
    const second = connection({ connection_id: id(92), connection_version_id: id(93), provider_kind: 'google' });
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(Response.json({ ...collection([first]), next_cursor: 'signed-next' })).mockResolvedValueOnce(Response.json(envelope(first))).mockResolvedValueOnce(Response.json(collection([second]))).mockResolvedValueOnce(Response.json(envelope(second)));
    vi.stubGlobal('fetch', fetch);
    renderPage();

    await userEvent.setup().click(await screen.findByRole('button', { name: 'Показать ещё' }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));
    expect(new URL((fetch.mock.calls[2]?.[0] as Request).url).searchParams.get('cursor')).toBe('signed-next');
    expect(screen.getByRole('heading', { name: 'Gmail' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Yandex Mail' })).toBeVisible();
  });
});

function renderPage(scopeValue: Scope = scope) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const view = render(<TestPage queryClient={queryClient} scopeValue={scopeValue} />);
  return { ...view, queryClient };
}

function TestPage({ queryClient, scopeValue }: { queryClient: QueryClient; scopeValue: Scope }) {
  return <MemoryRouter initialEntries={['/settings/connections']}><LocationProbe /><QueryClientProvider client={queryClient}><AuthProvider value={{ status: 'authenticated', role: 'administrator', getBearerCredential: () => 'test-token' }}><ScopeContext.Provider value={{ scope: scopeValue, queryKey: (resource) => ['spine', resource, scopeValue.workspaceId, scopeValue.environmentId] }}><ProviderConnectionsPage /></ScopeContext.Provider></AuthProvider></QueryClientProvider></MemoryRouter>;
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="route-location" hidden>{location.search}</span>;
}

async function openAndSubmitConfiguration() {
  const user = userEvent.setup();
  await user.click(screen.getAllByRole('button', { name: 'Настроить по ссылкам' })[0]!);
  await user.type(screen.getByLabelText(/Approval ID/), id(3));
  await user.type(screen.getByLabelText(/Connection ID/), id(2));
  await user.click(screen.getByRole('button', { name: 'Сохранить ссылки' }));
}

function collection(items: ReturnType<typeof connection>[], value: Scope = scope) { return { schema_version: 1, workspace_id: value.workspaceId, environment_id: value.environmentId, trace_id: id(50), items, limit: 100, next_cursor: null }; }
function envelope(item: ReturnType<typeof connection>, value: Scope = scope) { return { schema_version: 1, workspace_id: value.workspaceId, environment_id: value.environmentId, trace_id: id(50), data: item }; }
function commandResult(item: ReturnType<typeof connection>, replay: boolean) { return { schema_version: 1, workspace_id: scope.workspaceId, environment_id: scope.environmentId, trace_id: id(50), command_id: id(60), status: 'completed', replay, result: item }; }
function structuredError(status: number, code: string, protectedFields: Record<string, unknown> = {}) { return Response.json({ schema_version: 1, code, safe_message: 'The request could not be completed.', retryability: status >= 500 ? 'retryable' : 'never', trace_id: id(50), ...protectedFields }, { status }); }
function id(suffix: number) { return `00000000-0000-4000-8000-${String(suffix).padStart(12, '0')}`; }
function connection(overrides: Record<string, unknown> = {}) { return { schema_version: 2, connection_id: id(2), connection_version_id: id(4), approval_id: id(3), version: 1, predecessor_version_id: null, provider_kind: 'yandex_mail', status: 'active', authorization_generation: 2, configuration_generation: 3, approved_resource_scope_references: ['mailbox-scope:yandex:approved'], permitted_operations: ['enumerate_mailbox', 'read_message_metadata', 'synchronize'], capabilities: [{ kind: 'mailbox_enumeration', status: 'supported', qualification_reference: id(8) }, { kind: 'raw_message_read', status: 'unqualified', qualification_reference: null }], health: 'healthy', diagnostic_codes: ['connection_ready'], observed_at: '2026-11-03T17:00:00Z', cursor_lag_seconds: 75, last_successful_reconciliation_at: '2026-11-03T16:00:00Z', mailbox_sync: { state: 'complete', last_complete_at: '2026-11-03T16:00:00Z', lag_seconds: 75 }, ...overrides }; }
