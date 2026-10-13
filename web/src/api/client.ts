import createClient, { type Client, type Middleware } from 'openapi-fetch';

import type { paths } from '../../api/generated/schema';

const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export type ApiFailureKind =
  | 'authentication'
  | 'authorization'
  | 'validation'
  | 'unavailable'
  | 'request'
  | 'unexpected';

export type ApiRetryability = 'never' | 'same_command' | 'after_delay';

export interface ApiScope {
  environmentId: string;
  workspaceId: string;
}

export interface ScopedApiClientOptions {
  baseUrl: string;
  fetch?: typeof globalThis.fetch;
  getBearerCredential: () => Promise<string> | string;
  scope: ApiScope;
}

export class SpineApiError extends Error {
  readonly code: string;
  readonly kind: ApiFailureKind;
  readonly retryability: ApiRetryability;
  readonly safeMessage: string;
  readonly status: number;
  readonly traceId: string | null;

  constructor(options: {
    code: string;
    kind: ApiFailureKind;
    retryability: ApiRetryability;
    safeMessage: string;
    status: number;
    traceId: string | null;
  }) {
    super(options.safeMessage);
    this.name = 'SpineApiError';
    this.code = options.code;
    this.kind = options.kind;
    this.retryability = options.retryability;
    this.safeMessage = options.safeMessage;
    this.status = options.status;
    this.traceId = options.traceId;
  }

  toJSON() {
    return {
      code: this.code,
      kind: this.kind,
      retryability: this.retryability,
      safeMessage: this.safeMessage,
      status: this.status,
      traceId: this.traceId,
    };
  }
}

export type ScopedApiClient = Pick<
  Client<paths>,
  | 'request'
  | 'GET'
  | 'PUT'
  | 'POST'
  | 'DELETE'
  | 'OPTIONS'
  | 'HEAD'
  | 'PATCH'
  | 'TRACE'
>;

export function createScopedApiClient(
  options: ScopedApiClientOptions,
): ScopedApiClient {
  const baseUrl = parseBaseUrl(options.baseUrl);
  const scope = validateScope(options.scope);
  const client = createClient<paths>({
    baseUrl: baseUrl.href.replace(/\/$/, ''),
    fetch: options.fetch,
  });
  client.use(createScopeMiddleware(baseUrl, scope, options.getBearerCredential));
  return lockClient(client);
}

function lockClient(client: Client<paths>): ScopedApiClient {
  return Object.freeze({
    request: guardRequestOptions(client.request),
    GET: guardRequestOptions(client.GET),
    PUT: guardRequestOptions(client.PUT),
    POST: guardRequestOptions(client.POST),
    DELETE: guardRequestOptions(client.DELETE),
    OPTIONS: guardRequestOptions(client.OPTIONS),
    HEAD: guardRequestOptions(client.HEAD),
    PATCH: guardRequestOptions(client.PATCH),
    TRACE: guardRequestOptions(client.TRACE),
  });
}

function guardRequestOptions<T>(method: T): T {
  const callable = method as (...args: unknown[]) => unknown;
  return (async (...args: unknown[]) => {
    assertSafeRequestOptions(args.at(-1));
    return callable(...args);
  }) as T;
}

function assertSafeRequestOptions(candidate: unknown): void {
  if (!candidate || typeof candidate !== 'object') return;
  const options = candidate as Record<string, unknown>;
  if (
    ['baseUrl', 'fetch', 'Request', 'headers', 'middleware'].some((key) =>
      Object.hasOwn(options, key),
    )
  ) {
    throw clientError(
      'spine.client.invalid_request',
      'request',
      'The API request configuration is invalid.',
    );
  }
}

function createScopeMiddleware(
  baseUrl: URL,
  scope: ApiScope,
  getBearerCredential: ScopedApiClientOptions['getBearerCredential'],
): Middleware {
  return {
    async onRequest({ request }) {
      const requestUrl = new URL(request.url);
      if (requestUrl.origin !== baseUrl.origin) {
        throw clientError(
          'spine.client.invalid_request',
          'request',
          'The API request configuration is invalid.',
        );
      }
      let credential: string;
      try {
        credential = await getBearerCredential();
      } catch {
        throw clientError(
          'spine.client.invalid_credential',
          'authentication',
          'Authentication configuration is invalid.',
        );
      }
      if (!credential || /[\r\n]/.test(credential)) {
        throw clientError(
          'spine.client.invalid_credential',
          'authentication',
          'Authentication configuration is invalid.',
        );
      }
      const headers = new Headers(request.headers);
      headers.set('Authorization', `Bearer ${credential}`);
      headers.set('X-Workspace-ID', scope.workspaceId);
      headers.set('X-Environment-ID', scope.environmentId);
      return new Request(request, { headers, redirect: 'error' });
    },
    async onResponse({ response }) {
      if (!response.ok) {
        throw await mapErrorResponse(response);
      }
    },
    onError({ error, request }) {
      if (request.signal.aborted) {
        if (error instanceof Error) return error;
        const abortError = new Error('The operation was aborted.');
        abortError.name = 'AbortError';
        return abortError;
      }
      return clientError(
        'spine.client.api_unavailable',
        'unavailable',
        'The API is temporarily unavailable.',
        'after_delay',
      );
    },
  };
}

async function mapErrorResponse(response: Response): Promise<SpineApiError> {
  const traceHeader = validUuid(response.headers.get('X-Trace-ID'));
  let body: unknown;
  try {
    body = await response.clone().json();
  } catch {
    body = undefined;
  }

  if (isStructuredError(body)) {
    return new SpineApiError({
      code: body.code,
      kind: failureKind(response.status, body.code),
      retryability: body.retryability,
      safeMessage: body.safe_message,
      status: response.status,
      traceId: validUuid(body.trace_id) ?? traceHeader,
    });
  }

  const fallback = fallbackForStatus(response.status);
  return new SpineApiError({
    code: fallback.code,
    kind: fallback.kind,
    retryability: fallback.retryability,
    safeMessage: fallback.safeMessage,
    status: response.status,
    traceId: traceHeader,
  });
}

function isStructuredError(value: unknown): value is {
  code: string;
  retryability: ApiRetryability;
  safe_message: string;
  trace_id: string;
} {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.code === 'string' &&
    candidate.code.startsWith('spine.') &&
    typeof candidate.safe_message === 'string' &&
    candidate.safe_message.length > 0 &&
    candidate.safe_message.length <= 256 &&
    (candidate.retryability === 'never' ||
      candidate.retryability === 'same_command' ||
      candidate.retryability === 'after_delay') &&
    validUuid(candidate.trace_id) !== null
  );
}

function failureKind(status: number, code?: string): ApiFailureKind {
  if (status === 401 || code?.startsWith('spine.authentication.')) {
    return 'authentication';
  }
  if (status === 403 || code?.startsWith('spine.authorization.')) {
    return 'authorization';
  }
  if (code === 'spine.request.validation') {
    return 'validation';
  }
  return fallbackForStatus(status).kind;
}

function fallbackForStatus(status: number): {
  code: string;
  kind: ApiFailureKind;
  retryability: ApiRetryability;
  safeMessage: string;
} {
  if (status === 401) {
    return {
      code: 'spine.authentication.invalid',
      kind: 'authentication',
      retryability: 'never',
      safeMessage: 'Authentication is invalid.',
    };
  }
  if (status === 403) {
    return {
      code: 'spine.authorization.denied',
      kind: 'authorization',
      retryability: 'never',
      safeMessage: 'Authorization is denied.',
    };
  }
  if (status === 400 || status === 422) {
    return {
      code: 'spine.request.validation',
      kind: 'validation',
      retryability: 'never',
      safeMessage: 'Request validation failed.',
    };
  }
  if (status >= 500) {
    return {
      code: 'spine.client.api_unavailable',
      kind: 'unavailable',
      retryability: 'after_delay',
      safeMessage: 'The API is temporarily unavailable.',
    };
  }
  return {
    code: 'spine.client.request_failed',
    kind: status >= 400 ? 'request' : 'unexpected',
    retryability: 'never',
    safeMessage: 'The API request failed.',
  };
}

function parseBaseUrl(value: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw clientError(
      'spine.client.invalid_configuration',
      'request',
      'The API client configuration is invalid.',
    );
  }
  if (
    !['http:', 'https:'].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  ) {
    throw clientError(
      'spine.client.invalid_configuration',
      'request',
      'The API client configuration is invalid.',
    );
  }
  return url;
}

function validateScope(scope: ApiScope): ApiScope {
  if (!validUuid(scope.workspaceId) || !validUuid(scope.environmentId)) {
    throw clientError(
      'spine.client.invalid_scope',
      'request',
      'The selected API scope is invalid.',
    );
  }
  return Object.freeze({ ...scope });
}

function validUuid(value: unknown): string | null {
  return typeof value === 'string' && uuidPattern.test(value) ? value : null;
}

function clientError(
  code: string,
  kind: ApiFailureKind,
  safeMessage: string,
  retryability: ApiRetryability = 'never',
): SpineApiError {
  return new SpineApiError({
    code,
    kind,
    retryability,
    safeMessage,
    status: 0,
    traceId: null,
  });
}
