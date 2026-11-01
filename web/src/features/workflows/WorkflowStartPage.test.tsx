import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

const { post, navigate } = vi.hoisted(() => ({
  post: vi.fn(),
  navigate: vi.fn(),
}));

vi.mock('../../api/client', () => ({
  createScopedApiClient: () => ({ POST: post }),
  SpineApiError: class SpineApiError extends Error {
    safeMessage = 'Запрос отклонён';
  },
}));
vi.mock('../../app/auth', () => ({
  useAuth: () => ({ getBearerCredential: () => 'synthetic-token' }),
}));
vi.mock('../../app/scope', () => ({
  useScope: () => ({
    scope: {
      workspaceId: '10000000-0000-4000-8000-000000000001',
      environmentId: '20000000-0000-4000-8000-000000000001',
    },
  }),
}));
vi.mock('react-router', () => ({ useNavigate: () => navigate }));

import { WorkflowStartPage } from './WorkflowStartPage';

afterEach(() => {
  cleanup();
  post.mockReset();
  navigate.mockReset();
});

describe('WorkflowStartPage', () => {
  it('test_retry_reuses_idempotency_key_and_opens_admitted_run', async () => {
    post
      .mockResolvedValueOnce({ error: { message: 'temporary failure' } })
      .mockResolvedValueOnce({
        data: { run_id: '30000000-0000-4000-8000-000000000001' },
      });
    const user = userEvent.setup();
    render(<WorkflowStartPage />);

    await user.type(
      screen.getByLabelText('Сообщение'),
      'Подготовь требования для КП',
    );
    await user.click(screen.getByRole('button', { name: 'Начать работу' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Не удалось запустить работу. Повторите запрос.',
    );

    await user.click(screen.getByRole('button', { name: 'Начать работу' }));

    expect(post).toHaveBeenCalledTimes(2);
    expect(post.mock.calls[0]?.[1].params.header['Idempotency-Key']).toBe(
      post.mock.calls[1]?.[1].params.header['Idempotency-Key'],
    );
    expect(post.mock.calls[1]?.[1].body).toMatchObject({
      workflow_key: 'commercial.requirements',
      message: 'Подготовь требования для КП',
    });
    expect(navigate).toHaveBeenCalledWith(
      '/workflow/runs/30000000-0000-4000-8000-000000000001',
    );
  });

  it('test_lost_response_restores_the_form_and_retries_the_exact_start_command', async () => {
    post
      .mockRejectedValueOnce(new Error('connection closed after commit'))
      .mockResolvedValueOnce({
        data: { run_id: '30000000-0000-4000-8000-000000000001' },
      });
    const user = userEvent.setup();
    render(<WorkflowStartPage />);

    await user.type(
      screen.getByLabelText('Сообщение'),
      'Подготовь требования по принятой версии ТЗ',
    );
    await user.click(screen.getByRole('button', { name: 'Начать работу' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Не удалось запустить работу. Повторите запрос.',
    );
    expect(screen.getByRole('button', { name: 'Начать работу' })).toBeEnabled();

    await user.click(screen.getByRole('button', { name: 'Начать работу' }));

    expect(post).toHaveBeenCalledTimes(2);
    expect(post.mock.calls[1]?.[1]).toEqual(post.mock.calls[0]?.[1]);
    expect(navigate).toHaveBeenCalledWith(
      '/workflow/runs/30000000-0000-4000-8000-000000000001',
    );
  });

  it('test_source_edit_after_lost_response_starts_a_new_command_with_current_provenance', async () => {
    post
      .mockRejectedValueOnce(new Error('connection closed after commit'))
      .mockResolvedValueOnce({
        data: { run_id: '30000000-0000-4000-8000-000000000001' },
      });
    const user = userEvent.setup();
    render(<WorkflowStartPage />);

    await user.type(screen.getByLabelText('Сообщение'), 'Подготовь требования');
    await user.click(screen.getByRole('button', { name: 'Добавить принятую версию источника' }));
    await user.type(screen.getByLabelText('Source Object ID'), 'source-1');
    await user.type(screen.getByLabelText('Source Revision ID'), 'revision-1');
    await user.type(screen.getByLabelText('Provenance ID'), 'provenance-1');
    await user.click(screen.getByRole('button', { name: 'Начать работу' }));

    expect(await screen.findByRole('alert')).toBeVisible();
    await user.clear(screen.getByLabelText('Source Revision ID'));
    await user.type(screen.getByLabelText('Source Revision ID'), 'revision-2');
    await user.click(screen.getByRole('button', { name: 'Начать работу' }));

    expect(post).toHaveBeenCalledTimes(2);
    expect(post.mock.calls[1]?.[1].params.header['Idempotency-Key']).not.toBe(
      post.mock.calls[0]?.[1].params.header['Idempotency-Key'],
    );
    expect(post.mock.calls[1]?.[1].body.accepted_sources).toEqual([{
      source_object_id: 'source-1',
      source_revision_id: 'revision-2',
      provenance_id: 'provenance-1',
    }]);
  });
});
