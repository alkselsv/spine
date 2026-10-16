import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createMemoryRouter, RouterProvider } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { AuthProvider } from '../../app/auth';
import { DocumentsPage, documentsState, parseDocumentQuery, updateDocumentQuery } from './DocumentsPage';
import { ScopeContext, type Scope } from '../../app/scope';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const sourceObjectId = '30000000-0000-4000-8000-000000000001';
const secondSourceObjectId = '30000000-0000-4000-8000-000000000002';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Knowledge document discovery', () => {
  it('test_query_parser_restores_supported_filters_and_discards_unsupported_values', () => {
    expect(
      parseDocumentQuery(
        '?workspace=protected&source_kind=document&publication_status=published&unsupported=secret&cursor=next',
      ),
    ).toEqual({ source_kind: 'document', publication_status: 'published', cursor: 'next' });
    expect(
      updateDocumentQuery(
        '?workspace=workspace-a&environment=environment-a&publication_status=published&cursor=old',
        { lifecycle: 'current' },
      ),
    ).toBe('workspace=workspace-a&environment=environment-a&lifecycle=current');
  });

  it('test_document_list_uses_generated_query_and_hides_content_identity', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json(collection([makeDocument({ identity: { identity_mode: 'upload', upload_identity: 'secret.pdf' } })])),
    );
    vi.stubGlobal('fetch', fetch);
    const router = renderDocuments(
      '/knowledge/documents?workspace=10000000-0000-4000-8000-000000000001&environment=20000000-0000-4000-8000-000000000001&publication_status=published',
    );

    await screen.findByRole('table');
    const request = fetch.mock.calls[0]?.[0];
    expect(request).toBeInstanceOf(Request);
    const requestUrl = new URL((request as Request).url);
    expect(requestUrl.searchParams.get('limit')).toBe('25');
    expect(requestUrl.searchParams.get('publication_status')).toBe('published');
    expect(requestUrl.searchParams.has('unsupported')).toBe(false);
    expect((request as Request).headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect((request as Request).headers.get('X-Environment-ID')).toBe(scope.environmentId);
    expect(screen.getByText(`ID: ${sourceObjectId}`)).toBeVisible();
    expect(globalThis.document.body).not.toHaveTextContent('secret.pdf');
    expect(router.state.location.search).toContain('publication_status=published');
  });

  it('test_document_list_shows_compact_server_released_access_metadata', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockResolvedValue(
        Response.json(
          collection([
            makeDocument({
              operational_metadata: {
                access_policy: {
                  generation: 9,
                  policy_version_id: '51000000-0000-0000-0000-000000000025',
                  schema_version: 1,
                  version: 4,
                },
                freshness: 'current',
                health: 'healthy',
                lifecycle: 'current',
                processing_state: 'processed',
                publication_status: 'published',
                schema_version: 1,
                security_domain: {
                  binding_version: 2,
                  reference: 'security-domain:contracts',
                  schema_version: 1,
                  version: 'r3',
                },
                source_object_id: sourceObjectId,
                status: 'available',
              },
            }),
          ]),
        ),
      ),
    );

    renderDocuments('/knowledge/documents');

    await screen.findByRole('table');
    const metadataPanel = screen.getByRole('group', { name: 'Доступ и безопасные метаданные' });
    expect(metadataPanel).toBeVisible();
    expect(screen.getByText('security-domain:contracts')).toBeVisible();
    expect(screen.getByText('r3')).toBeVisible();
    expect(screen.getByText('51000000-0000-0000-0000-000000000025')).toBeVisible();
    expect(metadataPanel).toHaveTextContent('Поколение AccessPolicy: 9');
    expect(screen.getByText('Текущий')).toBeVisible();
    expect(screen.getByText('Обработан')).toBeVisible();
    expect(screen.getAllByText('Опубликован').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Актуален').length).toBeGreaterThan(0);
    expect(screen.getByText('50000000-0000-4000-8000-000000000001')).toBeVisible();
    expect(globalThis.document.body).not.toHaveTextContent('grants');
  });

  it('test_filter_change_resets_cursor_and_pagination_preserves_scope', async () => {
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(collection([makeDocument()], 'cursor-next')))
      .mockResolvedValueOnce(Response.json(collection([makeDocument({ source_object_id: secondSourceObjectId })])))
      .mockResolvedValue(Response.json(collection([makeDocument()])));
    vi.stubGlobal('fetch', fetch);
    const router = renderDocuments(
      '/knowledge/documents?workspace=10000000-0000-4000-8000-000000000001&environment=20000000-0000-4000-8000-000000000001&cursor=old&publication_status=published',
    );

    await screen.findByRole('button', { name: 'Следующая страница' });
    await userEvent.setup().click(screen.getByRole('button', { name: 'Следующая страница' }));
    await screen.findByText(`ID: ${secondSourceObjectId}`);
    expect(router.state.location.search).toContain('cursor=cursor-next');
    expect(router.state.location.search).toContain('workspace=10000000-0000-4000-8000-000000000001');
    expect(router.state.location.search).toContain('publication_status=published');

    const user = userEvent.setup();
    await user.click(screen.getByRole('combobox', { name: 'Публикация' }));
    await user.click(await screen.findByRole('option', { name: 'Не опубликован' }));
    await waitFor(() => expect(router.state.location.search).toContain('publication_status=unpublished'));
    expect(router.state.location.search).not.toContain('cursor=');
  });

  it('test_row_navigation_preserves_scope_and_filter_state', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(collection([makeDocument()]))));
    const router = renderDocuments(
      '/knowledge/documents?workspace=10000000-0000-0000-0000-000000000001&environment=20000000-0000-4000-8000-000000000001&source_kind=document',
    );

    await screen.findByRole('table');
    await userEvent.setup().click(screen.getByRole('link', { name: 'Открыть' }));
    expect(router.state.location.pathname).toBe(`/knowledge/documents/${sourceObjectId}`);
    expect(router.state.location.search).toContain('source_kind=document');
    expect(router.state.location.search).toContain('environment=20000000-0000-4000-8000-000000000001');
  });

  it('test_empty_response_exposes_safe_retry_action', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(collection([]))));
    renderDocuments('/knowledge/documents');

    expect(await screen.findByRole('region', { name: 'Пока нет данных' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Повторить загрузку' })).toBeVisible();
  });

  it.each([
    [403, 'Доступ запрещён'],
    [422, 'Проверьте данные'],
    [500, 'Сервис временно недоступен'],
    [503, 'Сервис временно недоступен'],
  ])('test_%s_failure_exposes_only_safe_state', async (status, message) => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockResolvedValue(
        Response.json(
          {
            code: 'spine.safe.failure',
            retryability: 'never',
            safe_message: 'Safe public message.',
            schema_version: 1,
            trace_id: '50000000-0000-4000-8000-000000000001',
            provider_detail: 'secret provider detail',
          },
          { status },
        ),
      ),
    );
    renderDocuments('/knowledge/documents');

    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(globalThis.document.body).not.toHaveTextContent('secret provider detail');
  });

  it('test_document_states_distinguish_partial_stale_and_ready_results', () => {
    const ready = collection([makeDocument()]);
    const partial = collection([makeDocument({ operational_metadata: { schema_version: 1, source_object_id: sourceObjectId, status: 'degraded' } })]);
    expect(documentsState(ready, null, false)).toBe('ready');
    expect(documentsState(partial, null, false)).toBe('partial');
    expect(documentsState(ready, null, true)).toBe('stale');
  });
});

function renderDocuments(initialEntry: string) {
  const router = createMemoryRouter([{ path: '*', element: <DocumentsPage /> }], { initialEntries: [initialEntry] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <AuthProvider value={{ status: 'authenticated', role: 'administrator', getBearerCredential: () => 'test-token' }}>
      <QueryClientProvider client={queryClient}>
        <ScopeContext.Provider value={{ scope, queryKey: (resource) => ['spine', resource, scope.workspaceId, scope.environmentId] }}>
          <RouterProvider router={router} />
        </ScopeContext.Provider>
      </QueryClientProvider>
    </AuthProvider>,
  );
  return router;
}

function collection(items: components['schemas']['DocumentSnapshot'][], nextCursor?: string) {
  return {
    environment_id: scope.environmentId,
    items,
    limit: 25,
    next_cursor: nextCursor ?? null,
    schema_version: 1 as const,
    trace_id: '50000000-0000-4000-8000-000000000001',
    workspace_id: scope.workspaceId,
  };
}

function makeDocument(overrides: Partial<components['schemas']['DocumentSnapshot']> = {}) {
  return {
    created_at: '2026-01-02T03:04:05Z',
    operational_metadata: {
      schema_version: 1 as const,
      source_object_id: overrides.source_object_id ?? sourceObjectId,
      status: 'available' as const,
      processing_state: 'processed' as const,
      lifecycle: 'current' as const,
      publication_status: 'published' as const,
    },
    publication: {
      projection_kind: 'qa',
      status: 'published' as const,
      health: 'healthy' as const,
      freshness: 'current' as const,
    },
    schema_version: 1 as const,
    source_kind: 'document' as const,
    source_object_id: sourceObjectId,
    ...overrides,
  } satisfies components['schemas']['DocumentSnapshot'];
}
