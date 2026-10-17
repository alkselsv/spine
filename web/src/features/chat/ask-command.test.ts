import { afterEach, describe, expect, it, vi } from 'vitest';

import { createQaApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import { createAskCommandActions } from './ask-command';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const sessionId = '30000000-0000-4000-8000-000000000001';
const clarificationMessageId = '40000000-0000-4000-8000-000000000001';
const commandId = '50000000-0000-4000-8000-000000000001';
const messageId = '60000000-0000-4000-8000-000000000001';
const runId = '70000000-0000-4000-8000-000000000001';
const traceId = '80000000-0000-4000-8000-000000000001';

function acceptedResponse(overrides: Record<string, unknown> = {}) {
  return Response.json(
    {
      schema_version: 1,
      command_id: commandId,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      trace_id: traceId,
      status: 'accepted',
      replay: false,
      result: {
        schema_version: 1,
        command_id: commandId,
        session_id: sessionId,
        message_id: messageId,
        run_id: runId,
        status: 'admitted',
      },
      ...overrides,
    },
    { status: 202 },
  );
}

function clientFor(fetcher: typeof fetch, selectedScope: Scope = scope) {
  return createQaApiClient({
    baseUrl: 'https://spine.test',
    fetch: fetcher,
    getBearerCredential: () => 'test-token',
    scope: selectedScope,
  });
}

afterEach(() => vi.restoreAllMocks());

describe('authorized Ask command actions', () => {
  it('submits a scoped admission with a new idempotency identity', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(acceptedResponse());
    const actions = createAskCommandActions(clientFor(fetcher), scope, () => 'ask-key-1');

    const result = await actions.submit(sessionId, '  Where is the policy?  ');

    expect(result.kind).toBe('accepted');
    if (result.kind !== 'accepted') throw new Error('Expected accepted Ask');
    expect(result.admission.command_id).toBe(commandId);
    const request = fetcher.mock.calls[0]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe('/api/v1/ask/admissions');
    expect(request.headers.get('Idempotency-Key')).toBe('ask-key-1');
    expect(request.headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(scope.environmentId);
    expect(await request.json()).toEqual({
      schema_version: 1,
      session_id: sessionId,
      question: 'Where is the policy?',
    });
  });

  it('isolates successful Ask commands between workspaces', async () => {
    const otherScope: Scope = {
      workspaceId: '10000000-0000-4000-8000-000000000002',
      environmentId: '20000000-0000-4000-8000-000000000002',
    };
    const firstFetcher = vi.fn<typeof fetch>().mockResolvedValue(acceptedResponse());
    const secondFetcher = vi.fn<typeof fetch>().mockResolvedValue(
      acceptedResponse({
        workspace_id: otherScope.workspaceId,
        environment_id: otherScope.environmentId,
      }),
    );
    const firstActions = createAskCommandActions(
      clientFor(firstFetcher, scope),
      scope,
      () => 'ask-workspace-a',
    );
    const secondActions = createAskCommandActions(
      clientFor(secondFetcher, otherScope),
      otherScope,
      () => 'ask-workspace-b',
    );

    expect((await firstActions.submit(sessionId, 'Question')).kind).toBe('accepted');
    expect((await secondActions.submit(sessionId, 'Question')).kind).toBe('accepted');
    expect((firstFetcher.mock.calls[0]?.[0] as Request).headers.get('X-Workspace-ID')).toBe(
      scope.workspaceId,
    );
    expect((secondFetcher.mock.calls[0]?.[0] as Request).headers.get('X-Workspace-ID')).toBe(
      otherScope.workspaceId,
    );
    expect((firstFetcher.mock.calls[0]?.[0] as Request).headers.get('X-Environment-ID')).toBe(
      scope.environmentId,
    );
    expect((secondFetcher.mock.calls[0]?.[0] as Request).headers.get('X-Environment-ID')).toBe(
      otherScope.environmentId,
    );
  });

  it('fails closed when the server responds with another workspace scope', async () => {
    const otherScope: Scope = {
      workspaceId: '10000000-0000-4000-8000-000000000002',
      environmentId: '20000000-0000-4000-8000-000000000002',
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(acceptedResponse());
    const actions = createAskCommandActions(
      clientFor(fetcher, otherScope),
      otherScope,
      () => 'ask-scope-mismatch',
    );

    const result = await actions.submit(sessionId, 'Question');

    expect(result.kind).toBe('failed');
    if (result.kind !== 'failed') throw new Error('Expected safe failure');
    expect(result.message).toBe('Запрос недоступен в текущей рабочей области или среде.');
    expect(result.retryable).toBe(false);
  });

  it('retries an ambiguous failure with the original key and exact payload', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        Response.json(
          {
            code: 'spine.ask.unavailable',
            retryability: 'after_delay',
            safe_message: 'Safe unavailable message.',
            schema_version: 1,
            trace_id: traceId,
            provider_detail: 'private provider output',
          },
          { status: 503 },
        ),
      )
      .mockResolvedValueOnce(acceptedResponse());
    const actions = createAskCommandActions(clientFor(fetcher), scope, () => 'ask-key-stable');

    const first = await actions.submit(sessionId, 'Which policy applies?');
    const retry = await actions.retry();

    expect(first.kind).toBe('failed');
    expect(retry.kind).toBe('accepted');
    const firstRequest = fetcher.mock.calls[0]?.[0] as Request;
    const retryRequest = fetcher.mock.calls[1]?.[0] as Request;
    expect(firstRequest.headers.get('Idempotency-Key')).toBe('ask-key-stable');
    expect(retryRequest.headers.get('Idempotency-Key')).toBe('ask-key-stable');
    expect(await firstRequest.clone().json()).toEqual(await retryRequest.clone().json());
  });

  it('prevents a second concurrent submission from creating another command', async () => {
    let finishRequest!: (response: Response) => void;
    const fetcher = vi.fn<typeof fetch>().mockReturnValue(
      new Promise((resolve) => {
        finishRequest = resolve;
      }),
    );
    const actions = createAskCommandActions(clientFor(fetcher), scope, () => 'ask-key-once');

    const first = actions.submit(sessionId, 'Where is the policy?');
    const duplicate = await actions.submit(sessionId, 'Where is the policy?');
    finishRequest(acceptedResponse());
    const accepted = await first;

    expect(duplicate.kind).toBe('busy');
    expect(accepted.kind).toBe('accepted');
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('links a clarification to its source message and gives it a new identity', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(acceptedResponse());
    const nextKey = vi.fn().mockReturnValueOnce('ask-original').mockReturnValueOnce('ask-clarification');
    const actions = createAskCommandActions(clientFor(fetcher), scope, nextKey);

    await actions.submit(sessionId, 'Which policy?');
    await actions.submit(sessionId, 'The travel policy', clarificationMessageId);

    const original = fetcher.mock.calls[0]?.[0] as Request;
    const clarification = fetcher.mock.calls[1]?.[0] as Request;
    expect(original.headers.get('Idempotency-Key')).toBe('ask-original');
    expect(clarification.headers.get('Idempotency-Key')).toBe('ask-clarification');
    expect(await clarification.json()).toEqual({
      schema_version: 1,
      session_id: sessionId,
      question: 'The travel policy',
      clarification_of_message_id: clarificationMessageId,
    });
  });

  it('uses a distinct command identity when the question payload changes', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(acceptedResponse());
    const nextKey = vi.fn().mockReturnValueOnce('ask-first').mockReturnValueOnce('ask-changed');
    const actions = createAskCommandActions(clientFor(fetcher), scope, nextKey);

    await actions.submit(sessionId, 'First question');
    await actions.submit(sessionId, 'Changed question');

    const first = fetcher.mock.calls[0]?.[0] as Request;
    const changed = fetcher.mock.calls[1]?.[0] as Request;
    expect(first.headers.get('Idempotency-Key')).toBe('ask-first');
    expect(changed.headers.get('Idempotency-Key')).toBe('ask-changed');
    expect(await first.json()).toMatchObject({ question: 'First question' });
    expect(await changed.json()).toMatchObject({ question: 'Changed question' });
  });

  it('does not expose raw failure details or allow retry after a conflict', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      Response.json(
        {
          code: 'spine.persistence.idempotency_conflict',
          retryability: 'never',
          safe_message: 'Safe conflict.',
          schema_version: 1,
          trace_id: traceId,
          provider_detail: 'private provider output',
        },
        { status: 409 },
      ),
    );
    const actions = createAskCommandActions(clientFor(fetcher), scope, () => 'ask-conflict');

    const result = await actions.submit(sessionId, 'Question');
    const retry = await actions.retry();

    expect(result.kind).toBe('failed');
    if (result.kind !== 'failed') throw new Error('Expected safe failure');
    expect(result.message).not.toContain('private provider output');
    expect(retry.kind).toBe('not-retryable');
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('localizes a denied admission and does not expose protected error details', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      Response.json(
        {
          code: 'spine.authorization.denied',
          retryability: 'never',
          safe_message: 'Safe denial.',
          schema_version: 1,
          trace_id: traceId,
          provider_detail: 'private source excerpt',
        },
        { status: 403 },
      ),
    );
    const actions = createAskCommandActions(clientFor(fetcher), scope, () => 'ask-denied');

    const result = await actions.submit(sessionId, 'Question');

    expect(result.kind).toBe('failed');
    if (result.kind !== 'failed') throw new Error('Expected safe failure');
    expect(result.message).toBe('Запрос недоступен в текущей рабочей области или среде.');
    expect(result.message).not.toContain('private source excerpt');
    expect(result.retryable).toBe(false);
  });

  it('fails closed when the admission result does not match the submitted session', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      acceptedResponse({
        result: {
          schema_version: 1,
          command_id: commandId,
          session_id: '30000000-0000-4000-8000-000000000002',
          message_id: messageId,
          run_id: runId,
          status: 'admitted',
        },
      }),
    );
    const actions = createAskCommandActions(clientFor(fetcher), scope, () => 'ask-mismatch');

    const result = await actions.submit(sessionId, 'Question');

    expect(result.kind).toBe('failed');
    if (result.kind !== 'failed') throw new Error('Expected safe failure');
    expect(result.message).toBe('Не удалось принять запрос. Защищённые данные не раскрыты.');
    expect(result.retryable).toBe(false);
  });

  it('fails closed when the response uses a stale command status', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      acceptedResponse({ status: 'failed' }),
    );
    const actions = createAskCommandActions(clientFor(fetcher), scope, () => 'ask-stale');

    const result = await actions.submit(sessionId, 'Question');

    expect(result.kind).toBe('failed');
    if (result.kind !== 'failed') throw new Error('Expected safe failure');
    expect(result.message).toBe('Не удалось принять запрос. Защищённые данные не раскрыты.');
    expect(result.retryable).toBe(false);
  });

  it('does not send another Ask when the action state is observed after reconnect', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(acceptedResponse());
    const actions = createAskCommandActions(clientFor(fetcher), scope, () => 'ask-reconnect');

    await actions.submit(sessionId, 'Question');
    const state = actions.getState();

    expect(state.kind).toBe('accepted');
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
