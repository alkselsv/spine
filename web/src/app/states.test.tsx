import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { StatusPanel, type InterfaceState } from './routes';

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
});
