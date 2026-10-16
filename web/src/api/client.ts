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

type ScopeHeaderName = 'X-Workspace-ID' | 'X-Environment-ID';

type RemoveScopeHeader<Parameters> = Parameters extends {
  header: infer Header;
}
  ? Header extends object
    ? Omit<Header, ScopeHeaderName> extends infer Remaining extends object
      ? Omit<Parameters, 'header'> &
          (keyof Remaining extends never
            ? { header?: never }
            : { header: Remaining })
      : Parameters
    : Parameters
  : Parameters;

type RemoveScopeParameters<Operation> = Operation extends {
  parameters: infer Parameters;
}
  ? Omit<Operation, 'parameters'> & {
      parameters: RemoveScopeHeader<Parameters>;
    }
  : Operation;

export type ScopedPaths<Paths> = {
  [Path in keyof Paths]: {
    [Method in keyof Paths[Path]]: RemoveScopeParameters<Paths[Path][Method]>;
  };
};

export type ScopedApiClient = Pick<
  Client<ScopedPaths<paths>>,
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

type PublishedQaPath =
  | '/api/v1/ask'
  | '/api/v1/qa/feedback'
  | '/api/v1/qa/runs'
  | '/api/v1/qa/runs/{run_id}'
  | '/api/v1/qa/sessions'
  | '/api/v1/qa/sessions/{session_id}'
  | '/api/v1/qa/sessions/{session_id}/messages'
  | '/api/v1/qa/sessions/{session_id}/messages/{message_id}'
  | '/api/v1/qa/sessions/{session_id}/messages/{message_id}/events'
  | '/api/v1/qa/sessions/{session_id}/restore';

type PublishedQaPaths = Pick<ScopedPaths<paths>, PublishedQaPath>;

export type QaApiClient = Pick<Client<PublishedQaPaths>, 'GET' | 'POST'>;

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

export function createQaApiClient(options: ScopedApiClientOptions): QaApiClient {
  const client = createScopedApiClient(options);
  return Object.freeze({
    GET: qaGet(client.GET, options.scope),
    POST: qaJsonMethod(client.POST, options.scope) as QaApiClient['POST'],
  }) as unknown as QaApiClient;
}

function qaGet(method: ScopedApiClient['GET'], scope: ApiScope): QaApiClient['GET'] {
  const call = method as (...args: unknown[]) => unknown;
  return (async (path: unknown, options?: unknown) => {
    if (path !== '/api/v1/qa/sessions/{session_id}/messages/{message_id}/events') {
      const result = await call(path, options);
      assertQaJsonResult(result, scope);
      return result;
    }
    const requestOptions = isRecord(options) ? options : {};
    return call(path, { ...requestOptions, parseAs: 'text' });
  }) as QaApiClient['GET'];
}

function qaJsonMethod<T>(method: T, scope: ApiScope): T {
  const call = method as (...args: unknown[]) => unknown;
  return (async (...args: unknown[]) => {
    const result = await call(...args);
    assertQaJsonResult(result, scope);
    return result;
  }) as T;
}

function assertQaJsonResult(result: unknown, scope: ApiScope): void {
  if (
    !isRecord(result) ||
    !isRecord(result.data) ||
    result.data.schema_version !== 1
  ) {
    throw clientError(
      'spine.client.incompatible_response',
      'unexpected',
      'The API response version is not supported.',
    );
  }
  if (
    result.data.workspace_id !== scope.workspaceId ||
    result.data.environment_id !== scope.environmentId
  ) {
    throw scopeMismatchError();
  }
  assertNestedQaScope(result.data, scope);
}

function assertNestedQaScope(value: unknown, scope: ApiScope): void {
  if (Array.isArray(value)) {
    value.forEach((item) => assertNestedQaScope(item, scope));
    return;
  }
  if (!isRecord(value)) return;
  if (
    (Object.hasOwn(value, 'workspace_id') && value.workspace_id !== scope.workspaceId) ||
    (Object.hasOwn(value, 'environment_id') && value.environment_id !== scope.environmentId)
  ) {
    throw scopeMismatchError();
  }
  Object.values(value).forEach((item) => assertNestedQaScope(item, scope));
}

function scopeMismatchError(): SpineApiError {
  return clientError(
    'spine.client.scope_mismatch',
    'authorization',
    'The API response scope is invalid.',
  );
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
  }) as ScopedApiClient;
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
      return new Request(request, {
        headers,
        redirect: 'error',
        signal: request.signal,
      });
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
  if (code?.startsWith('spine.authentication.')) {
    return 'authentication';
  }
  if (code?.startsWith('spine.authorization.')) {
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
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
