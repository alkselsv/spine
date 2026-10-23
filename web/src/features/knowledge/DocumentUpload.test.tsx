import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { createScopedApiClient } from '../../api/client';
import { ScopeContext, type Scope } from '../../app/scope';
import { DocumentUpload } from './DocumentUpload';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const runId = '30000000-0000-4000-8000-000000000001';
const traceId = '50000000-0000-4000-8000-000000000001';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('document upload entry point', () => {
  it('test_supported_docx_selection_opens_upload_review', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    renderUpload(fetch);
    const user = userEvent.setup();

    await user.upload(
      screen.getByLabelText('Выбрать файлы'),
      new File(
        ['synthetic docx fixture'],
        'handbook.docx',
        { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' },
      ),
    );

    expect(await screen.findByRole('region', { name: 'Проверка перед отправкой' })).toBeVisible();
    expect(screen.getByText('handbook.docx')).toBeVisible();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('test_invalid_selection_shows_russian_feedback_without_network_submission', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    renderUpload(fetch);

    fireEvent.change(screen.getByLabelText('Выбрать файлы'), {
      target: { files: [new File(['synthetic'], 'unsupported.exe', { type: 'application/x-msdownload' })] },
    });

    expect(await screen.findByRole('alert')).toHaveTextContent('файл неподдерживаемого типа');
    expect(screen.getByRole('alert')).not.toHaveTextContent('unsupported.exe');
    expect(fetch).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Загрузить документы' })).not.toBeInTheDocument();
  });

  it('test_mixed_outcomes_keep_success_and_retry_only_the_retryable_item_with_same_key', async () => {
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(uploadResponse({
        items: [
          item({ input_identity: 'file-1', status: 'succeeded', processing: 'succeeded', terminal: true }),
          item({ input_identity: 'file-2', status: 'failed', processing: 'failed', retryable: true, terminal: true }),
        ],
        status: 'partial',
        terminal: true,
      }), { status: 202 }))
      .mockResolvedValueOnce(Response.json(uploadResponse({
        replay: true,
        items: [
          item({ input_identity: 'file-1', status: 'succeeded', processing: 'succeeded', terminal: true }),
          item({ input_identity: 'file-2', status: 'failed', processing: 'failed', retryable: true, terminal: true }),
        ],
        status: 'partial',
        terminal: true,
      }), { status: 202 }));
    renderUpload(fetch);
    const user = userEvent.setup();
    await user.upload(screen.getByLabelText('Выбрать файлы'), [
      new File(['ok'], 'ok.txt', { type: 'text/plain' }),
      new File(['bad'], 'bad.md', { type: 'text/markdown' }),
    ]);
    await user.click(screen.getByRole('button', { name: 'Загрузить документы' }));

    expect(await screen.findByText('Готов')).toBeVisible();
    expect(screen.getAllByText('Ошибка обработки').length).toBeGreaterThan(0);
    expect(screen.getByText('Можно безопасно повторить эту команду.')).toBeVisible();

    await user.click(screen.getByRole('button', { name: 'Повторить' }));
    expect(await screen.findByText('Показан результат повтора той же команды.')).toBeVisible();
    expect(fetch).toHaveBeenCalledTimes(2);
    const firstRequest = fetch.mock.calls[0]?.[0] as Request;
    const secondRequest = fetch.mock.calls[1]?.[0] as Request;
    expect(firstRequest.headers.get('Idempotency-Key')).toBe(secondRequest.headers.get('Idempotency-Key'));
  });

  it('test_non_terminal_acceptance_links_run_and_cancel_stops_polling_without_server_cancellation', async () => {
    let statusRequest: Request | undefined;
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(
        Response.json(
          uploadResponse({
            items: [item({ input_identity: 'file-1', acceptance: 'accepted', processing: 'not_started' })],
          }),
          { status: 202 },
        ),
      )
      .mockImplementationOnce(
        (request) =>
          new Promise((_resolve, reject) => {
            statusRequest = request as Request;
            statusRequest.signal.addEventListener('abort', () => {
              const error = new DOMException('The operation was aborted.', 'AbortError');
              reject(error);
            });
          }),
      );
    renderUpload(fetch);
    const user = userEvent.setup();
    await user.upload(screen.getByLabelText('Выбрать файлы'), [new File(['ok'], 'ok.txt', { type: 'text/plain' })]);
    await user.click(screen.getByRole('button', { name: 'Загрузить документы' }));

    expect(await screen.findByText('Принят сервером')).toBeVisible();
    expect(await screen.findByRole('link', { name: 'Открыть запуск обработки' })).toHaveAttribute(
      'href',
      `/runs/${runId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`,
    );
    await user.click(screen.getByRole('button', { name: 'Остановить в браузере' }));

    expect(
      screen.getByText('Браузер остановил передачу или наблюдение; серверная обработка не отменена.', { selector: 'p' }),
    ).toBeVisible();
    expect(statusRequest?.signal.aborted).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(screen.queryByText('Серверная отмена')).not.toBeInTheDocument();
  });

  it('test_safe_retryable_api_error_exposes_trace_and_same_command_next_action', async () => {
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(
        Response.json(
          {
            code: 'spine.client.api_unavailable',
            retryability: 'same_command',
            safe_message: 'Сервис временно недоступен.',
            schema_version: 1,
            trace_id: traceId,
            provider_detail: 'private provider failure',
          },
          { status: 503 },
        ),
      )
      .mockResolvedValueOnce(
        Response.json(
          uploadResponse({
            items: [item({ status: 'succeeded', processing: 'succeeded', terminal: true })],
            status: 'succeeded',
            terminal: true,
          }),
          { status: 202 },
        ),
      );
    renderUpload(fetch);
    const user = userEvent.setup();
    await user.upload(screen.getByLabelText('Выбрать файлы'), [new File(['ok'], 'ok.txt', { type: 'text/plain' })]);
    await user.click(screen.getByRole('button', { name: 'Загрузить документы' }));

    expect(await screen.findByText(`Код обращения: ${traceId}`)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Повторить ту же команду' })).toBeVisible();
    expect(document.body).not.toHaveTextContent('private provider failure');
    await user.click(screen.getByRole('button', { name: 'Повторить ту же команду' }));
    expect((await screen.findAllByText('Готов')).length).toBeGreaterThan(0);
  });
});

function renderUpload(fetch: typeof globalThis.fetch) {
  const client = createScopedApiClient({
    baseUrl: window.location.origin,
    fetch,
    getBearerCredential: () => 'test-token',
    scope,
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ScopeContext.Provider value={{ scope, queryKey: (resource) => ['spine', resource, scope.workspaceId, scope.environmentId] }}>
          <DocumentUpload client={client} scopeSearch={`workspace=${scope.workspaceId}&environment=${scope.environmentId}`} />
        </ScopeContext.Provider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

function uploadResponse(
  overrides: Partial<components['schemas']['BatchIngestionUploadResponse']> = {},
): components['schemas']['BatchIngestionUploadResponse'] {
  return {
    items: [],
    replay: false,
    run_id: runId,
    run_state: 'running',
    schema_version: 1,
    status: 'processing',
    status_url: `${window.location.origin}/api/v1/ingestion/runs/${runId}`,
    terminal: false,
    ...overrides,
  };
}

function item(
  overrides: Partial<components['schemas']['BatchIngestionItemResponse']> = {},
): components['schemas']['BatchIngestionItemResponse'] {
  return {
    acceptance: 'accepted' as const,
    attempt: 1,
    coverage: null,
    failure_ids: [],
    failures: [],
    input_identity: 'file-1',
    input_ordinal: 1,
    item_id: '60000000-0000-4000-8000-000000000001',
    item_state: 'accepted',
    processing: 'not_started' as const,
    processing_receipt: null,
    retryable: false,
    source_object_id: null,
    source_revision_id: null,
    status: 'non_terminal' as const,
    terminal: false,
    warning_ids: [],
    warnings: [],
    ...overrides,
  };
}
