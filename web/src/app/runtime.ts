import type { AuthState } from './auth';
import {
  DEFAULT_SCOPE,
  environmentOptions,
  workspaceOptions,
  type EnvironmentId,
  type Scope,
  type ScopeOption,
  type WorkspaceId,
} from './scope';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const IDENTITY_VERSION_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/;

export interface BrowserRuntimeScope {
  defaultScope: Scope;
  environmentOptions: readonly ScopeOption<EnvironmentId>[];
  workspaceOptions: readonly ScopeOption<WorkspaceId>[];
}

export interface BrowserRuntime {
  auth: AuthState;
  scope: BrowserRuntimeScope;
}

interface InjectedAuthentication {
  getBearerCredential: () => Promise<string> | string;
  identityVersion: string;
  role: 'administrator';
  status: 'authenticated';
}

interface InjectedScope {
  defaultEnvironmentId: string;
  defaultWorkspaceId: string;
  environments: readonly ScopeOption<string>[];
  workspaces: readonly ScopeOption<string>[];
}

export interface InjectedBrowserRuntime {
  authentication: InjectedAuthentication;
  scope: InjectedScope;
}

export interface BrowserRuntimeSource {
  __SPINE_RUNTIME__?: InjectedBrowserRuntime;
}

const fallbackRuntime: BrowserRuntime = Object.freeze({
  auth: Object.freeze({ status: 'unavailable' }),
  scope: Object.freeze({
    defaultScope: DEFAULT_SCOPE,
    environmentOptions,
    workspaceOptions,
  }),
});

/**
 * Resolve the identity adapter installed by the deployment before the SPA loads.
 * The credential remains behind a callback and is never copied to URL or storage.
 * Scope values are selectors only; the server remains the authorization authority.
 */
export function readBrowserRuntime(
  source: BrowserRuntimeSource = window as Window & BrowserRuntimeSource,
): BrowserRuntime {
  const injected = source.__SPINE_RUNTIME__;
  if (!isInjectedAuthentication(injected?.authentication) || !injected?.scope) {
    return fallbackRuntime;
  }

  const workspaces = validateOptions(injected.scope.workspaces);
  const environments = validateOptions(injected.scope.environments);
  if (
    !workspaces ||
    !environments ||
    !workspaces.some(({ id }) => id === injected.scope.defaultWorkspaceId) ||
    !environments.some(({ id }) => id === injected.scope.defaultEnvironmentId)
  ) {
    return fallbackRuntime;
  }

  return Object.freeze({
    auth: Object.freeze({ ...injected.authentication }),
    scope: Object.freeze({
      defaultScope: Object.freeze({
        environmentId: injected.scope.defaultEnvironmentId,
        workspaceId: injected.scope.defaultWorkspaceId,
      }),
      environmentOptions: environments,
      workspaceOptions: workspaces,
    }),
  });
}

function isInjectedAuthentication(value: InjectedAuthentication | undefined): value is InjectedAuthentication {
  return value?.status === 'authenticated' &&
    value.role === 'administrator' &&
    typeof value.getBearerCredential === 'function' &&
    typeof value.identityVersion === 'string' &&
    IDENTITY_VERSION_PATTERN.test(value.identityVersion);
}

function validateOptions<Value extends string>(
  values: readonly ScopeOption<Value>[],
): readonly ScopeOption<Value>[] | null {
  if (!Array.isArray(values) || values.length === 0 || values.length > 32) return null;
  const ids = new Set<string>();
  const result: ScopeOption<Value>[] = [];
  for (const value of values) {
    if (
      !value ||
      typeof value.id !== 'string' ||
      !UUID_PATTERN.test(value.id) ||
      typeof value.label !== 'string' ||
      !value.label.trim() ||
      value.label.length > 80 ||
      ids.has(value.id)
    ) {
      return null;
    }
    ids.add(value.id);
    result.push(Object.freeze({ id: value.id, label: value.label }));
  }
  return Object.freeze(result);
}

declare global {
  interface Window {
    __SPINE_RUNTIME__?: InjectedBrowserRuntime;
  }
}
