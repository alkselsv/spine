import { describe, expect, it } from 'vitest';

import {
  DEFAULT_SCOPE,
  parseScopeSearch,
  scopeQueryKey,
  updateScopeSearch,
} from './scope';

describe('administrator scope context', () => {
  it('test_direct_link_restores_workspace_and_environment_from_url', () => {
    expect(
      parseScopeSearch(
        '?workspace=10000000-0000-4000-8000-000000000002&environment=20000000-0000-4000-8000-000000000002',
      ),
    ).toEqual({
      workspaceId: '10000000-0000-4000-8000-000000000002',
      environmentId: '20000000-0000-4000-8000-000000000002',
    });
  });

  it('test_invalid_scope_values_fall_back_to_safe_default', () => {
    expect(parseScopeSearch('?workspace=protected&environment=unknown')).toEqual(
      DEFAULT_SCOPE,
    );
  });

  it('test_scope_change_preserves_unrelated_url_state', () => {
    expect(
      updateScopeSearch(
        '?tab=activity&workspace=10000000-0000-4000-8000-000000000001',
        {
          workspaceId: '10000000-0000-4000-8000-000000000002',
          environmentId: '20000000-0000-4000-8000-000000000002',
        },
      ),
    ).toBe(
      'tab=activity&workspace=10000000-0000-4000-8000-000000000002&environment=20000000-0000-4000-8000-000000000002',
    );
  });

  it('test_scope_query_key_isolated_for_workspace_and_environment', () => {
    expect(
      scopeQueryKey('documents', {
        workspaceId: '10000000-0000-4000-8000-000000000001',
        environmentId: '20000000-0000-4000-8000-000000000001',
      }),
    ).not.toEqual(
      scopeQueryKey('documents', {
        workspaceId: '10000000-0000-4000-8000-000000000002',
        environmentId: '20000000-0000-4000-8000-000000000001',
      }),
    );
    expect(
      scopeQueryKey('documents', {
        workspaceId: '10000000-0000-4000-8000-000000000001',
        environmentId: '20000000-0000-4000-8000-000000000001',
      }),
    ).not.toEqual(
      scopeQueryKey('documents', {
        workspaceId: '10000000-0000-4000-8000-000000000001',
        environmentId: '20000000-0000-4000-8000-000000000002',
      }),
    );
  });
});
