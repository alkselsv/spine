import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import {
  AdministratorBoundary,
  AuthProvider,
  type AuthState,
} from './auth';

afterEach(cleanup);

describe('administrator authentication boundary', () => {
  it.each([
    ['unauthenticated', 'Войдите в Control Plane'],
    ['expired', 'Сеанс истёк'],
    ['unavailable', 'Сервис аутентификации недоступен'],
    ['forbidden', 'Доступ запрещён'],
  ] as const)('test_%s_status_has_safe_localized_outcome', (status, heading) => {
    renderWithAuth({ status });

    expect(screen.getByRole('heading', { name: heading })).toBeVisible();
    expect(screen.queryByText(/stack|provider|secret/i)).not.toBeInTheDocument();
  });

  it('test_authenticated_administrator_reaches_protected_content', () => {
    renderWithAuth({ status: 'authenticated', role: 'administrator' });

    expect(screen.getByText('Защищённый раздел')).toBeVisible();
  });
});

function renderWithAuth(state: AuthState) {
  return render(
    <AuthProvider value={state}>
      <AdministratorBoundary>
        <p>Защищённый раздел</p>
      </AdministratorBoundary>
    </AuthProvider>,
  );
}
