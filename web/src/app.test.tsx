import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
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
  it('test_primary_product_routes_render_the_original_spine_ui_shell', async () => {
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/knowledge/documents'] });

    renderApp(router);

    expect(await screen.findByRole('navigation', { name: 'Разделы workspace' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Знания' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByText('S', { selector: '[aria-hidden="true"]' })).toBeVisible();
  });

  it('test_root_route_renders_control_plane_boundary', () => {
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    renderApp(router);

    expect(screen.getByRole('heading', { name: 'Знания' })).toBeVisible();
    expect(screen.getByText('Control Plane Spine')).toBeVisible();
    expect(screen.getByRole('status')).toHaveTextContent('Подключено');
  });

  it('test_shell_exposes_scope_in_the_original_compound_pill', () => {
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/chat'] });

    renderApp(router);

    expect(screen.getByRole('navigation', { name: 'Разделы workspace' })).toBeVisible();
    expect(screen.getByRole('main')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Рабочая область: Основная, Среда: Local' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Чат' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('status')).toHaveTextContent('Подключено');
  });

  it('test_qa_runs_route_is_available_from_the_scoped_navigation', async () => {
    const router = createMemoryRouter(appRoutes, {
      initialEntries: ['/qa/runs?workspace=10000000-0000-4000-8000-000000000002&environment=20000000-0000-4000-8000-000000000002'],
    });

    renderApp(router);

    expect(await screen.findByRole('heading', { level: 1, name: 'Запуски Q&A' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Запуски Q&A' })).toHaveAttribute('aria-current', 'page');
  });

  it('test_connection_check_reports_unavailable_without_runtime_credentials', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    renderApp(router);
    await user.click(screen.getByRole('button', { name: 'Проверить соединение' }));

    expect(screen.getByRole('status')).toHaveTextContent('Соединение недоступно');
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

    await user.click(screen.getByRole('button', { name: /Рабочая область:/ }));
    await user.click(await screen.findByRole('menuitemradio', { name: 'Staging' }));

    expect(screen.getByRole('status')).toHaveTextContent('Подключено');
  });

  it('test_direct_link_restores_scope_and_scope_change_updates_url', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(appRoutes, {
      initialEntries: [
        '/knowledge/documents?workspace=10000000-0000-4000-8000-000000000002&environment=20000000-0000-4000-8000-000000000002&cursor=stale-cursor&processing_cursor=stale-processing-cursor',
      ],
    });

    renderApp(router);

    expect(await screen.findByRole('button', { name: 'Рабочая область: Продажи, Среда: Staging' })).toBeVisible();

    await user.click(screen.getByRole('button', { name: /Рабочая область:/ }));
    await user.click(await screen.findByRole('menuitemradio', { name: 'Основная' }));

    expect(router.state.location.search).toBe(
      '?workspace=10000000-0000-4000-8000-000000000001&environment=20000000-0000-4000-8000-000000000002',
    );
  });

  it('test_navigation_exposes_original_product_route_boundaries', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    renderApp(router);
    await user.click(screen.getByRole('button', { name: 'Знания' }));
    expect(router.state.location.pathname).toBe('/knowledge/documents');
    await user.click(screen.getByRole('button', { name: 'Чат' }));
    expect(router.state.location.pathname).toBe('/chat');
    await user.click(screen.getByRole('button', { name: 'Запуски Q&A' }));
    await waitFor(() => expect(router.state.location.pathname).toBe('/qa/runs'));
  });

  it('test_root_route_defaults_to_the_original_knowledge_screen', () => {
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    renderApp(router);
    expect(screen.getByRole('button', { name: 'Знания' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('button', { name: 'Обзор' })).not.toHaveAttribute('aria-current');
  });

  it('test_theme_toggle_switches_document_theme_and_persists_choice', async () => {
    const user = userEvent.setup();
    document.documentElement.dataset.theme = 'light';
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] });

    renderApp(router);
    await user.click(screen.getByRole('button', { name: 'Включить тёмную тему' }));

    expect(window.localStorage.getItem('spine.theme')).toBe('dark');
    await user.click(screen.getByRole('button', { name: 'Включить светлую тему' }));
    expect(window.localStorage.getItem('spine.theme')).toBe('light');
  });
});
