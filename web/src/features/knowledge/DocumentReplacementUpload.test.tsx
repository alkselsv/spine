import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { createScopedApiClient } from '../../api/client';
import { DocumentReplacementUpload } from './DocumentReplacementUpload';

const scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const sourceId = '30000000-0000-4000-8000-000000000001';
const revisionId = '40000000-0000-4000-8000-000000000001';
const runId = '50000000-0000-4000-8000-000000000001';
const itemId = '60000000-0000-4000-8000-000000000001';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('document replacement upload', () => {
  it('test_replacement_upload_sends_bytes_and_expected_revision_without_storage_reference', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json(uploadResponse(), { status: 202 }),
    );
    const refresh = vi.fn();
    renderUpload(fetch, refresh);
    const user = userEvent.setup();
    const file = new File(['updated source'], 'handbook.md', { type: 'text/markdown' });

    await user.upload(screen.getByLabelText('Файл новой ревизии'), file);
    await user.click(screen.getByRole('button', { name: 'Заменить документ' }));

    expect(await screen.findByRole('status')).toHaveTextContent('Новая ревизия принята и обработана');
    expect(refresh).toHaveBeenCalledTimes(1);
    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe(`/api/v1/documents/${sourceId}/replacement-upload`);
    expect(request.headers.get('If-Match')).toBe(revisionId);
    expect(request.headers.get('Idempotency-Key')).toBeTruthy();
    expect(request.headers.get('X-Filename')).toBe('handbook.md');
    expect(request.headers.get('Content-Type')).toBe('text/markdown');
    expect(await request.text()).toBe('updated source');
    expect(globalThis.document.body).not.toHaveTextContent('storage_request_reference');
  });

  it('test_partial_processing_reports_accepted_revision_without_calling_it_failed', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json(uploadResponse({ status: 'partial', processing: 'partial' }), { status: 202 }),
    );
    renderUpload(fetch, vi.fn());
    const user = userEvent.setup();

    await user.upload(screen.getByLabelText('Файл новой ревизии'), new File(['text'], 'handbook.txt'));
    await user.click(screen.getByRole('button', { name: 'Заменить документ' }));

    expect(await screen.findByRole('status')).toHaveTextContent('обработка завершилась частично');
  });

  it('test_denied_replacement_does_not_retry_or_expose_protected_response_text', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json(
        {
          code: 'spine.authorization.denied',
          retryability: 'never',
          safe_message: 'Доступ запрещён.',
          schema_version: 1,
          trace_id: runId,
          protected_text: 'must not appear',
        },
        { status: 403 },
      ),
    );
    renderUpload(fetch, vi.fn());
    const user = userEvent.setup();

    await user.upload(screen.getByLabelText('Файл новой ревизии'), new File(['text'], 'handbook.txt'));
    await user.click(screen.getByRole('button', { name: 'Заменить документ' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Доступ запрещён');
    expect(screen.getByRole('button', { name: 'Повторить команду' })).toBeDisabled();
    expect(globalThis.document.body).not.toHaveTextContent('must not appear');
  });

  it('test_retryable_replacement_replays_the_same_bytes_and_revision_command', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json({
        code: 'spine.client.api_unavailable',
        retryability: 'same_command',
        safe_message: 'Сервис временно недоступен.',
        schema_version: 1,
        trace_id: runId,
      }, { status: 503 }))
      .mockResolvedValueOnce(Response.json(uploadResponse(), { status: 202 }));
    renderUpload(fetch, vi.fn());
    const user = userEvent.setup();

    await user.upload(screen.getByLabelText('Файл новой ревизии'), new File(['same bytes'], 'handbook.md', { type: 'text/markdown' }));
    await user.click(screen.getByRole('button', { name: 'Заменить документ' }));
    await user.click(await screen.findByRole('button', { name: 'Повторить команду' }));

    expect(await screen.findByRole('status')).toHaveTextContent('Новая ревизия принята и обработана');
    expect(fetch).toHaveBeenCalledTimes(2);
    const first = fetch.mock.calls[0]?.[0] as Request;
    const retry = fetch.mock.calls[1]?.[0] as Request;
    expect(retry.headers.get('Idempotency-Key')).toBe(first.headers.get('Idempotency-Key'));
    expect(retry.headers.get('If-Match')).toBe(first.headers.get('If-Match'));
  });

  it('test_stale_revision_conflict_is_not_offered_as_retryable', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json({
        code: 'spine.persistence.optimistic_conflict',
        retryability: 'never',
        safe_message: 'Ревизия изменилась. Обновите документ.',
        schema_version: 1,
        trace_id: runId,
      }, { status: 409 }),
    );
    renderUpload(fetch, vi.fn());
    const user = userEvent.setup();

    await user.upload(screen.getByLabelText('Файл новой ревизии'), new File(['text'], 'handbook.txt'));
    await user.click(screen.getByRole('button', { name: 'Заменить документ' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Команда конфликтует с текущим состоянием');
    expect(screen.getByRole('button', { name: 'Повторить команду' })).toBeDisabled();
  });
});

function renderUpload(fetch: typeof globalThis.fetch, onRefresh: () => void) {
  const client = createScopedApiClient({
    baseUrl: 'https://spine.test',
    fetch,
    getBearerCredential: () => 'test-token',
    scope,
  });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <DocumentReplacementUpload
        client={client}
        currentRevisionId={revisionId}
        onRefresh={onRefresh}
        queryKey={(resource) => ['spine', resource, scope.workspaceId, scope.environmentId]}
        scope={scope}
        sourceObjectId={sourceId}
      />
    </QueryClientProvider>,
  );
}

function uploadResponse(overrides: Record<string, unknown> = {}) {
  return {
    acceptance: 'accepted',
    binding_id: '70000000-0000-4000-8000-000000000001',
    failure_ids: [],
    item_id: itemId,
    processing: 'succeeded',
    processing_receipt_id: null,
    replay: false,
    run_id: runId,
    schema_version: 1,
    source_object_id: sourceId,
    source_revision_id: '80000000-0000-4000-8000-000000000001',
    status: 'succeeded',
    status_url: `https://spine.test/api/v1/ingestion/runs/${runId}/items/${itemId}`,
    terminal: true,
    warning_ids: [],
    ...overrides,
  };
}
