import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createMemoryRouter, RouterProvider } from 'react-router';

import { AuthProvider } from '../../app/auth';
import { ScopeContext, type Scope } from '../../app/scope';
import { TranscriptionStatusPage } from './TranscriptionStatusPage';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const jobId = '30000000-0000-4000-8000-000000000001';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('transcription status', () => {
  it('test_completed_status_renders_safe_processing_lineage_without_artifact_metadata', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      schema_version: 1,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      data: {
        schema_version: 'transcription-status:v1',
        status: 'completed',
        job_id: jobId,
        source_object_id: '40000000-0000-4000-8000-000000000001',
        source_revision_id: '50000000-0000-4000-8000-000000000001',
        processing_profile_reference: 'meeting-audio:v2',
        adapter_reference: 'asr-primary:v3',
        model_config_reference: 'whisper-large-v3:ru',
        created_at: '2026-11-04T08:00:00Z',
        completed_at: '2026-11-04T08:02:00Z',
        terminal_code: 'completed',
        retryable: false,
        cancellation_requested: false,
      },
    })));

    renderPage();

    expect(await screen.findByRole('heading', { name: 'Статус расшифровки' })).toBeVisible();
    expect(screen.getByText('Расшифровка завершена')).toBeVisible();
    expect(screen.getByText('meeting-audio:v2')).toBeVisible();
    expect(document.body).not.toHaveTextContent('artifact');
    expect(document.body).not.toHaveTextContent('segment');
  });

  it.each([
    ['partial', 'Получен частичный результат'],
    ['failed', 'Расшифровка завершилась ошибкой'],
    ['cancelled', 'Расшифровка отменена'],
  ] as const)('test_terminal_and_partial_statuses_render_safe_state_%s', async (status, label) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      schema_version: 1,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      data: {
        schema_version: 'transcription-status:v1',
        status,
        job_id: jobId,
        source_object_id: '40000000-0000-4000-8000-000000000001',
        source_revision_id: '50000000-0000-4000-8000-000000000001',
        processing_profile_reference: 'meeting-audio:v2',
        adapter_reference: 'asr-primary:v3',
        model_config_reference: 'whisper-large-v3:ru',
        created_at: '2026-11-04T08:00:00Z',
        completed_at: '2026-11-04T08:02:00Z',
        terminal_code: status,
        retryable: status === 'failed',
        cancellation_requested: status === 'cancelled',
      },
    })));

    renderPage();

    expect(await screen.findByText(label)).toBeVisible();
    if (status === 'partial') {
      expect(screen.getByRole('region', { name: 'Показаны не все данные' })).toBeVisible();
    }
  });

  it('test_loading_and_permission_denied_states_are_explicit', () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockImplementation(() => new Promise(() => undefined)));
    const first = renderPage();
    expect(screen.getByRole('region', { name: 'Загрузка' })).toBeVisible();
    first.unmount();

    renderPage(false);
    expect(screen.getByRole('region', { name: 'Нет доступа к данным' })).toBeVisible();
  });

  it('test_api_outage_renders_degraded_state_without_protected_metadata', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockRejectedValue(new TypeError('provider secret')));

    renderPage();

    expect(await screen.findByRole('alert', {}, { timeout: 2_500 })).toHaveTextContent('Сервис временно недоступен');
    expect(document.body).not.toHaveTextContent('provider secret');
    expect(document.body).not.toHaveTextContent(jobId);
  });

  it('test_unavailable_status_hides_protected_metadata_and_supports_refresh', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      schema_version: 1,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      data: { schema_version: 'transcription-status:v1', status: 'unavailable' },
    }));
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    renderPage();

    expect(await screen.findByText('Статус недоступен')).toBeVisible();
    expect(document.body).not.toHaveTextContent(jobId);
    await user.click(screen.getByRole('button', { name: 'Обновить статус' }));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

function renderPage(withCredential = true) {
  const router = createMemoryRouter(
    [{ path: '/transcriptions/:jobId', element: <TranscriptionStatusPage /> }],
    { initialEntries: [`/transcriptions/${jobId}`] },
  );
  return render(
    <AuthProvider value={{
      status: 'authenticated',
      role: 'administrator',
      identityVersion: 'principal-a',
      ...(withCredential ? { getBearerCredential: () => 'test-token' } : {}),
    }}>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ScopeContext.Provider value={{
          scope,
          queryKey: (resource) => ['spine', resource, scope.workspaceId, scope.environmentId],
        }}>
          <RouterProvider router={router} />
        </ScopeContext.Provider>
      </QueryClientProvider>
    </AuthProvider>,
  );
}
