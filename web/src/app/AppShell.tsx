import { Button } from '@base-ui/react/button';
import { useState } from 'react';
import { NavLink, Outlet } from 'react-router';

import styles from './AppShell.module.css';

const navigation = [
  { label: 'Knowledge', to: '/knowledge/documents' },
  { label: 'Chat', to: '/chat' },
  { label: 'Runs', to: '/runs' },
  { label: 'Settings', to: '/settings' },
] as const;

export function AppShell() {
  const [statusMessage, setStatusMessage] = useState('Workspace connected');

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <div>
          <p className={styles.brand}>SPINE</p>
          <h1>Spine Control Plane</h1>
        </div>
        <div className={styles.context}>
          <span>Workspace: <strong>default</strong></span>
          <span>Environment: <strong>local</strong></span>
          <span className={styles.status} role="status" aria-live="polite">
            <span className={styles.statusDot} aria-hidden="true" />
            {statusMessage}
          </span>
          <Button
            className={styles.refreshButton}
            onClick={() => setStatusMessage('Connection checked')}
            type="button"
          >
            Check connection
          </Button>
        </div>
      </header>

      <div className={styles.layout}>
        <nav className={styles.navigation} aria-label="Primary navigation">
          <span className={styles.navigationLabel}>Workspace</span>
          {navigation.map((item) => (
            <NavLink
              className={({ isActive }) =>
                isActive ? `${styles.navLink} ${styles.navLinkActive}` : styles.navLink
              }
              key={item.to}
              to={item.to}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <main className={styles.content}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
