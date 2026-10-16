import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { components } from '../../../api/generated/schema';
import { AuthProvider } from '../../app/auth';
import { ScopeContext, type Scope } from '../../app/scope';
import {
  DocumentDetailPage,
  documentOperationalState,
} from './DocumentDetailPage';
import { documentMetadataState } from './DocumentAccessMetadata';

type DocumentSnapshot = components['schemas']['DocumentSnapshot'];

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000002',
  environmentId: '20000000-0000-4000-8000-000000000002',
};
const sourceObjectId = '30000000-0000-4000-8000-000000000001';
const revisionId = '40000000-0000-4000-8000-000000000001';
const ingestionRunId = '50000000-0000-4000-8000-000000000001';
const projectionRunId = '60000000-0000-4000-8000-000000000001';
const processingReceiptId = '50000000-0000-4000-8000-000000000003';
const snapshotId = '60000000-0000-4000-8000-000000000004';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Knowledge document operational overview', () => {
  it('test_detail_loads_scoped_snapshot_and_renders_safe_operational_metadata', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json({
        data: makeDocument({
          identity: { identity_mode: 'upload', upload_identity: 'secret-contract.pdf' },
        }),
        environment_id: scope.environmentId,
        schema_version: 1,
        trace_id: '70000000-0000-4000-8000-000000000001',
        workspace_id: scope.workspaceId,
      }),
    );
    vi.stubGlobal('fetch', fetch);
    const router = renderDetail(
      `/knowledge/documents/${sourceObjectId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=overview`,
    );

    expect(await screen.findByRole('heading', { name: 'Операционный обзор документа' })).toBeVisible();
    expect(screen.getAllByText('Текущий').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Опубликован').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Актуален').length).toBeGreaterThan(0);
    expect(screen.getAllByText(revisionId).length).toBeGreaterThan(0);
    expect(screen.getByText(processingReceiptId)).toBeVisible();
    expect(screen.getByText(snapshotId)).toBeVisible();
    expect(screen.getByRole('link', { name: 'Открыть запуск обработки' })).toHaveAttribute(
      'href',
      `/runs/${ingestionRunId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&document=${sourceObjectId}`,
    );
    expect(screen.getByRole('link', { name: 'История ревизий' })).toHaveAttribute(
      'href',
      `/knowledge/documents/${sourceObjectId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=revisions`,
    );
    expect(screen.getByText('secret-contract.pdf')).toBeVisible();

    const request = fetch.mock.calls[0]?.[0];
    expect(request).toBeInstanceOf(Request);
    expect(new URL((request as Request).url).pathname).toBe(
      `/api/v1/documents/${sourceObjectId}`,
    );
    expect((request as Request).headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect((request as Request).headers.get('X-Environment-ID')).toBe(scope.environmentId);

    await userEvent.setup().click(screen.getByRole('link', { name: 'История ревизий' }));
    await waitFor(() => expect(router.state.location.search).toContain('tab=revisions'));
    expect(router.state.location.search).toContain(`workspace=${scope.workspaceId}`);
    expect(router.state.location.search).toContain(`environment=${scope.environmentId}`);
  });

  it('test_detail_renders_only_server_released_policy_metadata_for_metadata_only_response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockResolvedValue(
        Response.json(
          makeDocumentEnvelope(scope, revisionId, {
            operational_metadata: {
              access_policy: {
                generation: 9,
                policy_version_id: '51000000-0000-0000-0000-000000000025',
                schema_version: 1,
                version: 4,
              },
              freshness: 'stale',
              health: 'degraded',
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
              source_revision_id: revisionId,
              status: 'available',
            },
            identity: null,
          }),
        ),
      ),
    );

    renderDetail(
      `/knowledge/documents/${sourceObjectId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}`,
    );

    expect(await screen.findByRole('heading', { name: 'Доступ и безопасные метаданные' })).toBeVisible();
    expect(screen.getByText('security-domain:contracts')).toBeVisible();
    expect(screen.getByText('51000000-0000-0000-0000-000000000025')).toBeVisible();
    expect(screen.getByText('Устарела')).toBeVisible();
    expect(screen.getAllByText('Ограничен').length).toBeGreaterThan(0);
    expect(screen.getByText('Повторите наблюдение после восстановления сервиса.')).toBeVisible();
    expect(screen.getByText(scope.workspaceId)).toBeVisible();
    expect(screen.getByText(scope.environmentId)).toBeVisible();
    expect(globalThis.document.body).not.toHaveTextContent('secret-contract.pdf');
    expect(globalThis.document.body).not.toHaveTextContent('grants');
  });

  it('test_denied_or_missing_detail_exposes_only_non_enumerating_safe_error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockResolvedValue(
        Response.json(
          {
            code: 'spine.documents.not_found',
            retryability: 'never',
            safe_message: 'The requested resource is unavailable.',
            schema_version: 1,
            trace_id: '70000000-0000-0000-0000-000000000001',
            protected_text: 'customer secret must not leak',
            resource_exists: true,
          },
          { status: 404 },
        ),
      ),
    );
    renderDetail(`/knowledge/documents/${sourceObjectId}`);

    expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось выполнить операцию');
    expect(globalThis.document.body).not.toHaveTextContent('customer secret must not leak');
    expect(globalThis.document.body).not.toHaveTextContent(sourceObjectId);
  });

  it('test_unavailable_metadata_exposes_safe_next_action_without_policy_or_content', async () => {
    const metadata = makeDocument().operational_metadata!;
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockResolvedValue(
        Response.json(
          makeDocumentEnvelope(scope, revisionId, {
            identity: null,
            operational_metadata: {
              ...metadata,
              access_policy: null,
              diagnostic_code: 'security_domain_unavailable',
              security_domain: null,
              status: 'unavailable',
            },
          }),
        ),
      ),
    );

    renderDetail(`/knowledge/documents/${sourceObjectId}`);

    expect(await screen.findByRole('heading', { name: 'Доступ и безопасные метаданные' })).toBeVisible();
    expect(screen.getAllByText('Проверьте привязку Security Domain.').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Недоступен').length).toBeGreaterThan(0);
    expect(screen.getByText('Недоступен в безопасном ответе')).toBeVisible();
    expect(globalThis.document.body).not.toHaveTextContent('secret-contract.pdf');
    expect(globalThis.document.body).not.toHaveTextContent('grants');
  });

  it.each([
    ['current', 'Текущий'],
    ['processing', 'Разбирается'],
    ['superseded', 'Замещён'],
    ['tombstoned', 'Удалён'],
    ['stale', 'Устарела'],
    ['degraded', 'Ограничен'],
    ['unpublished', 'Не опубликован'],
    ['unavailable', 'Недоступен'],
    ['failed', 'Ошибка обработки'],
    ['ready', 'Готов'],
  ] as const)('test_%s_operational_fixture_has_distinct_safe_label', (state, label) => {
    expect(documentOperationalState(makeDocumentForState(state))).toContain(label);
  });

  it.each([
    ['available', 'available'],
    ['stale', 'stale'],
    ['degraded', 'degraded'],
    ['unavailable', 'unavailable'],
  ] as const)('test_%s_metadata_fixture_has_distinct_safe_state', (state, expected) => {
    const metadata = makeDocumentForState(state === 'available' ? 'ready' : state).operational_metadata;
    expect(documentMetadataState(metadata)).toBe(expected);
  });

  it('test_revision_history_loads_scoped_revisions_in_deterministic_order', async () => {
    const olderId = '40000000-0000-4000-8000-000000000002';
    const currentId = '40000000-0000-4000-8000-000000000003';
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(makeDocumentEnvelope()))
      .mockResolvedValueOnce(Response.json(makeRevisionEnvelope([
        makeRevision({
          source_revision_id: olderId,
          observed_at: '2026-10-14T10:00:00Z',
          lifecycle: 'superseded',
          superseded_by_revision_id: revisionId,
        }),
        makeRevision({
          source_revision_id: currentId,
          observed_at: '2026-10-16T10:00:00Z',
          lifecycle: 'tombstoned',
          kind: 'tombstone',
          predecessor_revision_id: revisionId,
        }),
        makeRevision({ source_revision_id: revisionId, lifecycle: 'current' }),
      ])));
    vi.stubGlobal('fetch', fetch);

    renderDetail(`/knowledge/documents/${sourceObjectId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=revisions`);

    const rows = await screen.findAllByRole('link', { name: /40000000-0000-4000-8000-00000000000/ });
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining(currentId),
      expect.stringContaining(revisionId),
      expect.stringContaining(olderId),
    ]);
    expect(screen.getByText('Удалён')).toBeVisible();
    expect(screen.getAllByText('Замещена ревизией').length).toBeGreaterThan(0);
    expect(screen.getAllByText(revisionId).length).toBeGreaterThan(0);

    const request = fetch.mock.calls[1]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe(`/api/v1/documents/${sourceObjectId}/revisions`);
    expect(request.headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(scope.environmentId);
  });

  it('test_revision_history_restores_exact_url_selection_without_rebinding', async () => {
    const selectedRevisionId = '40000000-0000-4000-8000-000000000009';
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(makeDocumentEnvelope()))
      .mockResolvedValueOnce(Response.json(makeRevisionEnvelope([
        makeRevision({ source_revision_id: revisionId, lifecycle: 'current' }),
      ]))));

    const router = renderDetail(`/knowledge/documents/${sourceObjectId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=revisions&revision=${selectedRevisionId}`);

    expect(await screen.findByText('Выбранная ревизия недоступна в этой странице истории.')).toBeVisible();
    expect(router.state.location.search).toContain(`revision=${selectedRevisionId}`);
    expect(screen.queryByText('Защищённое содержимое ревизии')).not.toBeInTheDocument();
  });

  it('test_revision_history_paginates_with_scope_and_exact_selection_in_url', async () => {
    const selectedRevisionId = '40000000-0000-4000-8000-000000000009';
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(makeDocumentEnvelope()))
      .mockResolvedValueOnce(Response.json(makeRevisionEnvelope([
        makeRevision({ source_revision_id: selectedRevisionId, lifecycle: 'current' }),
      ], 'opaque-next-cursor')))
      .mockResolvedValueOnce(Response.json(makeRevisionEnvelope([
        makeRevision({ source_revision_id: revisionId, lifecycle: 'superseded' }),
      ])));
    vi.stubGlobal('fetch', fetch);
    const router = renderDetail(`/knowledge/documents/${sourceObjectId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=revisions&revision=${selectedRevisionId}`);
    const user = userEvent.setup();

    await user.click(await screen.findByRole('link', { name: 'Следующая страница' }));

    await waitFor(() => expect(router.state.location.search).toContain('cursor=opaque-next-cursor'));
    expect(router.state.location.search).toContain(`revision=${selectedRevisionId}`);
    expect(new URL((fetch.mock.calls[2]?.[0] as Request).url).searchParams.get('cursor')).toBe('opaque-next-cursor');
    expect((fetch.mock.calls[2]?.[0] as Request).headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
  });

  it('test_revision_history_recovers_from_tampered_cursor_without_losing_selection', async () => {
    const selectedRevisionId = '40000000-0000-4000-8000-000000000009';
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(makeDocumentEnvelope()))
      .mockResolvedValueOnce(Response.json({
        code: 'spine.documents.invalid_cursor',
        retryability: 'never',
        safe_message: 'The page cursor is invalid.',
        schema_version: 1,
        trace_id: '70000000-0000-4000-8000-000000000001',
      }, { status: 400 }))
      .mockResolvedValueOnce(Response.json(makeRevisionEnvelope([
        makeRevision({ source_revision_id: selectedRevisionId, lifecycle: 'superseded' }),
      ])));
    vi.stubGlobal('fetch', fetch);
    const user = userEvent.setup();
    const router = renderDetail(`/knowledge/documents/${sourceObjectId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=revisions&revision=${selectedRevisionId}&cursor=forged`);

    expect(await screen.findByRole('alert')).toHaveTextContent('Проверьте данные');
    await user.click(screen.getByRole('button', { name: 'Сбросить страницу и повторить' }));

    expect(await screen.findByRole('link', { name: selectedRevisionId })).toBeVisible();
    await waitFor(() => expect(router.state.location.search).not.toContain('cursor='));
    expect(router.state.location.search).toContain(`revision=${selectedRevisionId}`);
    expect(new URL((fetch.mock.calls[2]?.[0] as Request).url).searchParams.has('cursor')).toBe(false);
    expect(globalThis.document.body).not.toHaveTextContent('Защищённое содержимое ревизии');
  });

  it('test_revision_history_denied_content_shows_permission_state_without_payload', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(makeDocumentEnvelope()))
      .mockResolvedValueOnce(Response.json({
        code: 'spine.authorization.denied',
        retryability: 'never',
        safe_message: 'Not authorized.',
        schema_version: 1,
        trace_id: '70000000-0000-4000-8000-000000000001',
        protected_text: 'must never be displayed',
      }, { status: 403 })));

    renderDetail(`/knowledge/documents/${sourceObjectId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=revisions`);

    expect(await screen.findByRole('alert')).toHaveTextContent('Доступ запрещён');
    expect(globalThis.document.body).not.toHaveTextContent('must never be displayed');
  });

  it('test_revision_history_cache_isolated_when_workspace_changes', async () => {
    const workspaceA: Scope = {
      workspaceId: '10000000-0000-4000-8000-000000000001',
      environmentId: '20000000-0000-4000-8000-000000000001',
    };
    const workspaceB: Scope = {
      workspaceId: '10000000-0000-4000-8000-000000000002',
      environmentId: '20000000-0000-4000-8000-000000000002',
    };
    const revisionA = '40000000-0000-4000-8000-000000000010';
    const revisionB = '40000000-0000-4000-8000-000000000011';
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(makeDocumentEnvelope(workspaceA, revisionA)))
      .mockResolvedValueOnce(Response.json(makeRevisionEnvelope([
        makeRevision({ source_revision_id: revisionA, lifecycle: 'current' }),
      ])))
      .mockResolvedValueOnce(Response.json(makeDocumentEnvelope(workspaceB, revisionB)))
      .mockResolvedValueOnce(Response.json(makeRevisionEnvelope([
        makeRevision({ source_revision_id: revisionB, lifecycle: 'current' }),
      ])));
    vi.stubGlobal('fetch', fetch);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    renderDetail(
      `/knowledge/documents/${sourceObjectId}?workspace=${workspaceA.workspaceId}&environment=${workspaceA.environmentId}&tab=revisions`,
      { scope: workspaceA, queryClient },
    );
    expect(await screen.findByRole('link', { name: revisionA })).toBeVisible();

    cleanup();
    renderDetail(
      `/knowledge/documents/${sourceObjectId}?workspace=${workspaceB.workspaceId}&environment=${workspaceB.environmentId}&tab=revisions`,
      { scope: workspaceB, queryClient },
    );

    expect(await screen.findByRole('link', { name: revisionB })).toBeVisible();
    expect(globalThis.document.body).not.toHaveTextContent(revisionA);
    expect(fetch).toHaveBeenCalledTimes(4);
    expect((fetch.mock.calls[3]?.[0] as Request).headers.get('X-Workspace-ID')).toBe(workspaceB.workspaceId);
  });
});

function renderDetail(
  initialEntry: string,
  options: { queryClient?: QueryClient; scope?: Scope } = {},
) {
  const activeScope = options.scope ?? scope;
  const router = createMemoryRouter([
    { path: '/knowledge/documents/:documentId', element: <DocumentDetailPage /> },
  ], {
    initialEntries: [initialEntry],
  });
  const queryClient = options.queryClient ?? new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <AuthProvider
      value={{ status: 'authenticated', role: 'administrator', getBearerCredential: () => 'test-token' }}
    >
      <QueryClientProvider client={queryClient}>
        <ScopeContext.Provider
          value={{
            scope: activeScope,
            queryKey: (resource) => ['spine', resource, activeScope.workspaceId, activeScope.environmentId],
          }}
        >
          <RouterProvider router={router} />
        </ScopeContext.Provider>
      </QueryClientProvider>
    </AuthProvider>,
  );
  return router;
}

function makeDocument(
  overrides: Partial<components['schemas']['DocumentSnapshot']> = {},
): components['schemas']['DocumentSnapshot'] {
  return {
    created_at: '2026-10-15T09:00:00Z',
    current_revision: {
      accepted_at: '2026-10-15T10:01:00Z',
      accepted_generation: 7,
      admission_status: 'admitted',
      kind: 'content',
      lifecycle: 'current',
      observed_at: '2026-10-15T10:00:00Z',
      predecessor_revision_id: null,
      processing_references: [
        {
          coverage: 'complete',
          ingestion_item_id: '50000000-0000-4000-8000-000000000002',
          ingestion_run_id: ingestionRunId,
          processing_receipt_id: '50000000-0000-4000-8000-000000000003',
          state: 'processed',
        },
      ],
      projection_references: [
        {
          projection_candidate_id: '60000000-0000-4000-8000-000000000002',
          projection_kind: 'qa',
          projection_run_id: projectionRunId,
          state: 'complete',
        },
      ],
      schema_version: 1,
      source_revision_id: revisionId,
      superseded_by_revision_id: null,
    },
    operational_metadata: {
      freshness: 'current',
      health: 'healthy',
      lifecycle: 'current',
      processing_state: 'processed',
      publication_status: 'published',
      schema_version: 1,
      source_object_id: sourceObjectId,
      source_revision_id: revisionId,
      status: 'available',
    },
    publication: {
      drift_count: 0,
      freshness: 'current',
      health: 'healthy',
      manifest_complete: true,
      projection_kind: 'qa',
      snapshot_id: snapshotId,
      source_covered: true,
      status: 'published',
    },
    schema_version: 1,
    source_kind: 'document',
    source_object_id: sourceObjectId,
    ...overrides,
  };
}

function makeDocumentEnvelope(
  activeScope: Scope = scope,
  currentRevisionId = revisionId,
  overrides: Partial<components['schemas']['DocumentSnapshot']> = {},
) {
  const document = makeDocument(overrides);
  const data: DocumentSnapshot = {
    ...document,
    current_revision: document.current_revision
      ? { ...document.current_revision, source_revision_id: currentRevisionId }
      : null,
    operational_metadata: document.operational_metadata
      ? { ...document.operational_metadata, source_revision_id: currentRevisionId }
      : null,
  };
  return {
    data,
    environment_id: activeScope.environmentId,
    schema_version: 1,
    trace_id: '70000000-0000-4000-8000-000000000001',
    workspace_id: activeScope.workspaceId,
  };
}

function makeRevision(
  overrides: Partial<components['schemas']['RevisionSnapshot']> = {},
): components['schemas']['RevisionSnapshot'] {
  return {
    accepted_at: '2026-10-15T10:01:00Z',
    accepted_generation: 7,
    admission_status: 'admitted',
    content: null,
    deletion_reason: null,
    kind: 'content',
    lifecycle: 'processing',
    observed_at: '2026-10-15T10:00:00Z',
    predecessor_revision_id: null,
    processing_references: [],
    projection_references: [],
    schema_version: 1,
    source_revision_id: revisionId,
    superseded_by_revision_id: null,
    ...overrides,
  };
}

function makeRevisionEnvelope(
  items: components['schemas']['RevisionSnapshot'][],
  nextCursor: string | null = null,
) {
  return {
    environment_id: scope.environmentId,
    items,
    limit: 25,
    next_cursor: nextCursor,
    schema_version: 1,
    trace_id: '70000000-0000-4000-8000-000000000001',
    workspace_id: scope.workspaceId,
  };
}

function makeDocumentForState(
  state: 'current' | 'processing' | 'superseded' | 'tombstoned' | 'stale' | 'degraded' | 'unpublished' | 'unavailable' | 'failed' | 'ready',
) {
  const document = makeDocument();
  if (state === 'processing') {
    return makeDocument({
      current_revision: { ...document.current_revision!, lifecycle: 'processing' },
      operational_metadata: { ...document.operational_metadata!, lifecycle: 'processing' },
    });
  }
  if (state === 'superseded') {
    return makeDocument({
      current_revision: { ...document.current_revision!, lifecycle: 'superseded' },
      operational_metadata: { ...document.operational_metadata!, lifecycle: 'superseded' },
    });
  }
  if (state === 'tombstoned') {
    return makeDocument({
      current_revision: { ...document.current_revision!, lifecycle: 'tombstoned', kind: 'tombstone' },
      operational_metadata: { ...document.operational_metadata!, lifecycle: 'tombstoned' },
    });
  }
  if (state === 'stale') {
    return makeDocument({
      operational_metadata: { ...document.operational_metadata!, freshness: 'stale' },
      publication: { ...document.publication!, freshness: 'stale' },
    });
  }
  if (state === 'degraded') {
    return makeDocument({
      operational_metadata: { ...document.operational_metadata!, health: 'degraded' },
      publication: { ...document.publication!, health: 'degraded' },
    });
  }
  if (state === 'unpublished') {
    return makeDocument({
      operational_metadata: { ...document.operational_metadata!, publication_status: 'unpublished' },
      publication: { ...document.publication!, status: 'unpublished' },
    });
  }
  if (state === 'unavailable') {
    return makeDocument({
      operational_metadata: { ...document.operational_metadata!, status: 'unavailable' },
      publication: { ...document.publication!, status: 'unavailable' },
    });
  }
  if (state === 'failed') {
    return makeDocument({
      current_revision: {
        ...document.current_revision!,
        processing_references: [{ ...document.current_revision!.processing_references[0]!, state: 'processing_failed' }],
      },
      operational_metadata: { ...document.operational_metadata!, processing_state: 'processing_failed' },
    });
  }
  if (state === 'current') {
    return makeDocument({
      current_revision: { ...document.current_revision!, processing_references: [] },
      operational_metadata: { ...document.operational_metadata!, processing_state: null },
      publication: null,
    });
  }
  return makeDocument();
}
