import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';

import { appRoutes } from './app/routes';

afterEach(cleanup);

function renderApp(router: ReturnType<typeof createMemoryRouter>) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe('application bootstrap', () => {
  it('test_root_route_renders_control_plane_boundary', () => {
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    renderApp(router);

    expect(screen.getByRole('heading', { name: 'Control Plane Spine' })).toBeVisible();
    expect(screen.getByRole('status')).toHaveTextContent('Подключено');
  });

  it('test_connection_check_updates_workspace_status', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    renderApp(router);
    await user.click(screen.getByRole('button', { name: 'Проверить соединение' }));

    expect(screen.getByRole('status')).toHaveTextContent('Соединение проверено');
  });

  it('test_direct_link_restores_scope_and_scope_change_updates_url', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(appRoutes, {
      initialEntries: [
        '/knowledge/documents?workspace=10000000-0000-4000-8000-000000000002&environment=20000000-0000-4000-8000-000000000002&cursor=stale-cursor',
      ],
    });

    renderApp(router);

    expect(await screen.findByRole('combobox', { name: 'Рабочая область' })).toHaveTextContent(
      'Продажи',
    );
    expect(await screen.findByRole('combobox', { name: 'Среда' })).toHaveTextContent('Staging');

    await user.click(screen.getByRole('combobox', { name: 'Рабочая область' }));
    await user.click(await screen.findByRole('option', { name: 'Основная' }));

    expect(router.state.location.search).toBe(
      '?workspace=10000000-0000-4000-8000-000000000001&environment=20000000-0000-4000-8000-000000000002',
    );
  });

  it('test_navigation_exposes_required_route_boundaries', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    renderApp(router);
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
