import type { QueryKey } from '@tanstack/react-query';
import { createContext, useContext } from 'react';

import { ru } from '../i18n/resources';

export interface Scope {
  environmentId: EnvironmentId;
  workspaceId: WorkspaceId;
}

export const workspaceOptions = [
  {
    id: '10000000-0000-4000-8000-000000000001',
    label: ru.scope.workspacePrimary,
  },
  {
    id: '10000000-0000-4000-8000-000000000002',
    label: ru.scope.workspaceSales,
  },
] as const;

export const environmentOptions = [
  { id: '20000000-0000-4000-8000-000000000001', label: ru.scope.environmentLocal },
  { id: '20000000-0000-4000-8000-000000000002', label: ru.scope.environmentStaging },
] as const;

export type WorkspaceId = (typeof workspaceOptions)[number]['id'];
export type EnvironmentId = (typeof environmentOptions)[number]['id'];

export const DEFAULT_SCOPE: Scope = {
  workspaceId: workspaceOptions[0].id,
  environmentId: environmentOptions[0].id,
};

export function parseScopeSearch(search: string): Scope {
  const parameters = new URLSearchParams(search);
  const workspaceId = parameters.get('workspace');
  const environmentId = parameters.get('environment');
  if (isWorkspaceId(workspaceId) && isEnvironmentId(environmentId)) {
    return { workspaceId, environmentId };
  }
  return DEFAULT_SCOPE;
}

export function updateScopeSearch(search: string, scope: Scope): string {
  const parameters = new URLSearchParams(search);
  parameters.set('workspace', scope.workspaceId);
  parameters.set('environment', scope.environmentId);
  return parameters.toString();
}

export function scopeQueryKey(resource: string, scope: Scope): QueryKey {
  return ['spine', resource, scope.workspaceId, scope.environmentId];
}

export interface ScopeContextValue {
  scope: Scope;
  queryKey: (resource: string) => QueryKey;
}

export const ScopeContext = createContext<ScopeContextValue | null>(null);

export function useScope(): ScopeContextValue {
  const value = useContext(ScopeContext);
  if (!value) throw new Error('Scope context is required inside the Control Plane shell');
  return value;
}

function isWorkspaceId(value: string | null): value is WorkspaceId {
  return workspaceOptions.some((option) => option.id === value);
}

function isEnvironmentId(value: string | null): value is EnvironmentId {
  return environmentOptions.some((option) => option.id === value);
}
