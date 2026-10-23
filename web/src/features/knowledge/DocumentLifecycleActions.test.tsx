import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { createScopedApiClient } from '../../api/client';
import { DocumentLifecycleActions } from './DocumentLifecycleActions';

const scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const sourceObjectId = '30000000-0000-4000-8000-000000000001';
const revisionId = '40000000-0000-4000-8000-000000000001';
const operationId = '50000000-0000-4000-8000-000000000001';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('document lifecycle actions', () => {
  it('test_tombstone_conflict_preserves_history_and_exposes_safe_retry_state', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json(
        {
          code: 'spine.persistence.conflict',
          retryability: 'never',
          safe_message: 'The document changed before this command was accepted.',
          schema_version: 1,
          trace_id: operationId,
        },
        { status: 409 },
      ),
    );
    const refresh = vi.fn();
    renderActions(fetch, refresh);
    const user = userEvent.setup();

    await user.selectOptions(screen.getByLabelText('Причина логического удаления'), 'source_deleted');
    await user.click(screen.getByRole('button', { name: 'Создать tombstone' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Команда конфликтует с текущим состоянием');
    expect(screen.queryByRole('button', { name: 'Повторить команду' })).not.toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe(`/api/v1/documents/${sourceObjectId}/tombstone`);
    await expect(request.json()).resolves.toEqual({
      deletion_reason: 'source_deleted',
      expected_current_revision_id: revisionId,
    });
  });

  it('test_denied_command_shows_safe_permission_state_without_retry_or_protected_text', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json(
        {
          code: 'spine.authorization.denied',
          retryability: 'never',
          safe_message: 'Not authorized.',
          schema_version: 1,
          trace_id: operationId,
          protected_text: 'must never be displayed',
        },
        { status: 403 },
      ),
    );
    renderActions(fetch, vi.fn());
    const user = userEvent.setup();

    await user.selectOptions(screen.getByLabelText('Причина логического удаления'), 'source_deleted');
    await user.click(screen.getByRole('button', { name: 'Создать tombstone' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Доступ запрещён');
    expect(screen.queryByRole('button', { name: 'Повторить команду' })).not.toBeInTheDocument();
    expect(globalThis.document.body).not.toHaveTextContent('must never be displayed');
  });

  it('test_action_forms_expose_accessible_labels_without_physical_delete_control', () => {
    renderActions(vi.fn<typeof globalThis.fetch>(), vi.fn());

    expect(screen.getByLabelText('Файл новой ревизии')).toBeVisible();
    expect(screen.queryByLabelText('Ссылка на подготовленную загрузку')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Причина логического удаления')).toBeVisible();
    expect(screen.queryByRole('button', { name: /физически удалить/i })).not.toBeInTheDocument();
  });
});

function renderActions(fetch: typeof globalThis.fetch, onRefresh: () => void) {
  const client = createScopedApiClient({
    baseUrl: 'https://spine.test',
    fetch,
    getBearerCredential: () => 'test-token',
    scope,
  });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <DocumentLifecycleActions
        client={client}
        currentRevisionId={revisionId}
        onRefresh={onRefresh}
        queryKey={(resource) => ['spine', resource, scope.workspaceId, scope.environmentId]}
        scope={scope}
        sourceObjectId={sourceObjectId}
      />
    </QueryClientProvider>,
  );
}
