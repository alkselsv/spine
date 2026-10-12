import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';

import { appRoutes } from './app/routes';

afterEach(cleanup);

describe('application bootstrap', () => {
  it('test_root_route_renders_control_plane_boundary', () => {
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    render(<RouterProvider router={router} />);

    expect(
      screen.getByRole('heading', { name: 'Spine Control Plane' }),
    ).toBeVisible();
    expect(screen.getByRole('status')).toHaveTextContent('Workspace connected');
  });

  it('test_connection_check_updates_workspace_status', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    render(<RouterProvider router={router} />);
    await user.click(screen.getByRole('button', { name: 'Check connection' }));

    expect(screen.getByRole('status')).toHaveTextContent('Connection checked');
  });
});
