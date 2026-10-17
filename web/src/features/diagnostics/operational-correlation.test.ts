import { afterEach, describe, expect, it, vi } from 'vitest';

import type { components } from '../../../api/generated/schema';
import { createScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import { loadOperationalCorrelation } from './operational-correlation';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const runId = 'a0000000-0000-4000-8000-000000000001';
const traceId = '50000000-0000-4000-8000-000000000001';

afterEach(() => vi.unstubAllGlobals());

describe('operational correlation API', () => {
  it('test_correlation_read_uses_selected_tenant_and_returns_the_requested_safe_view', async () => {
    const fetchMock = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json({
        authorization_audit_event_id: '70000000-0000-4000-8000-000000000001',
        data: makeView(),
        environment_id: scope.environmentId,
        schema_version: 1,
        trace_id: traceId,
        workspace_id: scope.workspaceId,
      }),
    );
    const client = createClient(fetchMock);

    const view = await loadOperationalCorrelation(
      client,
      { referenceId: runId, referenceKind: 'run', scope },
      new AbortController().signal,
    );

    expect(view.view.reference_id).toBe(runId);
    expect(view.view.coverage).toBe('incomplete');
    expect(view.authorizationAuditEventId).toBe('70000000-0000-4000-8000-000000000001');
    const [requestUrl, requestInit] = fetchMock.mock.calls[0] ?? [];
    const resolvedUrl = requestUrl instanceof Request ? requestUrl.url : String(requestUrl);
    expect(new URL(resolvedUrl, 'https://spine.example.test').searchParams.toString()).toBe(
      `reference_kind=run&reference_id=${runId}`,
    );
    const headers = new Headers(
      requestInit?.headers ?? (requestUrl instanceof Request ? requestUrl.headers : undefined),
    );
    expect(headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect(headers.get('X-Environment-ID')).toBe(scope.environmentId);
    expect(headers.get('Authorization')).toBe('Bearer test-token');
  });

  it('test_correlation_read_rejects_a_response_for_another_scope_without_returning_its_records', async () => {
    const foreignView = { ...makeView(), workspace_id: '10000000-0000-4000-8000-000000000099' };
    const fetchMock = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json({
        authorization_audit_event_id: '70000000-0000-4000-8000-000000000001',
        data: foreignView,
        environment_id: scope.environmentId,
        schema_version: 1,
        trace_id: traceId,
        workspace_id: scope.workspaceId,
      }),
    );

    await expect(
      loadOperationalCorrelation(
        createClient(fetchMock),
        { referenceId: runId, referenceKind: 'run', scope },
        new AbortController().signal,
      ),
    ).rejects.toThrow('outside the selected scope');
  });

  it('test_correlation_read_rejects_a_response_for_a_different_reference', async () => {
    const fetchMock = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json({
        authorization_audit_event_id: '70000000-0000-4000-8000-000000000001',
        data: { ...makeView(), reference_id: 'a0000000-0000-4000-8000-000000000099' },
        environment_id: scope.environmentId,
        schema_version: 1,
        trace_id: traceId,
        workspace_id: scope.workspaceId,
      }),
    );

    await expect(
      loadOperationalCorrelation(
        createClient(fetchMock),
        { referenceId: runId, referenceKind: 'run', scope },
        new AbortController().signal,
      ),
    ).rejects.toThrow('does not match the requested reference');
  });
});

function createClient(fetchMock: typeof globalThis.fetch) {
  return createScopedApiClient({
    baseUrl: 'https://spine.example.test',
    fetch: fetchMock,
    getBearerCredential: () => 'test-token',
    scope,
  });
}

function makeView(): components['schemas']['OperationalCorrelationView'] {
  return {
    conflict_codes: [],
    correlation_ids: [],
    coverage: 'incomplete',
    environment_id: scope.environmentId,
    lineage: [],
    missing_sources: ['delivery_metadata'],
    records: [],
    reference_id: runId,
    reference_kind: 'run',
    schema_version: 'operational-correlation-view:v1',
    trace_ids: [traceId],
    workspace_id: scope.workspaceId,
  };
}
