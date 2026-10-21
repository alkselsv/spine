import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { components } from '../../../api/generated/schema';
import { AuthProvider } from '../../app/auth';
import { ScopeContext, type Scope } from '../../app/scope';
import { DocumentDetailPage } from './DocumentDetailPage';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000002',
  environmentId: '20000000-0000-4000-8000-000000000002',
};
const documentId = '30000000-0000-4000-8000-000000000001';
const traceId = '70000000-0000-4000-8000-000000000001';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('document projection operations panel', () => {
  it('test_projection_panel_loads_scoped_status_and_operational_health_independently', async () => {
    const calls: Request[] = [];
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      calls.push(request);
      const path = new URL(request.url).pathname;
      if (path === `/api/v1/documents/${documentId}`) {
        return Response.json(documentEnvelope());
      }
      if (path === '/api/v1/projections/qa/status') {
        return Response.json({
          data: {
            activation_generation: 12,
            as_of: {
              accepted_through_generation: 81,
              boundary_time: '2026-10-15T10:00:00Z',
              canonical_boundary_id: '40000000-0000-4000-8000-000000000001',
            },
            canonical_boundary_id: '40000000-0000-4000-8000-000000000001',
            drift_count: 3,
            freshness: 'stale',
            health: 'healthy',
            projection_config_version: 4,
            projection_config_version_id: '50000000-0000-4000-8000-000000000001',
            projection_kind: 'qa',
            publication_status: 'published',
            recovery_state: 'not_requested',
            retryable: false,
            schema_version: 1,
            snapshot_id: '60000000-0000-4000-8000-000000000001',
          },
          environment_id: scope.environmentId,
          schema_version: 1,
          trace_id: traceId,
          workspace_id: scope.workspaceId,
        });
      }
      if (path === '/api/v1/health/summary') {
        return Response.json({
          data: {
            authorization: { code: 'authorization.ready', status: 'ready' },
            build_version: 'test-build',
            ingestion: { code: 'ingestion.ready', status: 'ready' },
            providers: {},
            publication: { code: 'publication.ready', status: 'ready' },
            status: 'ready',
          },
          environment_id: scope.environmentId,
          schema_version: 1,
          trace_id: traceId,
          workspace_id: scope.workspaceId,
        });
      }
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderPanel(`/knowledge/documents/${documentId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=projection`);

    expect(await screen.findByText('Активный снимок')).toBeVisible();
    expect(screen.getByText('Устарела')).toBeVisible();
    expect(screen.getByText('Дрейф')).toBeVisible();
    expect(screen.getAllByText('Здоров').length).toBeGreaterThan(1);
    expect(screen.getByText(/не подтверждает доступ к доказательствам/i)).toBeVisible();
    expect(calls.map((request) => new URL(request.url).pathname)).toEqual(
      expect.arrayContaining([
        `/api/v1/projections/qa/status`,
        '/api/v1/health/summary',
      ]),
    );
    for (const request of calls.filter((item) => new URL(item.url).pathname.includes('/projections/'))) {
      expect(request.headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
      expect(request.headers.get('X-Environment-ID')).toBe(scope.environmentId);
    }
    expect(globalThis.document.body).not.toHaveTextContent(/namespace|provider diagnostics/i);
  });

  it.each(projectionStateMatrix())('test_projection_status_dimensions_remain_independent: $name', async ({ status, processing, labels }) => {
    installControlledPanel({ status, processing });
    renderPanel(`/knowledge/documents/${documentId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=projection&projection_kind=qa`);
    for (const label of labels) expect(await screen.findAllByText(label)).not.toHaveLength(0);
  });

  it('test_projection_optional_provenance_omits_null_values', async () => {
    installControlledPanel({
      status: publishedStatus({ activation_generation: null, projection_config_version: null, projection_config_version_id: null }),
      processing: 'processed',
    });
    renderPanel(`/knowledge/documents/${documentId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=projection&projection_kind=qa`);
    expect(await screen.findByText('Поколение активации')).toBeVisible();
    expect(screen.getByText('Версия конфигурации проекции')).toBeVisible();
    expect(globalThis.document.body).not.toHaveTextContent(/\bnull\b/);
  });

  it('test_projection_recovery_actions_are_hidden_when_status_advertises_none', async () => {
    installControlledPanel({ status: publishedStatus(), processing: 'processed' });
    renderPanel(`/knowledge/documents/${documentId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=projection&projection_kind=qa`);
    expect(await screen.findByText(/Дополнительное восстановление не требуется/)).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Повторить операцию проекции' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Запросить пересборку проекции' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Запросить восстановление проекции' })).not.toBeInTheDocument();
  });

  it('test_projection_status_denial_hides_protected_diagnostics', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === `/api/v1/documents/${documentId}`) return Response.json(documentEnvelope());
      if (path === '/api/v1/projections/qa/status') {
        return Response.json({
          code: 'spine.authorization.denied',
          retryability: 'never',
          safe_message: 'The requested resource is unavailable.',
          schema_version: 1,
          trace_id: traceId,
          protected_text: 'private customer payload',
          provider_diagnostic: 'secret provider trace',
        }, { status: 403 });
      }
      return Response.json(healthEnvelope());
    }));
    renderPanel(`/knowledge/documents/${documentId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=projection`);
    expect(await screen.findByRole('alert')).toHaveTextContent('Доступ запрещён');
    expect(globalThis.document.body).not.toHaveTextContent('private customer payload');
    expect(globalThis.document.body).not.toHaveTextContent('secret provider trace');
  });

  it('test_projection_requested_polls_expected_unpublished_status_until_current', async () => {
    let statusReads = 0;
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === `/api/v1/documents/${documentId}`) {
        return Response.json(documentEnvelope('projection_requested'));
      }
      if (path === '/api/v1/projections/qa/status') {
        statusReads += 1;
        if (statusReads === 1) {
          return Response.json({
            code: 'spine.projection.unavailable',
            retryability: 'after_delay',
            safe_message: 'The projection is not published yet.',
            schema_version: 1,
            trace_id: traceId,
          }, { status: 404 });
        }
        return Response.json({ ...scopeEnvelope(), data: publishedStatus() });
      }
      if (path === '/api/v1/health/summary') return Response.json(healthEnvelope());
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderPanel(`/knowledge/documents/${documentId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=projection`);

    expect(await screen.findByText('Актуальна', {}, { timeout: 5_000 })).toBeVisible();
    expect(statusReads).toBeGreaterThan(1);
  });

  it('test_processed_ingestion_keeps_polling_stale_projection_until_current', async () => {
    let statusReads = 0;
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === `/api/v1/documents/${documentId}`) {
        return Response.json(documentEnvelope('processed'));
      }
      if (path === '/api/v1/projections/qa/status') {
        statusReads += 1;
        return Response.json({
          ...scopeEnvelope(),
          data: publishedStatus(statusReads === 1 ? { freshness: 'stale' } : {}),
        });
      }
      if (path === '/api/v1/health/summary') return Response.json(healthEnvelope());
      throw new Error(`Unexpected request: ${path}`);
    }));

    renderPanel(`/knowledge/documents/${documentId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=projection`);

    expect(await screen.findByText('Актуальна', {}, { timeout: 5_000 })).toBeVisible();
    expect(statusReads).toBeGreaterThan(1);
  });

  it('test_projection_recovery_reuses_idempotency_key_after_ambiguous_failure', async () => {
    const keys: string[] = [];
    let attempts = 0;
    vi.stubGlobal('crypto', { randomUUID: () => 'fixed-command-id' });
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>(async (input, init) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === `/api/v1/documents/${documentId}`) return Response.json(documentEnvelope());
      if (path === '/api/v1/projections/qa/status') {
        return Response.json({ ...scopeEnvelope(), data: publishedStatus({ freshness: 'stale', drift_count: 1 }) });
      }
      if (path === '/api/v1/health/summary') return Response.json(healthEnvelope());
      if (path === '/api/v1/projections/qa/recovery') {
        const command = new Request(request, init);
        keys.push(command.headers.get('Idempotency-Key') ?? '');
        attempts += 1;
        if (attempts === 1) {
          return Response.json({ code: 'spine.projection.unavailable', retryability: 'after_delay', safe_message: 'The service is unavailable.', schema_version: 1, trace_id: traceId }, { status: 503 });
        }
        return Response.json({ command_id: '70000000-0000-4000-8000-000000000002', environment_id: scope.environmentId, replay: true, result: { action: 'rebuild', command_id: '70000000-0000-4000-8000-000000000002', projection_kind: 'qa', schema_version: 1, state: 'accepted' }, schema_version: 1, status: 'accepted', trace_id: traceId, workspace_id: scope.workspaceId }, { status: 202 });
      }
      throw new Error(`Unexpected request: ${path}`);
    }));
    const user = userEvent.setup();
    renderPanel(`/knowledge/documents/${documentId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=projection`);

    const retry = await screen.findByRole('button', { name: 'Запросить пересборку проекции' });
    await user.click(retry);
    expect(await screen.findByRole('alert')).toHaveTextContent('Сервис временно недоступен');
    await user.click(retry);
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[0]).toBe('projection-recovery-fixed-command-id');
    expect(keys[1]).toBe(keys[0]);
    expect(await screen.findByText(/Команда уже была принята/)).toBeVisible();
    expect(screen.getByText(/70000000-0000-4000-8000-000000000002/)).toBeVisible();
    expect(screen.getByText('Состояние квитанции').parentElement).toHaveTextContent('Принята');
    expect(screen.getByText(/не подтверждает завершение пересборки или активации/i)).toBeVisible();
  });

  it('test_projection_recovery_conflict_is_safe_and_allows_a_new_command_after_refresh', async () => {
    const keys: string[] = [];
    let attempts = 0;
    let statusReads = 0;
    vi.stubGlobal('crypto', { randomUUID: vi.fn().mockReturnValueOnce('first-command').mockReturnValueOnce('next-command') });
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>(async (input, init) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === `/api/v1/documents/${documentId}`) return Response.json(documentEnvelope());
      if (path === '/api/v1/projections/qa/status') {
        statusReads += 1;
        return Response.json({ ...scopeEnvelope(), data: publishedStatus({ freshness: 'stale' }) });
      }
      if (path === '/api/v1/health/summary') return Response.json(healthEnvelope());
      if (path === '/api/v1/projections/qa/recovery') {
        const command = new Request(request, init);
        keys.push(command.headers.get('Idempotency-Key') ?? '');
        attempts += 1;
        if (attempts === 1) return Response.json({ code: 'spine.persistence.idempotency_conflict', retryability: 'never', safe_message: 'Safe conflict.', schema_version: 1, trace_id: traceId, provider_detail: 'private provider detail' }, { status: 409 });
        return Response.json({ command_id: '70000000-0000-4000-8000-000000000003', environment_id: scope.environmentId, replay: false, schema_version: 1, status: 'accepted', trace_id: traceId, workspace_id: scope.workspaceId }, { status: 202 });
      }
      throw new Error(`Unexpected request: ${path}`);
    }));
    const user = userEvent.setup();
    renderPanel(`/knowledge/documents/${documentId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=projection`);

    const rebuild = await screen.findByRole('button', { name: 'Запросить пересборку проекции' });
    await user.click(rebuild);
    expect(await screen.findByRole('alert')).toHaveTextContent('Команда конфликтует');
    expect(globalThis.document.body).not.toHaveTextContent('private provider detail');
    await user.click(screen.getByRole('button', { name: 'Обновить состояние' }));
    await waitFor(() => expect(statusReads).toBeGreaterThan(1));
    await user.click(rebuild);
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[0]).not.toBe(keys[1]);
    expect(await screen.findByText('Идентификатор команды')).toBeVisible();
  });

  it.each([
    [403, 'Доступ запрещён', 'private provider detail'],
    [503, 'Сервис временно недоступен', 'private provider detail'],
  ])('test_projection_recovery_failure_%s_stays_safe', async (status, expectedMessage, privateDetail) => {
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === `/api/v1/documents/${documentId}`) return Response.json(documentEnvelope());
      if (path === '/api/v1/projections/qa/status') return Response.json({ ...scopeEnvelope(), data: publishedStatus({ freshness: 'stale' }) });
      if (path === '/api/v1/health/summary') return Response.json(healthEnvelope());
      if (path === '/api/v1/projections/qa/recovery') return Response.json({ code: status === 403 ? 'spine.authorization.denied' : 'spine.projection.unavailable', retryability: 'after_delay', safe_message: 'Safe message.', schema_version: 1, trace_id: traceId, provider_detail: privateDetail }, { status });
      throw new Error(`Unexpected request: ${path}`);
    }));
    const user = userEvent.setup();
    renderPanel(`/knowledge/documents/${documentId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=projection`);
    await user.click(await screen.findByRole('button', { name: 'Запросить пересборку проекции' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(expectedMessage);
    expect(globalThis.document.body).not.toHaveTextContent(privateDetail);
  });
});

function installControlledPanel(options: {
  status: components['schemas']['ProjectionStatusResponse'];
  processing: components['schemas']['IngestionItemState'];
}) {
  vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>(async (input) => {
    const path = new URL((input as Request).url).pathname;
    if (path === `/api/v1/documents/${documentId}`) return Response.json(documentEnvelope(options.processing));
    if (path === '/api/v1/projections/qa/status') {
      return Response.json({ ...scopeEnvelope(), data: options.status });
    }
    if (path === '/api/v1/health/summary') return Response.json(healthEnvelope());
    throw new Error(`Unexpected request: ${path}`);
  }));
}

function publishedStatus(overrides: Partial<components['schemas']['ProjectionStatusResponse']> = {}): components['schemas']['ProjectionStatusResponse'] {
  return {
    activation_generation: 12,
    as_of: {
      accepted_through_generation: 81,
      boundary_time: '2026-10-15T10:00:00Z',
      canonical_boundary_id: '40000000-0000-4000-8000-000000000001',
    },
    canonical_boundary_id: '40000000-0000-4000-8000-000000000001',
    drift_count: 0,
    freshness: 'current',
    health: 'healthy',
    projection_config_version: 4,
    projection_config_version_id: '50000000-0000-4000-8000-000000000001',
    projection_kind: 'qa',
    publication_status: 'published',
    recovery_state: 'not_requested',
    retryable: false,
    schema_version: 1,
    snapshot_id: '60000000-0000-4000-8000-000000000001',
    ...overrides,
  };
}

function projectionStateMatrix() {
  const publications = ['published', 'unpublished', 'unavailable'] as const;
  const freshnessValues = ['current', 'stale', null] as const;
  const healthValues = ['healthy', 'degraded', null] as const;
  const processingValues = [
    'received', 'processing_ready', 'stored', 'observed', 'admission_pending', 'quarantined',
    'rejected', 'accepted', 'processing', 'processed', 'processing_failed', 'projection_requested',
  ] as const;
  const publicationLabels = { published: 'Опубликован', unpublished: 'Не опубликован', unavailable: 'Недоступен' };
  const freshnessLabels = { current: 'Актуальна', stale: 'Устарела' };
  const healthLabels = { healthy: 'Здоров', degraded: 'Ограничен' };
  const processingLabels: Record<(typeof processingValues)[number], string> = {
    received: 'Индексация ещё не готова',
    processing_ready: 'Индексация ещё не готова',
    stored: 'Индексация ещё не готова',
    observed: 'Индексация ещё не готова',
    admission_pending: 'Индексация ещё не готова',
    quarantined: 'Индексация ещё не готова',
    rejected: 'Индексация ещё не готова',
    accepted: 'Индексация ещё не готова',
    processing: 'Индексируется',
    processed: 'Индексация завершена',
    processing_failed: 'Ошибка обработки',
    projection_requested: 'Индексируется',
  };
  return publications.flatMap((publication) => freshnessValues.flatMap((freshness) => healthValues.flatMap((health) => processingValues.map((processing) => ({
    name: `${publication}/${freshness ?? 'unknown'}/${health ?? 'unknown'}/${processing}`,
    status: publication === 'published'
      ? publishedStatus({ freshness, health })
      : { ...emptyStatus(publication), freshness, health },
    processing,
    labels: [
      publicationLabels[publication],
      freshness ? freshnessLabels[freshness] : 'Не указано',
      health ? healthLabels[health] : 'Не указано',
      processingLabels[processing],
    ],
  })))));
}

function emptyStatus(publication_status: components['schemas']['ProjectionPublicationStatus']): components['schemas']['ProjectionStatusResponse'] {
  return { projection_kind: 'qa', publication_status, recovery_state: 'not_requested', retryable: false, schema_version: 1 };
}

function scopeEnvelope() {
  return { environment_id: scope.environmentId, schema_version: 1, trace_id: traceId, workspace_id: scope.workspaceId };
}

function healthEnvelope() {
  return {
    ...scopeEnvelope(),
    data: {
      authorization: { code: 'authorization.ready', status: 'ready' },
      build_version: 'test-build',
      ingestion: { code: 'ingestion.ready', status: 'ready' },
      providers: {},
      publication: { code: 'publication.ready', status: 'ready' },
      status: 'ready',
    },
  };
}

function renderPanel(initialEntry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(
    [{ path: '/knowledge/documents/:documentId', element: <DocumentDetailPage /> }],
    { initialEntries: [initialEntry] },
  );
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider value={{ status: 'authenticated', role: 'administrator', getBearerCredential: () => 'test-token' }}>
        <ScopeContext.Provider value={{ scope, queryKey: (resource) => ['test', resource, scope.workspaceId, scope.environmentId] }}>
          <RouterProvider router={router} />
        </ScopeContext.Provider>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

function documentEnvelope(processing: components['schemas']['IngestionItemState'] = 'processed') {
  return {
    data: {
      created_at: '2026-10-15T09:00:00Z',
      current_revision: null,
      operational_metadata: {
        freshness: 'current',
        health: 'healthy',
        lifecycle: 'current',
        processing_state: processing,
        publication_status: 'published',
        schema_version: 1,
        source_object_id: documentId,
        source_revision_id: '40000000-0000-4000-8000-000000000002',
        status: 'available',
      },
      publication: {
        freshness: 'current',
        health: 'healthy',
        projection_kind: 'qa',
        status: 'published',
      },
      schema_version: 1,
      source_kind: 'document',
      source_object_id: documentId,
    },
    environment_id: scope.environmentId,
    schema_version: 1,
    trace_id: traceId,
    workspace_id: scope.workspaceId,
  };
}
