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

    expect(screen.getByRole('heading', { name: 'Control Plane Spine' })).toBeVisible();
    expect(screen.getByRole('status')).toHaveTextContent('Подключено');
  });

  it('test_connection_check_updates_workspace_status', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    render(<RouterProvider router={router} />);
    await user.click(screen.getByRole('button', { name: 'Проверить соединение' }));

    expect(screen.getByRole('status')).toHaveTextContent('Соединение проверено');
  });

  it('test_direct_link_restores_scope_and_scope_change_updates_url', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(appRoutes, {
      initialEntries: [
        '/knowledge/documents?workspace=10000000-0000-4000-8000-000000000002&environment=20000000-0000-4000-8000-000000000002',
      ],
    });

    render(<RouterProvider router={router} />);

    expect(screen.getByRole('combobox', { name: 'Рабочая область' })).toHaveTextContent(
      'Продажи',
    );
    expect(screen.getByRole('combobox', { name: 'Среда' })).toHaveTextContent('Staging');

    await user.click(screen.getByRole('combobox', { name: 'Рабочая область' }));
    await user.click(screen.getByRole('option', { name: 'Основная' }));

    expect(router.state.location.search).toBe(
      '?workspace=10000000-0000-4000-8000-000000000001&environment=20000000-0000-4000-8000-000000000002',
    );
  });

  it('test_navigation_exposes_required_route_boundaries', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    render(<RouterProvider router={router} />);
    await user.click(screen.getByRole('link', { name: 'Знания' }));
    expect(router.state.location.pathname).toBe('/knowledge/documents');
    await user.click(screen.getByRole('link', { name: 'Чат' }));
    expect(router.state.location.pathname).toBe('/chat/demo-session');
    await user.click(screen.getByRole('link', { name: 'Запуски' }));
    expect(router.state.location.pathname).toBe('/runs/demo-run');
    await user.click(screen.getByRole('link', { name: 'Настройки' }));
    expect(router.state.location.pathname).toBe('/settings');
  });
});
