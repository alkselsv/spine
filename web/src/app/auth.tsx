import type { ReactNode } from 'react';
import { createContext, useContext } from 'react';

import { ru } from '../i18n/resources';
import styles from './AppShell.module.css';

export type AuthenticationStatus =
  | 'authenticated'
  | 'unauthenticated'
  | 'expired'
  | 'unavailable'
  | 'forbidden';

export interface AuthState {
  status: AuthenticationStatus;
  role?: 'administrator' | 'member';
  /**
   * The identity adapter injects the access token. The shell never accepts a
   * token from a route, request body or user-controlled scope.
   */
  getBearerCredential?: () => Promise<string> | string;
}

const defaultAuthState: AuthState = {
  status: 'authenticated',
  role: 'administrator',
};

const AuthContext = createContext<AuthState>(defaultAuthState);

export function AuthProvider({ children, value }: { children: ReactNode; value: AuthState }) {
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  return useContext(AuthContext);
}

export function AdministratorBoundary({ children }: { children: ReactNode }) {
  const auth = useAuth();
  if (auth.status === 'authenticated' && auth.role === 'administrator') {
    return children;
  }

  const copy = ru.auth[auth.status === 'authenticated' ? 'forbidden' : auth.status];
  return (
    <main className={styles.authBoundary} aria-labelledby="auth-boundary-heading">
      <section className={styles.pageSection}>
        <p className={styles.eyebrow}>{ru.brand}</p>
        <h1 id="auth-boundary-heading">{copy.title}</h1>
        <p className={styles.lede}>{copy.description}</p>
        <button className={styles.primaryButton} type="button">
          {copy.action}
        </button>
      </section>
    </main>
  );
}
