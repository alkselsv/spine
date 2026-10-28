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
});
