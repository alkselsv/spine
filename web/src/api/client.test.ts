import { describe, expect, it, vi } from 'vitest';
import type { Client } from 'openapi-fetch';

import {
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
  });
});

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
