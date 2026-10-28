import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));

vi.mock('../../api/client', () => ({
  createScopedApiClient: () => ({ GET: get, POST: post }),
}));
vi.mock('../../app/auth', () => ({
  useAuth: () => ({ getBearerCredential: () => 'synthetic-token' }),
}));
vi.mock('../../app/scope', () => ({
  useScope: () => ({
    queryKey: (name: string) => ['scope', name],
    scope: {
      workspaceId: '10000000-0000-4000-8000-000000000001',
      environmentId: '20000000-0000-4000-8000-000000000001',
    },
  }),
}));
vi.mock('react-router', () => ({
  useNavigate: () => vi.fn(),
  useParams: () => ({ runId: '30000000-0000-4000-8000-000000000001' }),
}));

import { WorkflowRunPage } from './WorkflowRunPage';

afterEach(() => {
  cleanup();
  get.mockReset();
  post.mockReset();
});

describe('WorkflowRunPage', () => {
  it('loads_every_cursor_page_of_the_run_timeline', async () => {
    get.mockImplementation((path: string, options?: { params?: { query?: { cursor?: string } } }) => {
      if (path.endsWith('/events')) {
        return Promise.resolve({
          data: options?.params?.query?.cursor === 'next-page'
            ? {
                items: [
                  { sequence: 101, event_type: 'workflow.context-added', summary: 'Later timeline event.' },
                ],
                next_cursor: null,
              }
            : {
                items: [
                  { sequence: 1, event_type: 'workflow.run-started', summary: 'First timeline event.' },
                ],
                next_cursor: 'next-page',
              },
        });
      }
      return Promise.resolve({
        data: {
          run_id: '30000000-0000-4000-8000-000000000001',
          work_item_id: '40000000-0000-4000-8000-000000000001',
          workspace_id: '10000000-0000-4000-8000-000000000001',
          environment_id: '20000000-0000-4000-8000-000000000001',
          workflow_key: 'commercial.requirements',
          workflow_version_id: '50000000-0000-4000-8000-000000000001',
          workflow_version: '1.0.0',
          run_version: 2,
          state: 'running',
          allowed_commands: [],
          created_at: '2026-10-28T00:00:00Z',
        },
      });
    });
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <WorkflowRunPage />
      </QueryClientProvider>,
    );

    expect(await screen.findByText('First timeline event.')).toBeVisible();
    expect(screen.getByText('Later timeline event.')).toBeVisible();
    expect(get).toHaveBeenCalledWith(
      '/api/v1/runs/{run_id}/events',
      expect.objectContaining({ params: expect.objectContaining({ query: { limit: 100, cursor: 'next-page' } }) }),
    );
  });

  it('restores_safe_timeline_and_continues_the_same_run', async () => {
    get.mockImplementation((path: string) => {
      if (path.endsWith('/events')) {
        return Promise.resolve({
          data: {
            items: [
              { sequence: 1, event_type: 'workflow.run-started', summary: 'Workflow run started.' },
              { sequence: 2, event_type: 'workflow.context-added', summary: 'Additional context received.' },
            ],
            next_cursor: null,
          },
        });
      }
      return Promise.resolve({
        data: {
          run_id: '30000000-0000-4000-8000-000000000001',
          work_item_id: '40000000-0000-4000-8000-000000000001',
          workspace_id: '10000000-0000-4000-8000-000000000001',
          environment_id: '20000000-0000-4000-8000-000000000001',
          workflow_key: 'commercial.requirements',
          workflow_version_id: '50000000-0000-4000-8000-000000000001',
          workflow_version: '1.0.0',
          run_version: 2,
          state: 'running',
          allowed_commands: ['add_context'],
          created_at: '2026-10-28T00:00:00Z',
        },
      });
    });
    post.mockResolvedValue({ data: { run_version: 3 } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <WorkflowRunPage />
      </QueryClientProvider>,
    );

    expect(await screen.findByText('Workflow run started.')).toBeVisible();
    expect(screen.getByText('Additional context received.')).toBeVisible();
    await user.type(screen.getByLabelText('Дополнительный контекст'), 'Срок запуска — 20 декабря');
    await user.click(screen.getByRole('button', { name: 'Добавить контекст' }));

    expect(post).toHaveBeenCalledWith(
      '/api/v1/runs/{run_id}/commands',
      expect.objectContaining({
        body: expect.objectContaining({
          command_type: 'add_context',
          expected_run_version: 2,
          payload: expect.objectContaining({ message: 'Срок запуска — 20 декабря' }),
        }),
      }),
    );
  });
});
