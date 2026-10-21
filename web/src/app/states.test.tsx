import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { SpineApiError } from '../api/client';
import { ApiErrorPanel, StatusPanel, type InterfaceState } from './routes';

afterEach(cleanup);

describe('shared interface state presentation', () => {
  it.each([
    'loading',
    'empty',
    'partial',
    'stale',
    'degraded',
    'permissionDenied',
    'validationFailed',
    'failed',
    'ready',
  ] as InterfaceState[])('test_%s_state_has_a_named_safe_panel', (state) => {
    render(<StatusPanel state={state} />);

    const panel = screen.getByRole('region');
    expect(panel).toHaveAccessibleName();
    expect(panel).toHaveTextContent(/\S/);
  });

  it('test_authorization_error_has_a_named_permission_denied_alert', () => {
    render(<ApiErrorPanel error={new SpineApiError({
      code: 'spine.authorization.denied',
      kind: 'authorization',
      retryability: 'never',
      safeMessage: 'safe denial',
      status: 403,
      traceId: '50000000-0000-4000-8000-000000000001',
    })} />);

    expect(screen.getByRole('alert', { name: 'Доступ запрещён' })).toBeVisible();
  });
});
