import { describe, expect, it, vi } from 'vitest';
import type { Client } from 'openapi-fetch';

import {
  createQaApiClient,
  createScopedApiClient,
  type ScopedPaths,
  SpineApiError,
} from './client';

const workspaceId = '20000000-0000-4000-8000-000000000001';
const environmentId = '30000000-0000-4000-8000-000000000001';
const traceId = '50000000-0000-4000-8000-000000000001';

function createClient(fetch: typeof globalThis.fetch) {
  return createScopedApiClient({
    baseUrl: 'https://spine.test',
    fetch,
    getBearerCredential: () => 'injected-secret',
    scope: { environmentId, workspaceId },
  });
}

describe('scoped API client', () => {
  it('test_scoped_type_contract_omits_generated_scope_headers', () => {
    type ProtectedClient = Client<ScopedPaths<ProtectedPathsFixture>>;
    const protectedClient = null as unknown as ProtectedClient;

    if (false) {
      void protectedClient.GET('/api/v1/protected');
    }

    expect(true).toBe(true);
  });

  it('test_request_applies_injected_bearer_and_required_scope_headers', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json({ status: 'ok' }),
    );

    const client = createClient(fetch);
    await client.GET('/api/v1/health/live');

    const request = fetch.mock.calls[0]?.[0];
    expect(request).toBeInstanceOf(Request);
    expect((request as Request).headers.get('Authorization')).toBe(
      'Bearer injected-secret',
    );
    expect((request as Request).headers.get('X-Workspace-ID')).toBe(workspaceId);
    expect((request as Request).headers.get('X-Environment-ID')).toBe(
      environmentId,
    );
    expect((request as Request).url).not.toContain('injected-secret');
    expect((request as Request).redirect).toBe('error');
  });

  it('test_request_rejects_an_origin_override_before_reading_credential', async () => {
    const getBearerCredential = vi.fn(() => 'injected-secret');
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch: vi.fn(),
      getBearerCredential,
      scope: { environmentId, workspaceId },
    });

    await expect(
      client.GET('/api/v1/health/live', { baseUrl: 'https://attacker.test' }),
    ).rejects.toMatchObject({ code: 'spine.client.invalid_request' });
    expect(getBearerCredential).not.toHaveBeenCalled();
  });

  it('test_client_rejects_per_request_transport_overrides_before_credentials', async () => {
    const getBearerCredential = vi.fn(() => 'injected-secret');
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch: vi.fn(),
      getBearerCredential,
      scope: { environmentId, workspaceId },
    });

    expect('use' in client).toBe(false);
    await expect(
      client.GET('/api/v1/health/live', {
        fetch: vi.fn(),
        middleware: [{ onRequest: ({ request }) => request }],
      }),
    ).rejects.toMatchObject({ code: 'spine.client.invalid_request' });
    expect(getBearerCredential).not.toHaveBeenCalled();
  });

  it('test_credential_provider_failure_is_sanitized', async () => {
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch: vi.fn(),
      getBearerCredential: () => {
        throw new Error('oidc provider detail with raw-secret');
      },
      scope: { environmentId, workspaceId },
    });

    const request = client.GET('/api/v1/health/live');

    await expect(request).rejects.toMatchObject({
      code: 'spine.client.invalid_credential',
      kind: 'authentication',
    });
    await expect(request).rejects.not.toThrow('raw-secret');
    await expect(request).rejects.not.toThrow('oidc provider');
  });

  it('test_malformed_credential_is_rejected_before_transport', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch,
      getBearerCredential: () => 'secret\r\nX-Injected: value',
      scope: { environmentId, workspaceId },
    });

    await expect(client.GET('/api/v1/health/live')).rejects.toMatchObject({
      code: 'spine.client.invalid_credential',
    });
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([
    ['notaurl', 'spine.client.invalid_configuration'],
    ['https://user:secret@spine.test', 'spine.client.invalid_configuration'],
  ])('test_invalid_base_url_%s_is_rejected', (baseUrl, code) => {
    expect(() =>
      createScopedApiClient({
        baseUrl,
        getBearerCredential: () => 'secret',
        scope: { environmentId, workspaceId },
      }),
    ).toThrow(expect.objectContaining({ code }));
  });

  it.each([
    ['invalid-workspace', environmentId],
    [workspaceId, 'invalid-environment'],
  ])(
    'test_invalid_scope_%s_%s_is_rejected',
    (invalidWorkspaceId, invalidEnvironmentId) => {
      expect(() =>
        createScopedApiClient({
          baseUrl: 'https://spine.test',
          getBearerCredential: () => 'secret',
          scope: {
            environmentId: invalidEnvironmentId,
            workspaceId: invalidWorkspaceId,
          },
        }),
      ).toThrow(expect.objectContaining({ code: 'spine.client.invalid_scope' }));
    },
  );

  it.each([
    [401, 'spine.authentication.invalid', 'authentication'],
    [403, 'spine.authorization.denied', 'authorization'],
    [422, 'spine.request.validation', 'validation'],
  ] as const)(
    'test_structured_failure_%s_maps_to_safe_distinguishable_outcome',
    async (status, code, kind) => {
      const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
        Response.json(
          {
            code,
            provider_detail: 'secret-provider-detail',
            retryability: 'never',
            safe_message: 'Safe public message.',
            schema_version: 1,
            stack: 'raw-server-stack',
            trace_id: traceId,
          },
          { status },
        ),
      );

      const request = createClient(fetch).GET('/api/v1/health/live');

      await expect(request).rejects.toMatchObject({
        code,
        kind,
        retryability: 'never',
        safeMessage: 'Safe public message.',
        status,
        traceId,
      });
      await expect(request).rejects.not.toThrow('secret-provider-detail');
      await expect(request).rejects.not.toThrow('raw-server-stack');
    },
  );

  it('test_denied_cross_workspace_fixtures_do_not_disclose_text_or_existence', async () => {
    const outcomes = await Promise.all(
      [
        { protected_text: 'customer alpha exists', resource_exists: true },
        { protected_text: 'customer beta is absent', resource_exists: false },
      ].map(async (protectedFields) => {
        const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
          Response.json(
            {
              code: 'spine.authorization.denied',
              retryability: 'never',
              safe_message: 'Authorization is denied.',
              schema_version: 1,
              trace_id: traceId,
              ...protectedFields,
            },
            { status: 403 },
          ),
        );

        try {
          await createClient(fetch).GET('/api/v1/health/live');
        } catch (error) {
          expect(error).toBeInstanceOf(SpineApiError);
          return JSON.stringify(error);
        }
        throw new Error('Expected request denial');
      }),
    );

    expect(outcomes[0]).toBe(outcomes[1]);
    expect(outcomes.join(' ')).not.toContain('customer');
    expect(outcomes.join(' ')).not.toContain('resource_exists');
  });

  it('test_success_response_with_mismatched_scope_fails_closed_without_protected_content', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json({
        schema_version: 1,
        workspace_id: '20000000-0000-4000-8000-000000000002',
        environment_id: environmentId,
        trace_id: traceId,
        data: { answer: 'protected answer from another workspace' },
      }),
    );

    const request = createQaClient(fetch).GET('/api/v1/qa/sessions');

    await expect(request).rejects.toMatchObject({
      code: 'spine.client.scope_mismatch',
      kind: 'authorization',
    });
    await expect(request).rejects.not.toThrow('protected answer');
    await expect(request).rejects.not.toThrow('another workspace');
  });

  it('test_unavailable_api_maps_provider_failure_without_leaking_details', async () => {
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockRejectedValue(new Error('dial tcp provider.internal bearer-secret'));

    const request = createClient(fetch).GET('/api/v1/health/live');

    await expect(request).rejects.toMatchObject({
      code: 'spine.client.api_unavailable',
      kind: 'unavailable',
      retryability: 'after_delay',
    });
    await expect(request).rejects.not.toThrow('provider.internal');
    await expect(request).rejects.not.toThrow('bearer-secret');
  });

  it.each([
    [401, 'spine.authentication.invalid', 'authentication'],
    [403, 'spine.authorization.denied', 'authorization'],
    [422, 'spine.request.validation', 'validation'],
    [503, 'spine.client.api_unavailable', 'unavailable'],
    [409, 'spine.client.request_failed', 'request'],
  ] as const)(
    'test_unstructured_failure_%s_uses_safe_fallback',
    async (status, code, kind) => {
      const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
        new Response('raw protected provider failure', {
          headers: { 'X-Trace-ID': traceId },
          status,
        }),
      );

      const request = createClient(fetch).GET('/api/v1/health/live');

      await expect(request).rejects.toMatchObject({ code, kind, status, traceId });
      await expect(request).rejects.not.toThrow('raw protected provider failure');
    },
  );

  it('test_request_cancellation_preserves_abort_outcome', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(
      (request) =>
        new Promise((_resolve, reject) => {
          (request as Request).signal.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted.', 'AbortError'));
          });
        }),
    );
    const controller = new AbortController();
    const request = createClient(fetch).GET('/api/v1/health/live', {
      signal: controller.signal,
    });

    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    controller.abort();

    await expect(request).rejects.toMatchObject({ name: 'AbortError' });
    await expect(request).rejects.not.toBeInstanceOf(SpineApiError);
  }, 10_000);
});

describe('published Q&A API client', () => {
  it('test_qa_type_boundary_contains_only_published_qa_routes', () => {
    const client = createQaApiClient({
      baseUrl: 'https://spine.test',
      fetch: vi.fn(),
      getBearerCredential: () => 'test-token',
      scope: { environmentId, workspaceId },
    });

    if (false) {
      void client.GET('/api/v1/qa/sessions');
      // @ts-expect-error Knowledge and health routes are outside the Q&A boundary.
      void client.GET('/api/v1/health/live');
      // @ts-expect-error Active cancellation is owned by its approved adapter ticket.
      void client.POST('/api/v1/ask/{run_id}/cancel');
    }

    expect('DELETE' in client).toBe(false);
  });

  it('test_authorized_ask_preserves_exact_generated_body_idempotency_and_scope', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json(commandEnvelope({
        attempt_id: '71000000-0000-4000-8000-000000000001',
        message_id: '72000000-0000-4000-8000-000000000001',
        outcome: 'abstained',
        reason: 'no_usable_evidence',
        run_id: '73000000-0000-4000-8000-000000000001',
        schema_version: 1,
        session_id: '74000000-0000-4000-8000-000000000001',
      })),
    );
    const client = createQaClient(fetch);

    await client.POST('/api/v1/ask', {
      params: { header: { 'Idempotency-Key': 'ask-stable-key' } },
      body: {
        clarification_of_message_id: null,
        question: 'Каков срок?',
        schema_version: 1,
        session_id: '74000000-0000-4000-8000-000000000001',
      },
    });

    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(request.url).toBe('https://spine.test/api/v1/ask');
    expect(request.headers.get('Idempotency-Key')).toBe('ask-stable-key');
    expect(request.headers.get('X-Workspace-ID')).toBe(workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(environmentId);
    await expect(request.json()).resolves.toEqual({
      clarification_of_message_id: null,
      question: 'Каков срок?',
      schema_version: 1,
      session_id: '74000000-0000-4000-8000-000000000001',
    });
  });

  it('test_sse_resume_uses_generated_path_query_and_last_event_id', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      new Response('id: event-2\nevent: heartbeat\ndata: {}\n\n', {
        headers: { 'Content-Type': 'text/event-stream' },
      }),
    );
    const client = createQaClient(fetch);

    const response = await client.GET(
      '/api/v1/qa/sessions/{session_id}/messages/{message_id}/events',
      {
        params: {
          path: { session_id: 'session-1', message_id: 'message-1' },
          query: { run_id: 'run-1', last_sequence: 1, limit: 100, poll: false },
          header: { 'Last-Event-ID': 'event-1' },
        },
      },
    );

    const request = fetch.mock.calls[0]?.[0] as Request;
    const url = new URL(request.url);
    expect(url.pathname).toBe('/api/v1/qa/sessions/session-1/messages/message-1/events');
    expect(Object.fromEntries(url.searchParams)).toEqual({
      last_sequence: '1',
      limit: '100',
      poll: 'false',
      run_id: 'run-1',
    });
    expect(request.headers.get('Last-Event-ID')).toBe('event-1');
    expect(response.data).toContain('event: heartbeat');
  });

  it('test_feedback_preserves_exact_generated_target_and_idempotency', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json(commandEnvelope({
        feedback_id: '75000000-0000-4000-8000-000000000001',
        feedback_version: 1,
        outcome: 'citation.incorrect',
        run_id: '73000000-0000-4000-8000-000000000001',
        schema_version: 1,
        status: 'recorded',
        target_id: '76000000-0000-4000-8000-000000000001',
        target_kind: 'citation',
      })),
    );
    const client = createQaClient(fetch);

    await client.POST('/api/v1/qa/feedback', {
      params: { header: { 'Idempotency-Key': 'feedback-stable-key' } },
      body: {
        citation_id: '76000000-0000-4000-8000-000000000001',
        comment: 'Неверная ссылка',
        message_id: '72000000-0000-4000-8000-000000000001',
        outcome: 'citation.incorrect',
        reason: 'wrong source',
        run_id: '73000000-0000-4000-8000-000000000001',
        target_id: '76000000-0000-4000-8000-000000000001',
        target_kind: 'citation',
      },
    });

    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe('/api/v1/qa/feedback');
    expect(request.headers.get('Idempotency-Key')).toBe('feedback-stable-key');
    await expect(request.json()).resolves.toMatchObject({
      outcome: 'citation.incorrect',
      target_kind: 'citation',
      target_id: '76000000-0000-4000-8000-000000000001',
    });
  });

  it('test_qa_response_with_unsupported_schema_version_fails_safely', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json({
        ...commandEnvelope({ answer: 'protected incompatible answer' }),
        schema_version: 2,
      }),
    );
    const client = createQaClient(fetch);

    const request = client.POST('/api/v1/ask', {
      params: { header: { 'Idempotency-Key': 'ask-version-key' } },
      body: {
        question: 'Каков срок?',
        schema_version: 1,
        session_id: '74000000-0000-4000-8000-000000000001',
      },
    });

    await expect(request).rejects.toMatchObject({
      code: 'spine.client.incompatible_response',
      kind: 'unexpected',
    });
    await expect(request).rejects.not.toThrow('protected incompatible answer');
  });

  it('test_qa_response_with_nested_cross_workspace_record_fails_closed', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json({
        environment_id: environmentId,
        items: [
          {
            environment_id: environmentId,
            schema_version: 1,
            workspace_id: '20000000-0000-4000-8000-000000000002',
            question: 'protected question from another workspace',
          },
        ],
        limit: 20,
        schema_version: 1,
        trace_id: traceId,
        workspace_id: workspaceId,
      }),
    );

    const request = createQaClient(fetch).GET(
      '/api/v1/qa/sessions/{session_id}/messages',
      { params: { path: { session_id: '74000000-0000-4000-8000-000000000001' } } },
    );

    await expect(request).rejects.toMatchObject({
      code: 'spine.client.scope_mismatch',
      kind: 'authorization',
    });
    await expect(request).rejects.not.toThrow('protected question');
    await expect(request).rejects.not.toThrow('another workspace');
  });
});

function createQaClient(fetch: typeof globalThis.fetch) {
  return createQaApiClient({
    baseUrl: 'https://spine.test',
    fetch,
    getBearerCredential: () => 'test-token',
    scope: { environmentId, workspaceId },
  });
}

function commandEnvelope(result: object) {
  return {
    command_id: '70000000-0000-4000-8000-000000000001',
    environment_id: environmentId,
    replay: false,
    result,
    schema_version: 1,
    status: 'completed',
    trace_id: traceId,
    workspace_id: workspaceId,
  };
}

interface ProtectedPathsFixture {
  '/api/v1/protected': {
    get: {
      parameters: {
        cookie?: never;
        header: {
          'X-Environment-ID': string;
          'X-Workspace-ID': string;
        };
        path?: never;
        query?: never;
      };
      responses: {
        200: {
          content: { 'application/json': { status: string } };
        };
      };
    };
  };
}
