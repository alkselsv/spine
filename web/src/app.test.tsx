import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { appRoutes } from './app/routes';
import { AuthProvider, type AuthState } from './app/auth';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  delete document.documentElement.dataset.theme;
});

function renderApp(router: ReturnType<typeof createMemoryRouter>, auth?: AuthState) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AuthProvider value={auth ?? { status: 'authenticated', role: 'administrator' }}>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe('application bootstrap', () => {
  it('test_primary_product_routes_render_the_halaska_control_plane_runtime', async () => {
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/knowledge/documents'] });

    renderApp(router);

    const controlPlane = await screen.findByTestId('spine-control-plane');
    expect(controlPlane).toHaveAttribute('data-presentation-runtime', 'halaska');
    expect(screen.getByRole('navigation', { name: 'Разделы workspace' })).toBeVisible();
  });

  it('test_root_route_renders_control_plane_boundary', () => {
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    renderApp(router);

    expect(screen.getByRole('heading', { name: 'Control Plane Spine' })).toBeVisible();
    expect(screen.getByRole('status')).toHaveTextContent('Подключено');
  });

  it('test_shell_exposes_scoped_context_as_a_labeled_accessible_group', () => {
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/chat'] });

    renderApp(router);

    expect(screen.getByRole('navigation', { name: 'Разделы workspace' })).toBeVisible();
    expect(screen.getByRole('main')).toBeVisible();
    expect(screen.getByRole('group', { name: 'Рабочая область и среда' })).toBeVisible();
    const connectionStatus = screen.getByRole('group', { name: 'Состояние соединения' });
    expect(connectionStatus).toBeVisible();
    expect(screen.getByRole('combobox', { name: 'Рабочая область' })).toBeVisible();
    expect(screen.getByRole('combobox', { name: 'Среда' })).toBeVisible();
    expect(screen.getByRole('link', { name: 'Чат' })).toHaveAttribute('aria-current', 'page');
    expect(within(connectionStatus).getByRole('status')).toHaveTextContent('Подключено');
  });

  it('test_qa_runs_route_is_available_from_the_scoped_navigation', async () => {
    const router = createMemoryRouter(appRoutes, {
      initialEntries: ['/qa/runs?workspace=10000000-0000-4000-8000-000000000002&environment=20000000-0000-4000-8000-000000000002'],
    });

    renderApp(router);

    expect(await screen.findByRole('heading', { level: 2, name: 'Запуски Q&A' })).toBeVisible();
    expect(screen.getByRole('link', { name: 'Запуски Q&A' })).toHaveAttribute(
      'href',
      expect.stringContaining('/qa/runs'),
    );
  });

  it('test_connection_check_updates_workspace_status', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    renderApp(router);
    await user.click(screen.getByRole('button', { name: 'Проверить соединение' }));

    expect(screen.getByRole('status')).toHaveTextContent('Соединение проверено');
  });

  it('test_scope_change_clears_failed_connection_status', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Network unavailable')));

    renderApp(router, {
      status: 'authenticated',
      role: 'administrator',
      getBearerCredential: () => 'test-token',
    });
    await user.click(screen.getByRole('button', { name: 'Проверить соединение' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Соединение недоступно');

    await user.click(screen.getByRole('combobox', { name: 'Среда' }));
    await user.click(await screen.findByRole('option', { name: 'Staging' }));

    expect(screen.getByRole('status')).toHaveTextContent('Подключено');
    expect(screen.getByRole('status')).toHaveAttribute('data-state', 'connected');
  });

  it('test_direct_link_restores_scope_and_scope_change_updates_url', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(appRoutes, {
      initialEntries: [
        '/knowledge/documents?workspace=10000000-0000-4000-8000-000000000002&environment=20000000-0000-4000-8000-000000000002&cursor=stale-cursor&processing_cursor=stale-processing-cursor',
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
    expect(router.state.location.pathname).toBe('/chat');
    await user.click(screen.getByRole('link', { name: 'Запуски' }));
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs'));
    await user.click(screen.getByRole('link', { name: 'Настройки' }));
    expect(router.state.location.pathname).toBe('/settings');
  });

  it('test_overview_link_is_current_only_on_root_route', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    renderApp(router);
    expect(screen.getByRole('link', { name: 'Обзор' })).toHaveAttribute('aria-current', 'page');

    await user.click(screen.getByRole('link', { name: 'Чат' }));
    expect(screen.getByRole('link', { name: 'Обзор' })).not.toHaveAttribute('aria-current');
  });

  it('test_theme_toggle_switches_document_theme_and_persists_choice', async () => {
    const user = userEvent.setup();
    document.documentElement.dataset.theme = 'light';
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    renderApp(router);
    await user.click(screen.getByRole('button', { name: 'Включить тёмную тему' }));

    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(window.localStorage.getItem('spine.theme')).toBe('dark');
    await user.click(screen.getByRole('button', { name: 'Включить светлую тему' }));
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(window.localStorage.getItem('spine.theme')).toBe('light');
  });
});
