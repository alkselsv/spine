import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createMemoryRouter, RouterProvider } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { AuthProvider } from '../../app/auth';
import { ScopeContext, type Scope } from '../../app/scope';
import {
  IngestionRunPage,
  parseRunQuery,
  updateRunQuery,
} from './IngestionRunPage';
import { mapIngestionItemState } from './run-state';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const runId = '30000000-0000-4000-8000-000000000001';
const firstItemId = '30000000-0000-4000-8000-000000000002';
const secondItemId = '30000000-0000-4000-8000-000000000003';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Knowledge ingestion run view', () => {
  it('test_run_query_restores_both_history_cursors_without_accepting_oversized_values', () => {
    expect(
      parseRunQuery(`?cursor=run-cursor&processing_cursor=processing-cursor`),
    ).toEqual({ cursor: 'run-cursor', processingCursor: 'processing-cursor' });
    expect(parseRunQuery(`?cursor=${'x'.repeat(769)}`)).toEqual({});
    expect(
      updateRunQuery('?workspace=workspace-a&cursor=old', {
        cursor: 'new-run-cursor',
        processingCursor: 'new-processing-cursor',
      }),
    ).toBe('workspace=workspace-a&cursor=new-run-cursor&processing_cursor=new-processing-cursor');
  });

  it('test_run_view_renders_scope_phases_diagnostics_receipt_and_history_links', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === '/api/v1/ingestion/runs') return Response.json(runHistoryCollection());
      if (path === '/api/v1/documents/processing-history') {
        return Response.json(processingHistoryCollection());
      }
      return Response.json(runStatus());
    });
    vi.stubGlobal('fetch', fetch);

    const { router } = renderRun(
      `/runs/${runId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&cursor=restored-cursor`,
    );

    expect(await screen.findByRole('heading', { name: 'Запуск' })).toBeVisible();
    expect((await screen.findAllByText(runId)).length).toBeGreaterThan(0);
    const detail = screen.getByRole('heading', { name: 'Запуск' }).closest('section');
    expect(detail).not.toBeNull();
    const detailView = within(detail!);
    expect(detailView.getAllByText(runId).length).toBeGreaterThan(0);
    expect(detailView.getByText(scope.workspaceId)).toBeVisible();
    expect(detailView.getByText('Частично завершён')).toBeVisible();
    expect(detailView.getByText('Частичное')).toBeVisible();
    expect(detailView.getAllByText('Предупреждение: parser.warning (parse)').length).toBeGreaterThan(0);
    expect(detailView.getByText(/Ошибка: parser\.invalid \(parse\)/)).toBeVisible();
    expect(detailView.getByText(/Ошибка: parser\.final \(index\).*Повтор этой ошибки запрещён/)).toBeVisible();
    expect(detailView.getByText('Повтор операции разрешён')).toBeVisible();
    expect(detailView.getByText('Следующее действие: Повтор операции разрешён.')).toBeVisible();
    expect(detail).toHaveTextContent('Парсер: markdown-v2');
    expect(detail).toHaveTextContent('Квитанция: 60000000-0000-4000-8000-000000000001');
    expect(screen.getByRole('heading', { name: 'История запусков' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'История обработки файлов' })).toBeVisible();
    expect(screen.getAllByRole('link', { name: 'Открыть запуск' }).length).toBeGreaterThan(0);
    expect(detailView.getAllByRole('link', { name: 'Открыть запуск' })[0]).toHaveAttribute(
      'href',
      expect.stringContaining(`/runs/${runId}`),
    );

    const historyRequest = fetch.mock.calls
      .map(([input]) => new URL((input as Request).url))
      .find((url) => url.pathname === '/api/v1/ingestion/runs');
    expect(historyRequest?.searchParams.get('cursor')).toBe('restored-cursor');
    expect(router.state.location.search).toContain(`workspace=${scope.workspaceId}`);
  });

  it('test_retry_action_targets_only_retryable_failure_and_preserves_file_outcomes', async () => {
    const requests: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async (input) => {
      const request = input as Request;
      requests.push(request);
      const url = new URL(request.url);
      if (url.pathname === `/api/v1/ingestion/runs/${runId}`) {
        const response = runStatus();
        response.items[1] = makeBatchItem({
          acceptance: 'accepted',
          attempt: 2,
          coverage: 'failed',
          failures: [
            {
              attempt: 2,
              code: 'parser.invalid',
              failure_id: '60000000-0000-4000-8000-000000000002',
              retryable: true,
              stage: 'parse',
            },
            {
              attempt: 2,
              code: 'parser.final',
              failure_id: '60000000-0000-4000-8000-000000000004',
              retryable: false,
              stage: 'index',
            },
          ],
          item_id: secondItemId,
          input_identity: 'file-2',
          input_ordinal: 2,
          processing: 'failed',
          retryable: true,
          source_object_id: '30000000-0000-4000-8000-000000000010',
          source_revision_id: '40000000-0000-4000-8000-000000000010',
          status: 'failed',
        });
        return Response.json(response);
      }
      if (url.pathname === '/api/v1/ingestion/runs') return Response.json(runHistoryCollection());
      if (url.pathname === '/api/v1/documents/processing-history') return Response.json(processingHistoryCollection());
      return Response.json(processingCommandResponse(url.pathname.includes('/operations/')));
    });
    vi.stubGlobal('fetch', fetch);
    vi.stubGlobal('crypto', { randomUUID: () => 'run-retry-command-key' });

    renderRun(`/runs/${runId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`);
    const file = await screen.findByText('file-2');
    const fileItem = file.closest('li');
    expect(fileItem).not.toBeNull();
    expect(within(fileItem!).getAllByRole('button', { name: 'Повторить обработку файла' })).toHaveLength(1);
    const unaffectedFile = screen.getAllByText('file-1')[0]?.closest('li');
    expect(unaffectedFile).not.toBeNull();
    expect(within(unaffectedFile!).getByText('Частичный результат')).toBeVisible();
    await userEvent.setup().click(within(fileItem!).getByRole('button', { name: 'Повторить обработку файла' }));

    expect(await within(fileItem!).findByText('Обработка завершена')).toBeVisible();
    const post = requests.find((request) => request.method === 'POST');
    expect(post?.headers.get('Idempotency-Key')).toBe('run-retry-command-key');
    expect(await post?.json()).toMatchObject({
      expected_current_revision_id: '40000000-0000-4000-8000-000000000010',
      parent_failure_id: '60000000-0000-4000-8000-000000000002',
      source_object_id: '30000000-0000-4000-8000-000000000010',
      source_revision_id: '40000000-0000-4000-8000-000000000010',
    });
    expect(screen.getAllByText('file-1').length).toBeGreaterThan(0);
  });

  it('test_history_pagination_updates_and_restores_both_cursor_parameters', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async (input) => {
      const url = new URL((input as Request).url);
      if (url.pathname === '/api/v1/ingestion/runs') {
        const response = runHistoryCollection();
        response.next_cursor = 'next-run-cursor';
        return Response.json(response);
      }
      if (url.pathname === '/api/v1/documents/processing-history') {
        const response = processingHistoryCollection();
        response.next_cursor = 'next-processing-cursor';
        return Response.json(response);
      }
      return Response.json(runStatus());
    });
    vi.stubGlobal('fetch', fetch);

    const { router } = renderRun(
      `/runs?workspace=${scope.workspaceId}&environment=${scope.environmentId}&cursor=restored-run&processing_cursor=restored-processing`,
    );

    expect(await screen.findByRole('heading', { name: 'История запусков' })).toBeVisible();
    const nextPageButtons = await screen.findAllByRole('button', { name: 'Следующая страница' });
    expect(nextPageButtons).toHaveLength(2);
    await nextPageButtons[0]!.click();
    expect(router.state.location.search).toContain('cursor=next-run-cursor');
    expect(router.state.location.search).toContain('processing_cursor=restored-processing');
  });

  it('test_non_terminal_success_stays_out_of_ready_state_until_terminal', () => {
    expect(
      mapIngestionItemState(
        makeHistoryItem({
          coverage: 'complete',
          processing: 'succeeded',
          status: 'succeeded',
          terminal: false,
        }),
      ),
    ).not.toBe('ready');
    expect(
      mapIngestionItemState(
        makeHistoryItem({
          coverage: 'complete',
          processing: 'succeeded',
          status: 'succeeded',
          terminal: true,
        }),
      ),
    ).toBe('ready');
  });

  it.each([
    ['unsupported', 'unsupported'],
    ['cancelled', 'cancelled'],
    ['partial', 'partial'],
    ['failed', 'failed'],
    ['rejected', 'rejected'],
  ] as const)('test_%s_public_outcome_has_a_distinct_safe_label', (status, expected) => {
    expect(
      mapIngestionItemState(
        makeHistoryItem({
          coverage: 'complete',
          processing: status === 'rejected' ? 'failed' : status,
          status,
          terminal: true,
        }),
      ),
    ).toBe(expected);
  });

  it('test_projection_requested_never_reports_ready_even_after_terminal_success', () => {
    expect(
      mapIngestionItemState(
        makeHistoryItem({
          coverage: 'complete',
          item_state: 'projection_requested',
          processing: 'succeeded',
          status: 'succeeded',
          terminal: true,
        }),
      ),
    ).toBe('indexing');
  });

  it.each([
    ['pending', 'not_started', 'non_terminal', 'uploaded'],
    ['accepted', 'processing', 'non_terminal', 'parsing'],
  ] as const)('test_%s_phase_is_mapped_to_%s', (acceptance, processing, status, expected) => {
    expect(
      mapIngestionItemState(
        makeHistoryItem({ acceptance, coverage: 'complete', processing, status, terminal: false }),
      ),
    ).toBe(expected);
  });

  it('test_unavailable_history_keeps_diagnostics_safe_and_actionable', async () => {
    const processingHistory = processingHistoryCollection();
    processingHistory.items = [];
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockImplementation(async (input) => {
        const path = new URL((input as Request).url).pathname;
        if (path === '/api/v1/ingestion/runs') {
          return Response.json(
            {
              code: 'spine.dependency.unavailable',
              retryability: 'never',
              safe_message: 'Service unavailable.',
              schema_version: 1,
              trace_id: '50000000-0000-4000-8000-000000000001',
              provider_detail: 'secret provider stack must not leak',
            },
            { status: 503 },
          );
        }
        if (path === '/api/v1/documents/processing-history') {
          return Response.json(processingHistory);
        }
        return Response.json(runStatus());
      }),
    );

    renderRun(`/runs/${runId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`);

    expect(await screen.findByRole('alert')).toHaveTextContent('Сервис временно недоступен');
    expect(globalThis.document.body).not.toHaveTextContent('secret provider stack');
    expect(screen.getByRole('button', { name: 'Повторить загрузку' })).toBeVisible();
  });

  it('test_permission_denial_hides_cached_run_details_after_a_successful_read', async () => {
    let denyRunRead = false;
    const emptyRuns = runHistoryCollection();
    emptyRuns.items = [];
    const emptyProcessing = processingHistoryCollection();
    emptyProcessing.items = [];
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockImplementation(async (input) => {
        const path = new URL((input as Request).url).pathname;
        if (path === `/api/v1/ingestion/runs/${runId}`) {
          if (!denyRunRead) {
            denyRunRead = true;
            return Response.json(runStatus());
          }
          return Response.json(
            {
              code: 'spine.authorization.denied',
              retryability: 'never',
              safe_message: 'Authorization is denied.',
              schema_version: 1,
              trace_id: '50000000-0000-0000-0000-000000000001',
              provider_detail: 'protected provider detail must not leak',
            },
            { status: 403 },
          );
        }
        if (path === '/api/v1/ingestion/runs') return Response.json(emptyRuns);
        return Response.json(emptyProcessing);
      }),
    );

    const { queryClient } = renderRun(`/runs/${runId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`);
    expect(await screen.findByText(scope.workspaceId)).toBeVisible();

    await act(async () => {
      await queryClient.refetchQueries({
        queryKey: ['spine', 'ingestion-run', scope.workspaceId, scope.environmentId, runId],
        exact: true,
      });
    });

    expect(await screen.findByRole('alert')).toHaveTextContent('Доступ запрещён');
    expect(globalThis.document.body).not.toHaveTextContent('protected provider detail');
    expect(globalThis.document.body).not.toHaveTextContent(runId);
  });
});

function renderRun(initialEntry: string) {
  const router = createMemoryRouter(
    [
      { path: '/runs', element: <IngestionRunPage /> },
      { path: '/runs/:runId', element: <IngestionRunPage /> },
    ],
    { initialEntries: [initialEntry] },
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <AuthProvider
      value={{ status: 'authenticated', role: 'administrator', getBearerCredential: () => 'test-token' }}
    >
      <QueryClientProvider client={queryClient}>
        <ScopeContext.Provider value={{ scope, queryKey: (resource) => ['spine', resource, scope.workspaceId, scope.environmentId] }}>
          <RouterProvider router={router} />
        </ScopeContext.Provider>
      </QueryClientProvider>
    </AuthProvider>,
  );
  return { queryClient, router };
}

function runHistoryCollection(): components['schemas']['ApiCollectionEnvelope_IngestionRunHistoryResponse_'] {
  const item = makeHistoryItem({ input_identity: 'file-1' });
  return {
    environment_id: scope.environmentId,
    items: [
      {
        acceptance: 'accepted',
        items: [item],
        processing: 'succeeded',
        requested_at: '2026-01-02T03:04:05Z',
        run_id: runId,
        run_state: 'completed',
        status: 'partial',
        status_url: `/api/v1/ingestion/runs/${runId}`,
        terminal: true,
      },
    ],
    limit: 25,
    next_cursor: null,
    schema_version: 1,
    trace_id: '50000000-0000-0000-0000-000000000001',
    workspace_id: scope.workspaceId,
  };
}

function processingHistoryCollection(): components['schemas']['ApiCollectionEnvelope_DocumentProcessingHistoryResponse_'] {
  const item = makeHistoryItem({ input_identity: 'file-1' });
  return {
    environment_id: scope.environmentId,
    items: [item],
    limit: 25,
    next_cursor: null,
    schema_version: 1,
    trace_id: '50000000-0000-0000-0000-000000000001',
    workspace_id: scope.workspaceId,
  };
}

function runStatus(): components['schemas']['BatchIngestionStatusResponse'] {
  return {
    items: [
      makeBatchItem({ input_identity: 'file-1' }),
      makeBatchItem({
        acceptance: 'accepted',
        attempt: 2,
        coverage: 'failed',
        failures: [
          {
            attempt: 2,
            code: 'parser.invalid',
            failure_id: '60000000-0000-0000-0000-000000000002',
            retryable: true,
            stage: 'parse',
          },
          {
            attempt: 2,
            code: 'parser.final',
            failure_id: '60000000-0000-0000-0000-000000000004',
            retryable: false,
            stage: 'index',
          },
        ],
        input_identity: 'file-2',
        input_ordinal: 2,
        item_id: secondItemId,
        processing: 'failed',
        retryable: true,
        status: 'failed',
      }),
    ],
    run_id: runId,
    run_state: 'completed',
    schema_version: 1,
    status: 'partial',
    terminal: true,
  };
}

function processingCommandResponse(ready: boolean) {
  const operationId = '80000000-0000-4000-8000-000000000001';
  return {
    failure_ids: [],
    item_id: '80000000-0000-4000-8000-000000000002',
    next_action: ready ? 'none' : 'poll',
    operation_id: operationId,
    processing_receipt_id: ready ? '80000000-0000-4000-8000-000000000003' : null,
    replay: false,
    run_id: '90000000-0000-4000-8000-000000000001',
    schema_version: 1,
    source_object_id: '30000000-0000-4000-8000-000000000010',
    source_revision_id: '40000000-0000-4000-8000-000000000010',
    status: ready ? 'ready' : 'accepted',
    status_url: `/api/v1/documents/processing/operations/${operationId}`,
    trigger_kind: 'retry',
  };
}

function makeBatchItem(
  overrides: Partial<components['schemas']['BatchIngestionItemResponse']> = {},
): components['schemas']['BatchIngestionItemResponse'] {
  return {
    acceptance: 'accepted',
    attempt: 1,
    coverage: 'partial',
    failure_ids: [],
    failures: [],
    input_identity: 'file-1',
    input_ordinal: 1,
    item_id: firstItemId,
    item_state: 'processed',
    processing: 'succeeded',
    processing_receipt: {
      artifact_digest: null,
      artifact_id: '70000000-0000-0000-0000-000000000001',
      coverage: 'partial',
      parser_name: 'markdown-v2',
      receipt_id: '60000000-0000-4000-8000-000000000001',
    },
    retryable: false,
    source_object_id: null,
    source_revision_id: null,
    status: 'succeeded',
    terminal: true,
    warning_ids: ['60000000-0000-0000-0000-000000000003'],
    warnings: [
      {
        attempt: 1,
        code: 'parser.warning',
        stage: 'parse',
        warning_id: '60000000-0000-0000-0000-000000000003',
      },
    ],
    ...overrides,
  };
}

function makeHistoryItem(
  overrides: Partial<components['schemas']['DocumentProcessingHistoryResponse']> = {},
): components['schemas']['DocumentProcessingHistoryResponse'] {
  return {
    acceptance: 'accepted',
    attempt: 1,
    coverage: 'partial',
    failure_ids: [],
    failures: [],
    input_identity: 'file-1',
    input_ordinal: 1,
    item_id: firstItemId,
    item_state: 'processed',
    processing: 'succeeded',
    processing_receipt: {
      artifact_digest: null,
      artifact_id: '70000000-0000-0000-0000-000000000001',
      coverage: 'partial',
      parser_name: 'markdown-v2',
      receipt_id: '60000000-0000-4000-8000-000000000001',
    },
    retryable: false,
    run_id: runId,
    run_state: 'completed',
    source_object_id: null,
    source_revision_id: null,
    status: 'succeeded',
    status_url: `/api/v1/ingestion/runs/${runId}`,
    terminal: true,
    warning_ids: ['60000000-0000-0000-0000-000000000003'],
    warnings: [
      {
        attempt: 1,
        code: 'parser.warning',
        stage: 'parse',
        warning_id: '60000000-0000-0000-0000-000000000003',
      },
    ],
    ...overrides,
  };
}
