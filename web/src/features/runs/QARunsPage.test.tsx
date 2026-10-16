import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createMemoryRouter, RouterProvider } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { AuthProvider } from '../../app/auth';
import { ScopeContext, type Scope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import { QARunsPage } from './QARunsPage';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const runId = 'a0000000-0000-4000-8000-000000000001';
const subjectId = '30000000-0000-4000-8000-000000000001';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Q&A runs list', () => {
  it('test_qa_run_list_renders_safe_status_initiator_and_timestamps', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockResolvedValue(
        Response.json(collection([makeRun()])),
      ),
    );

    renderQARuns('/qa/runs');

    expect(await screen.findByRole('table', { name: ru.qaRuns.tableCaption })).toBeVisible();
    expect(screen.getByText(runId)).toBeVisible();
    expect(screen.getByText(subjectId)).toBeVisible();
    expect(screen.getByText(ru.qaRuns.outcomes.answered)).toBeVisible();
    expect(screen.getAllByRole('time')[0]).toHaveAttribute('datetime', '2026-10-16T10:00:00Z');
    expect(document.body).not.toHaveTextContent('secret answer or provider detail');
  });

  it('test_qa_run_filters_update_url_and_request_without_losing_scope', async () => {
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(collection([makeRun()], 'cursor-next')))
      .mockResolvedValue(Response.json(collection([makeRun({ outcome: 'failed' })])));
    vi.stubGlobal('fetch', fetch);
    const router = renderQARuns(
      `/qa/runs?workspace=${scope.workspaceId}&environment=${scope.environmentId}&cursor=old`,
    );
    const user = userEvent.setup();

    await screen.findByRole('table', { name: ru.qaRuns.tableCaption });
    await user.click(screen.getByRole('button', { name: ru.qaRuns.nextPage }));
    await waitFor(() => expect(router.state.location.search).toContain('cursor=cursor-next'));

    await user.click(screen.getByRole('combobox', { name: ru.qaRuns.outcome }));
    await user.click(await screen.findByRole('option', { name: ru.qaRuns.outcomes.failed }));
    await user.click(screen.getByRole('button', { name: ru.qaRuns.applyFilters }));

    await waitFor(() => expect(router.state.location.search).toContain('outcome=failed'));
    expect(router.state.location.search).toContain(`workspace=${scope.workspaceId}`);
    expect(router.state.location.search).toContain(`environment=${scope.environmentId}`);
    expect(router.state.location.search).not.toContain('cursor=');

    const request = fetch.mock.calls.at(-1)?.[0];
    const requestUrl = new URL((request as Request).url);
    expect(requestUrl.searchParams.get('outcome')).toBe('failed');
    expect(requestUrl.searchParams.get('cursor')).toBeNull();
  });

  it('test_qa_run_list_exposes_safe_denied_state_without_provider_details', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockResolvedValue(
        Response.json(
          {
            code: 'spine.authorization.denied',
            retryability: 'never',
            safe_message: 'Access denied.',
            schema_version: 1,
            trace_id: '50000000-0000-4000-8000-000000000001',
            provider_detail: 'secret answer or provider detail',
          },
          { status: 403 },
        ),
      ),
    );

    renderQARuns('/qa/runs');

    expect(await screen.findByRole('alert')).toHaveTextContent('Доступ запрещён');
    expect(document.body).not.toHaveTextContent('secret answer or provider detail');
  });
});

function renderQARuns(initialEntry: string) {
  const router = createMemoryRouter([{ path: '*', element: <QARunsPage /> }], {
    initialEntries: [initialEntry],
  });
  render(
    <AuthProvider
      value={{
        status: 'authenticated',
        role: 'administrator',
        getBearerCredential: () => 'test-token',
      }}
    >
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ScopeContext.Provider
          value={{ scope, queryKey: (resource) => ['spine', resource, scope.workspaceId, scope.environmentId] }}
        >
          <RouterProvider router={router} />
        </ScopeContext.Provider>
      </QueryClientProvider>
    </AuthProvider>,
  );
  return router;
}

function collection(
  items: components['schemas']['QARunSummaryResponse'][],
  nextCursor?: string,
) {
  return {
    environment_id: scope.environmentId,
    items,
    limit: 25,
    next_cursor: nextCursor ?? null,
    schema_version: 1 as const,
    trace_id: '50000000-0000-4000-8000-000000000001',
    workspace_id: scope.workspaceId,
  } satisfies components['schemas']['ApiCollectionEnvelope_QARunSummaryResponse_'];
}

function makeRun(
  overrides: Partial<components['schemas']['QARunSummaryResponse']> = {},
) {
  return {
    attempt_count: 1,
    completed_at: '2026-10-16T10:00:02Z',
    created_at: '2026-10-16T10:00:00Z',
    environment_id: scope.environmentId,
    final_attempt_id: null,
    message_id: null,
    outcome: 'answered' as const,
    run_id: runId,
    schema_version: 1 as const,
    session_id: null,
    state: 'completed',
    subject_id: subjectId,
    subject_kind: 'human',
    trace_id: '50000000-0000-4000-8000-000000000002',
    workspace_id: scope.workspaceId,
    ...overrides,
  } satisfies components['schemas']['QARunSummaryResponse'];
}
