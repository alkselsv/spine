import { describe, expect, it } from 'vitest';

import { DEFAULT_SCOPE, environmentOptions, workspaceOptions } from './scope';
import { readBrowserRuntime } from './runtime';

describe('readBrowserRuntime', () => {
  it('returns a degraded content-free runtime when no identity adapter is installed', () => {
    const runtime = readBrowserRuntime({});

    expect(runtime.auth).toEqual({ status: 'unavailable' });
    expect(runtime.scope).toEqual({
      defaultScope: DEFAULT_SCOPE,
      environmentOptions,
      workspaceOptions,
    });
  });

  it('binds an authenticated administrator and configured scope options without reading a token', () => {
    let credentialReads = 0;
    const getBearerCredential = () => {
      credentialReads += 1;
      return 'operator-supplied-token';
    };

    const runtime = readBrowserRuntime({
      __SPINE_RUNTIME__: {
        authentication: {
          getBearerCredential,
          identityVersion: 'principal-generation-7',
          role: 'administrator',
          status: 'authenticated',
        },
        scope: {
          defaultEnvironmentId: '22000000-0000-4000-8000-000000000001',
          defaultWorkspaceId: '11000000-0000-4000-8000-000000000001',
          environments: [
            { id: '22000000-0000-4000-8000-000000000001', label: 'Qualification' },
          ],
          workspaces: [
            { id: '11000000-0000-4000-8000-000000000001', label: 'Workspace A' },
            { id: '11000000-0000-4000-8000-000000000002', label: 'Workspace B' },
          ],
        },
      },
    });

    expect(credentialReads).toBe(0);
    expect(runtime.auth).toEqual({
      getBearerCredential,
      identityVersion: 'principal-generation-7',
      role: 'administrator',
      status: 'authenticated',
    });
    expect(runtime.scope.defaultScope).toEqual({
      environmentId: '22000000-0000-4000-8000-000000000001',
      workspaceId: '11000000-0000-4000-8000-000000000001',
    });
    expect(runtime.scope.workspaceOptions).toHaveLength(2);
  });

  it.each([
    ['invalid workspace id', {
      defaultEnvironmentId: '22000000-0000-4000-8000-000000000001',
      defaultWorkspaceId: 'not-a-uuid',
      environments: [{ id: '22000000-0000-4000-8000-000000000001', label: 'Qualification' }],
      workspaces: [{ id: 'not-a-uuid', label: 'Workspace A' }],
    }],
    ['default outside options', {
      defaultEnvironmentId: '22000000-0000-4000-8000-000000000001',
      defaultWorkspaceId: '11000000-0000-4000-8000-000000000002',
      environments: [{ id: '22000000-0000-4000-8000-000000000001', label: 'Qualification' }],
      workspaces: [{ id: '11000000-0000-4000-8000-000000000001', label: 'Workspace A' }],
    }],
  ])('fails closed for %s', (_name, scope) => {
    const runtime = readBrowserRuntime({
      __SPINE_RUNTIME__: {
        authentication: {
          getBearerCredential: () => 'operator-supplied-token',
          identityVersion: 'principal-generation-7',
          role: 'administrator',
          status: 'authenticated',
        },
        scope,
      },
    });

    expect(runtime.auth).toEqual({ status: 'unavailable' });
    expect(runtime.scope.defaultScope).toEqual(DEFAULT_SCOPE);
  });
});
