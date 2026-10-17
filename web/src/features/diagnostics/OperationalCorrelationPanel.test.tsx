import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createMemoryRouter, RouterProvider } from 'react-router';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';

import type { components } from '../../../api/generated/schema';
import { createScopedApiClient } from '../../api/client';
import { AuthProvider } from '../../app/auth';
import { ScopeContext, type Scope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import { OperationalCorrelationPanel } from './OperationalCorrelationPanel';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const runId = 'a0000000-0000-4000-8000-000000000001';
const traceId = '50000000-0000-4000-8000-000000000001';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('operational correlation panel', () => {
  it('test_correlation_panel_shows_safe_partial_diagnostics_and_scoped_cross_feature_links', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(makeResponse())));

    renderPanel(runId);
    await userEvent.click(screen.getByRole('button', { name: ru.diagnostics.open }));

    expect(await screen.findByText(ru.diagnostics.partial)).toBeVisible();
    expect(screen.getByText(ru.diagnostics.sources.canonical_run)).toBeVisible();
    expect(screen.getByText('failed')).toBeVisible();
    expect(screen.getAllByText(`${traceId.slice(0, 8)}…`)).toHaveLength(2);
    expect(screen.getByRole('link', { name: ru.diagnostics.qaRuns })).toHaveAttribute(
      'href',
      `/qa/runs?workspace=${scope.workspaceId}&environment=${scope.environmentId}`,
    );
    expect(screen.getByRole('link', { name: ru.diagnostics.ingestionRuns })).toHaveAttribute(
      'href',
      `/runs?workspace=${scope.workspaceId}&environment=${scope.environmentId}`,
    );
    expect(document.body).not.toHaveTextContent('protected answer text');
    expect(document.body).not.toHaveTextContent('provider stack trace');
  });

  it('test_correlation_panel_renders_unavailable_coverage_without_fabricating_records', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(makeResponse({
        coverage: 'unavailable',
        missing_sources: ['operational_metric', 'delivery_metadata', 'canonical_run', 'usefulness_feedback'],
        records: [],
      }))),
    );

    renderPanel(runId);
    await userEvent.click(screen.getByRole('button', { name: ru.diagnostics.open }));

    expect(await screen.findByText(ru.diagnostics.unavailable)).toBeVisible();
    expect(screen.queryByText(ru.diagnostics.partial)).not.toBeInTheDocument();
    expect(screen.queryByRole('list', { name: ru.diagnostics.records })).not.toBeInTheDocument();
  });

  it('test_correlation_panel_explains_complete_results_without_records', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(makeResponse({
        coverage: 'complete',
        missing_sources: [],
        records: [],
      }))),
    );

    renderPanel(runId);
    await userEvent.click(screen.getByRole('button', { name: ru.diagnostics.open }));

    expect(await screen.findByText(ru.status.empty.title)).toBeVisible();
    expect(screen.getByRole('list', { name: ru.diagnostics.records })).toBeEmptyDOMElement();
  });

  it('test_correlation_panel_hides_protected_api_diagnostics_on_access_denial', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockResolvedValue(
        Response.json(
          {
            code: 'spine.authorization.denied',
            retryability: 'never',
            safe_message: 'Access denied.',
            schema_version: 1,
            trace_id: traceId,
            provider_error: 'protected answer text and provider stack trace',
          },
          { status: 403 },
        ),
      ),
    );

    renderPanel(runId);
    await userEvent.click(screen.getByRole('button', { name: ru.diagnostics.open }));

    expect(await screen.findByRole('alert')).toHaveTextContent(ru.errors.authorization.title);
    expect(document.body).not.toHaveTextContent('protected answer text');
    expect(document.body).not.toHaveTextContent('provider stack trace');
    expect(screen.getByRole('button', { name: ru.documents.retry })).toBeVisible();
  });

  it('test_correlation_panel_drops_cached_records_when_access_is_revoked', async () => {
    const fetchMock = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(makeResponse()))
      .mockResolvedValue(
        Response.json(
          {
            code: 'spine.authorization.denied',
            retryability: 'never',
            safe_message: 'Access denied.',
            schema_version: 1,
            trace_id: traceId,
            provider_error: 'protected answer text',
          },
          { status: 403 },
        ),
      );
    vi.stubGlobal('fetch', fetchMock);

    renderPanel(runId);
    await userEvent.click(screen.getByRole('button', { name: ru.diagnostics.open }));
    expect(await screen.findByText(ru.diagnostics.partial)).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: ru.diagnostics.close }));
    await userEvent.click(screen.getByRole('button', { name: ru.diagnostics.open }));

    expect(await screen.findByRole('alert')).toHaveTextContent(ru.errors.authorization.title);
    expect(screen.queryByText(ru.diagnostics.partial)).not.toBeInTheDocument();
    expect(screen.queryByText(ru.diagnostics.sources.canonical_run)).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('protected answer text');
  });

  it('test_correlation_panel_revalidates_and_hides_previous_identity_data_after_identity_change', async () => {
    const fetchMock = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(makeResponse()))
      .mockResolvedValue(
        Response.json(
          {
            code: 'spine.authorization.denied',
            retryability: 'never',
            safe_message: 'Access denied.',
            schema_version: 1,
            trace_id: traceId,
          },
          { status: 403 },
        ),
      );
    vi.stubGlobal('fetch', fetchMock);

    const { setIdentityVersion } = renderPanel(runId);
    await userEvent.click(screen.getByRole('button', { name: ru.diagnostics.open }));
    expect(await screen.findByText(ru.diagnostics.sources.canonical_run)).toBeVisible();

    setIdentityVersion('principal-b');

    expect(await screen.findByRole('alert')).toHaveTextContent(ru.errors.authorization.title);
    expect(screen.queryByText(ru.diagnostics.sources.canonical_run)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('test_correlation_panel_switches_reference_without_rendering_previous_run_signals', async () => {
    const nextRunId = 'a0000000-0000-4000-8000-000000000002';
    const fetchMock = vi.fn<typeof globalThis.fetch>().mockImplementation(async (input) => {
      const url = new URL(input instanceof Request ? input.url : String(input));
      return url.searchParams.get('reference_id') === nextRunId
        ? Response.json(makeResponse({ coverage: 'complete', records: [], reference_id: nextRunId }))
        : Response.json(makeResponse());
    });
    vi.stubGlobal('fetch', fetchMock);

    const { setReferenceId } = renderPanel(runId);
    await userEvent.click(screen.getByRole('button', { name: ru.diagnostics.open }));
    expect(await screen.findByText(ru.diagnostics.sources.canonical_run)).toBeVisible();

    setReferenceId(nextRunId);

    expect(await screen.findByText(ru.status.empty.title)).toBeVisible();
    expect(screen.queryByText(ru.diagnostics.sources.canonical_run)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const switchedRequest = new URL(fetchMock.mock.calls[1]![0] instanceof Request
      ? fetchMock.mock.calls[1]![0].url
      : String(fetchMock.mock.calls[1]![0]));
    expect(switchedRequest.searchParams.get('reference_id')).toBe(nextRunId);
  });

  it('test_correlation_panel_allows_retry_after_a_transient_read_failure', async () => {
    let resolveRetry: ((response: Response) => void) | undefined;
    const fetchMock = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(
        {
          code: 'spine.validation.failed',
          retryability: 'never',
          safe_message: 'Invalid request.',
          schema_version: 1,
          trace_id: traceId,
        },
        { status: 400 },
      ))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveRetry = resolve; }));
    vi.stubGlobal('fetch', fetchMock);

    renderPanel(runId);
    await userEvent.click(screen.getByRole('button', { name: ru.diagnostics.open }));
    expect(await screen.findByRole('alert')).toHaveTextContent(ru.errors.validation.title);
    await userEvent.click(screen.getByRole('button', { name: ru.documents.retry }));
    expect(await screen.findByText(ru.diagnostics.reconnecting)).toBeVisible();
    resolveRetry?.(Response.json(makeResponse()));

    expect(await screen.findByText(ru.diagnostics.partial)).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('test_correlation_panel_drills_into_a_correlated_delivery_and_preserves_scope', async () => {
    const deliveryId = 'b0000000-0000-4000-8000-000000000001';
    const fetchMock = vi.fn<typeof globalThis.fetch>().mockImplementation(async (input) => {
      const url = new URL(input instanceof Request ? input.url : String(input));
      return Response.json(
        url.searchParams.get('reference_kind') === 'delivery'
          ? makeDeliveryResponse(deliveryId)
          : makeResponse({}, deliveryId),
      );
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPanel(runId);
    await userEvent.click(screen.getByRole('button', { name: ru.diagnostics.open }));
    expect(await screen.findByText(ru.diagnostics.partial)).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: `${ru.diagnostics.inspectDelivery}: ${deliveryId.slice(0, 8)}…` }));

    expect(await screen.findByText(ru.diagnostics.sources.delivery_metadata)).toBeVisible();
    expect(screen.getByText('outbox.failed')).toBeVisible();
    const deliveryRequest = fetchMock.mock.calls
      .map(([input]) => new URL(input instanceof Request ? input.url : String(input)))
      .find((url) => url.searchParams.get('reference_kind') === 'delivery');
    expect(deliveryRequest?.searchParams.get('reference_id')).toBe(deliveryId);
    expect(screen.getByRole('button', { name: ru.diagnostics.backToOriginalReference })).toBeVisible();
  });

  it('test_correlation_panel_does_not_query_an_invalid_correlation_reference', async () => {
    const fetchMock = vi.fn<typeof globalThis.fetch>();
    vi.stubGlobal('fetch', fetchMock);

    renderPanel('not-a-uuid');
    await userEvent.click(screen.getByRole('button', { name: ru.diagnostics.open }));

    expect(screen.getByText(ru.diagnostics.referenceUnavailable)).toBeVisible();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

function renderPanel(referenceId: string) {
  const client = createScopedApiClient({
    baseUrl: 'https://spine.example.test',
    getBearerCredential: () => 'test-token',
    scope,
  });
  let updateReferenceId: (nextReferenceId: string) => void = () => undefined;
  function PanelHarness() {
    const [currentReferenceId, setCurrentReferenceId] = useState(referenceId);
    updateReferenceId = setCurrentReferenceId;
    return (
      <OperationalCorrelationPanel
        client={client}
        queryKey={(resource) => ['spine', resource, scope.workspaceId, scope.environmentId]}
        referenceId={currentReferenceId}
        referenceKind="run"
        scope={scope}
      />
    );
  }
  const router = createMemoryRouter(
    [{ path: '/diagnostics', element: <PanelHarness /> }],
    { initialEntries: ['/diagnostics'] },
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const renderTree = (identityVersion: string) => (
    <AuthProvider value={{ status: 'authenticated', role: 'administrator', identityVersion }}>
      <QueryClientProvider client={queryClient}>
        <ScopeContext.Provider
          value={{ scope, queryKey: (resource) => ['spine', resource, scope.workspaceId, scope.environmentId] }}
        >
          <RouterProvider router={router} />
        </ScopeContext.Provider>
      </QueryClientProvider>
    </AuthProvider>
  );
  const mounted = render(renderTree('principal-a'));
  return {
    setIdentityVersion: (identityVersion: string) => mounted.rerender(renderTree(identityVersion)),
    setReferenceId: (nextReferenceId: string) => updateReferenceId(nextReferenceId),
  };
}

function makeResponse(
  viewOverrides: Partial<components['schemas']['OperationalCorrelationView']> = {},
  deliveryEventId: string | null = null,
): components['schemas']['OperationalCorrelationResponse'] {
  return {
    authorization_audit_event_id: '70000000-0000-4000-8000-000000000001',
    data: {
      conflict_codes: [],
      correlation_ids: [],
      coverage: 'incomplete',
      environment_id: scope.environmentId,
      lineage: [],
      missing_sources: ['delivery_metadata'],
      records: [
        {
          correlation_id: null,
          deduplication_key: `run:${runId}`,
          delivery_event_id: deliveryEventId,
          delivery_last_error_code: null,
          delivery_status: null,
          environment_id: scope.environmentId,
          feedback_id: null,
          feedback_outcome: null,
          feedback_target_kind: null,
          message_id: '90000000-0000-4000-8000-000000000001',
          metric_coverage: null,
          metric_currency: null,
          metric_denominator: null,
          metric_measurement: null,
          metric_numerator: null,
          metric_value: null,
          observed_at: '2026-10-16T10:00:00Z',
          run_id: runId,
          run_outcome: 'failed',
          run_state: 'completed',
          schema_version: 'operational-correlation-record:v1',
          source_identity: 'canonical.answer-question-run:v1',
          source_kind: 'canonical_run',
          source_record_id: runId,
          source_schema_version: 'answer-question-run:v1',
          trace_id: traceId,
          workspace_id: scope.workspaceId,
        },
      ],
      reference_id: runId,
      reference_kind: 'run',
      schema_version: 'operational-correlation-view:v1',
      trace_ids: [traceId],
      workspace_id: scope.workspaceId,
      ...viewOverrides,
    },
    environment_id: scope.environmentId,
    schema_version: 1,
    trace_id: traceId,
    workspace_id: scope.workspaceId,
  };
}

function makeDeliveryResponse(
  deliveryId: string,
): components['schemas']['OperationalCorrelationResponse'] {
  return makeResponse({
    coverage: 'complete',
    missing_sources: [],
    records: [{
      deduplication_key: `delivery:${deliveryId}`,
      delivery_attempt_count: 3,
      delivery_event_id: deliveryId,
      delivery_last_error_code: 'outbox.failed',
      delivery_quarantined_at: '2026-10-16T10:00:00Z',
      delivery_status: 'quarantined',
      environment_id: scope.environmentId,
      observed_at: '2026-10-16T10:00:00Z',
      schema_version: 'operational-correlation-record:v1',
      source_identity: 'o06.outbox-delivery:v1',
      source_kind: 'delivery_metadata',
      source_record_id: deliveryId,
      source_schema_version: 'outbox-delivery:v1',
      workspace_id: scope.workspaceId,
    }],
    reference_id: deliveryId,
    reference_kind: 'delivery',
  });
}
