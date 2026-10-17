import { afterEach, describe, expect, it, vi } from 'vitest';

import { createScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import { createAskCancellationActions } from './cancellation-api';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const runId = '30000000-0000-4000-8000-000000000001';
const commandId = '40000000-0000-4000-8000-000000000001';
const traceId = '50000000-0000-4000-8000-000000000001';

function cancelResponse(
  status: 'cancellation_requested' | 'terminal',
  outcome: 'answered' | 'abstained' | 'clarification_required' | 'denied' | 'failed' | 'cancelled' | null,
  httpStatus: number,
) {
  return Response.json(
    {
      schema_version: 1,
      command_id: commandId,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      trace_id: traceId,
      status: httpStatus === 202 ? 'accepted' : 'completed',
      replay: false,
      result: { schema_version: 1, command_id: commandId, run_id: runId, status, outcome },
    },
    { status: httpStatus },
  );
}

function clientFor(fetcher: typeof fetch, selectedScope: Scope = scope) {
  return createScopedApiClient({
    baseUrl: 'https://spine.test',
    fetch: fetcher,
    getBearerCredential: () => 'test-token',
    scope: selectedScope,
  });
}

afterEach(() => vi.restoreAllMocks());

describe('active Q&A cancellation actions', () => {
  it('requests cancellation for the exact scoped run with the approved versioned contract', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() =>
      Promise.resolve(cancelResponse('cancellation_requested', null, 202)),
    );
    const actions = createAskCancellationActions(clientFor(fetcher), scope, () => 'cancel-key-1');

    const result = await actions.cancel(runId);

    expect(result).toEqual({ kind: 'requested', runId });
    const request = fetcher.mock.calls[0]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe(`/api/v1/ask/${runId}/cancel`);
    expect(request.headers.get('Accept')).toBe('application/vnd.spine.qa-cancel+json;version=1');
    expect(request.headers.get('Idempotency-Key')).toBe('cancel-key-1');
    expect(request.headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(scope.environmentId);
    expect(await request.json()).toEqual({ schema_version: 1 });
  });

  it('returns a content-free canonical terminal outcome when completion wins the race', async () => {
    const terminalResponse = cancelResponse('terminal', 'answered', 200);
    const payload = await terminalResponse.json();
    payload.answer = 'protected answer content';
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(payload, { status: 200 }));
    const actions = createAskCancellationActions(clientFor(fetcher), scope, () => 'cancel-key-race');

    const result = await actions.cancel(runId);

    expect(result).toEqual({ kind: 'terminal', runId, outcome: 'answered' });
    expect(JSON.stringify(result)).not.toContain('protected answer content');
  });

  it('reports the canonical cancelled outcome when cancellation wins the race', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      cancelResponse('terminal', 'cancelled', 200),
    );
    const actions = createAskCancellationActions(clientFor(fetcher), scope, () => 'cancel-key-wins');

    expect(await actions.cancel(runId)).toEqual({ kind: 'terminal', runId, outcome: 'cancelled' });
  });

  it('reuses the same cancellation identity for retries and repeated requests', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        Response.json(
          { code: 'spine.ask.unavailable', retryability: 'after_delay', safe_message: 'Safe', schema_version: 1, trace_id: traceId },
          { status: 503 },
        ),
      )
      .mockResolvedValue(cancelResponse('cancellation_requested', null, 202));
    const actions = createAskCancellationActions(clientFor(fetcher), scope, () => 'cancel-key-stable');

    expect((await actions.cancel(runId)).kind).toBe('failed');
    expect(await actions.retry()).toEqual({ kind: 'requested', runId });
    expect(await actions.cancel(runId)).toEqual({ kind: 'requested', runId });
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls.map(([request]) => (request as Request).headers.get('Idempotency-Key')))
      .toEqual(['cancel-key-stable', 'cancel-key-stable']);
  });

  it('uses a run-stable identity when cancellation is retried after control recreation', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() =>
      Promise.resolve(cancelResponse('cancellation_requested', null, 202)),
    );
    const firstControl = createAskCancellationActions(clientFor(fetcher), scope);
    const recreatedControl = createAskCancellationActions(clientFor(fetcher), scope);

    expect((await firstControl.cancel(runId)).kind).toBe('requested');
    expect((await recreatedControl.cancel(runId)).kind).toBe('requested');
    const keys = fetcher.mock.calls.map(([request]) => (request as Request).headers.get('Idempotency-Key'));
    expect(keys).toEqual([`qa-cancel-${runId}`, `qa-cancel-${runId}`]);
  });

  it('fails closed when the response names a different run', async () => {
    const response = cancelResponse('cancellation_requested', null, 202);
    const payload = await response.json();
    payload.result.run_id = '30000000-0000-4000-8000-000000000099';
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(payload, { status: 202 }));
    const actions = createAskCancellationActions(clientFor(fetcher), scope, () => 'cancel-mismatch');

    expect(await actions.cancel(runId)).toEqual({ kind: 'failed', message: 'Не удалось запросить остановку. Состояние ответа не изменено.', retryable: false });
  });

  it('fails closed when the terminal result belongs to a stale workspace scope', async () => {
    const response = cancelResponse('cancellation_requested', null, 202);
    const payload = await response.json();
    payload.environment_id = '20000000-0000-4000-8000-000000000099';
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(payload, { status: 202 }));
    const actions = createAskCancellationActions(clientFor(fetcher), scope, () => 'cancel-stale-scope');

    expect(await actions.cancel(runId)).toEqual({
      kind: 'failed',
      message: 'Не удалось запросить остановку. Состояние ответа не изменено.',
      retryable: false,
    });
  });

  it('does not expose authorization or provider details in failures', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      Response.json(
        { code: 'spine.authorization.denied', retryability: 'never', safe_message: 'Private detail', provider_error: 'secret', schema_version: 1, trace_id: traceId },
        { status: 403 },
      ),
    );
    const actions = createAskCancellationActions(clientFor(fetcher), scope, () => 'cancel-denied');

    const result = await actions.cancel(runId);

    expect(result).toEqual({ kind: 'failed', message: 'Отмена недоступна в текущей рабочей области или среде.', retryable: false });
    expect(JSON.stringify(result)).not.toContain('Private');
    expect(JSON.stringify(result)).not.toContain('secret');
  });
});
