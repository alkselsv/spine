import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
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
  it('test_replacement_submits_current_server_revision_and_renders_completed_receipt', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      Response.json(receipt('replacement', 'completed'), { status: 202 }),
    );
    const refresh = vi.fn();
    renderActions(fetch, refresh);
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Ссылка на подготовленную загрузку'), 'staged-upload:request-001');
    await user.type(screen.getByLabelText('Имя файла новой ревизии'), 'handbook.md');
    await user.selectOptions(screen.getByLabelText('Тип содержимого'), 'text/markdown');
    await user.click(screen.getByRole('button', { name: 'Заменить документ' }));

    expect(await screen.findByRole('status')).toHaveTextContent('Операция завершена');
    expect(refresh).toHaveBeenCalledTimes(1);
    const request = fetch.mock.calls[0]?.[0] as Request;
    expect(request.headers.get('Idempotency-Key')).toMatch(/^document-replacement-/);
    await expect(request.json()).resolves.toEqual({
      expected_current_revision_id: revisionId,
      upload_intent: {
        declared_filename: 'handbook.md',
        declared_media_type: 'text/markdown',
        storage_request_reference: 'staged-upload:request-001',
        upload_identity: 'handbook',
      },
    });
  });

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

  it('test_retryable_replacement_replays_same_command_key_only_when_server_allows_retry', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(
        Response.json(
          {
            code: 'spine.document_lifecycle.processing_unavailable',
            retryability: 'after_delay',
            safe_message: 'The operation can be retried later.',
            schema_version: 1,
            trace_id: operationId,
          },
          { status: 503 },
        ),
      )
      .mockResolvedValueOnce(Response.json(receipt('replacement', 'completed'), { status: 202 }));
    renderActions(fetch, vi.fn());
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Ссылка на подготовленную загрузку'), 'staged-upload:request-001');
    await user.type(screen.getByLabelText('Имя файла новой ревизии'), 'handbook.md');
    await user.type(screen.getByLabelText('Идентичность загрузки'), 'handbook');
    await user.click(screen.getByRole('button', { name: 'Заменить документ' }));
    await screen.findByRole('button', { name: 'Повторить команду' });
    await user.click(screen.getByRole('button', { name: 'Повторить команду' }));

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('status')).toHaveTextContent('Операция завершена');
    expect(fetch.mock.calls[1]?.[0] instanceof Request).toBe(true);
    expect(
      (fetch.mock.calls[1]?.[0] as Request).headers.get('Idempotency-Key'),
    ).toBe((fetch.mock.calls[0]?.[0] as Request).headers.get('Idempotency-Key'));
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

    expect(screen.getByLabelText('Ссылка на подготовленную загрузку')).toBeVisible();
    expect(screen.getByLabelText('Имя файла новой ревизии')).toBeVisible();
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
        uploadIdentity="handbook"
      />
    </QueryClientProvider>,
  );
}

function receipt(
  operationKind: 'replacement' | 'tombstone',
  status: 'completed' | 'processing_dispatched',
) {
  return {
    accepted_at: '2026-10-16T10:00:00Z',
    accepted_revision_id: '60000000-0000-0000-0000-000000000001',
    canonical_acceptance_id: operationId,
    completed_at: status === 'completed' ? '2026-10-16T10:01:00Z' : null,
    expected_current_revision_id: revisionId,
    idempotency_receipt_id: '70000000-0000-0000-0000-000000000001',
    operation_id: operationId,
    operation_kind: operationKind,
    processing_run_id: null,
    replay: false,
    retryable: false,
    safe_failure_code: null,
    schema_version: 1,
    source_object_id: sourceObjectId,
    status,
    status_url: `https://spine.test/api/v1/documents/lifecycle/operations/${operationId}`,
  };
}
