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
    expect(screen.getByText(revisionId)).toBeVisible();
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
});

function renderDetail(initialEntry: string) {
  const router = createMemoryRouter([
    { path: '/knowledge/documents/:documentId', element: <DocumentDetailPage /> },
  ], {
    initialEntries: [initialEntry],
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <AuthProvider
      value={{ status: 'authenticated', role: 'administrator', getBearerCredential: () => 'test-token' }}
    >
      <QueryClientProvider client={queryClient}>
        <ScopeContext.Provider
          value={{ scope, queryKey: (resource) => ['spine', resource, scope.workspaceId, scope.environmentId] }}
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
