import { Button, Select } from '@base-ui/react';
import { useState } from 'react';
import { NavLink, Outlet, useNavigate, useSearchParams } from 'react-router';

import { createScopedApiClient, type SpineApiError } from '../api/client';
import { ru } from '../i18n/resources';
import { useAuth } from './auth';
import { localizeApiError } from './errors';
import {
  environmentOptions,
  parseScopeSearch,
  ScopeContext,
  scopeQueryKey,
  updateScopeSearch,
  workspaceOptions,
  type EnvironmentId,
  type Scope,
  type WorkspaceId,
} from './scope';
import styles from './AppShell.module.css';

const navigation = [
  { label: ru.navigation.knowledge, to: '/knowledge/documents' },
  { label: ru.navigation.chat, to: '/chat/demo-session' },
  { label: ru.navigation.runs, to: '/runs' },
  { label: ru.navigation.settings, to: '/settings' },
] as const;

export function AppShell() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const auth = useAuth();
  const [statusMessage, setStatusMessage] = useState<string>(ru.connection.connected);
  const [connectionError, setConnectionError] = useState<SpineApiError | null>(null);
  const [isCheckingConnection, setIsCheckingConnection] = useState(false);
  const scope = parseScopeSearch(`?${searchParams.toString()}`);

  function changeScope(next: Partial<Scope>) {
    const nextScope: Scope = { ...scope, ...next };
    const nextSearch = new URLSearchParams(updateScopeSearch(searchParams.toString(), nextScope));
    if (
      nextScope.workspaceId !== scope.workspaceId ||
      nextScope.environmentId !== scope.environmentId
    ) {
      nextSearch.delete('cursor');
      nextSearch.delete('processing_cursor');
    }
    navigate({ search: `?${nextSearch.toString()}` }, {
      replace: true,
    });
    setConnectionError(null);
  }

  async function checkConnection() {
    if (!auth.getBearerCredential) {
      setStatusMessage(ru.connection.checked);
      return;
    }

    setIsCheckingConnection(true);
    setConnectionError(null);
    try {
      const client = createScopedApiClient({
        baseUrl: window.location.origin,
        getBearerCredential: auth.getBearerCredential,
        scope,
      });
      await client.GET('/api/v1/health/ready');
      setStatusMessage(ru.connection.checked);
    } catch (error) {
      setConnectionError(error instanceof Error ? (error as SpineApiError) : null);
      setStatusMessage(ru.connection.failed);
    } finally {
      setIsCheckingConnection(false);
    }
  }

  return (
    <ScopeContext.Provider value={{ scope, queryKey: (resource) => scopeQueryKey(resource, scope) }}>
      <div className={styles.shell}>
        <header className={styles.header}>
          <div>
            <p className={styles.brand}>{ru.brand}</p>
            <h1>{ru.shellTitle}</h1>
          </div>
          <div className={styles.context}>
            <ScopeSelector
              ariaLabel={ru.scope.workspaceLabel}
              options={workspaceOptions}
              value={scope.workspaceId}
              onValueChange={(value) => changeScope({ workspaceId: value as WorkspaceId })}
            />
            <ScopeSelector
              ariaLabel={ru.scope.environmentLabel}
              options={environmentOptions}
              value={scope.environmentId}
              onValueChange={(value) => changeScope({ environmentId: value as EnvironmentId })}
            />
            <span className={styles.status} role="status" aria-live="polite">
              <span className={styles.statusDot} aria-hidden="true" />
              {isCheckingConnection ? ru.connection.checking : statusMessage}
            </span>
            <Button
              className={styles.refreshButton}
              disabled={isCheckingConnection}
              onClick={() => void checkConnection()}
              type="button"
            >
              {isCheckingConnection ? ru.connection.checking : ru.connection.check}
            </Button>
          </div>
        </header>

        <div className={styles.layout}>
          <nav className={styles.navigation} aria-label={ru.navigation.label}>
            <span className={styles.navigationLabel}>{ru.navigation.label}</span>
            {navigation.map((item) => (
              <NavLink
                className={({ isActive }) =>
                  isActive ? `${styles.navLink} ${styles.navLinkActive}` : styles.navLink
                }
                key={item.to}
                to={{ pathname: item.to, search: searchParams.toString() }}
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <main className={styles.content}>
            {connectionError ? <ConnectionErrorPanel error={connectionError} /> : null}
            <Outlet />
          </main>
        </div>
      </div>
    </ScopeContext.Provider>
  );
}

function ConnectionErrorPanel({ error }: { error: SpineApiError }) {
  const copy = localizeApiError(error);
  return (
    <div className={`${styles.statusPanel} ${styles[`statusPanel_${copy.state}`]}`} role="alert">
      <strong>{copy.title}</strong>
      <span>{copy.description}</span>
      {copy.traceId ? <small>Код обращения: {copy.traceId}</small> : null}
    </div>
  );
}

function ScopeSelector<Value extends string>({
  ariaLabel,
  options,
  value,
  onValueChange,
}: {
  ariaLabel: string;
  options: readonly { id: Value; label: string }[];
  value: Value;
  onValueChange: (value: Value) => void;
}) {
  return (
    <div className={styles.scopeSelector}>
      <span className={styles.scopeLabel}>{ariaLabel}</span>
      <Select.Root
        items={options.map((option) => ({ label: option.label, value: option.id }))}
        value={value}
        onValueChange={(nextValue) => {
          if (nextValue !== null) onValueChange(nextValue as Value);
        }}
      >
        <Select.Trigger className={styles.scopeTrigger} aria-label={ariaLabel}>
          <Select.Value />
          <Select.Icon className={styles.scopeIcon}>⌄</Select.Icon>
        </Select.Trigger>
        <Select.Portal>
          <Select.Positioner className={styles.scopePositioner} sideOffset={4}>
            <Select.Popup className={styles.scopePopup}>
              <Select.List>
                {options.map((option) => (
                  <Select.Item className={styles.scopeOption} key={option.id} value={option.id}>
                    <Select.ItemText>{option.label}</Select.ItemText>
                  </Select.Item>
                ))}
              </Select.List>
            </Select.Popup>
          </Select.Positioner>
        </Select.Portal>
      </Select.Root>
    </div>
  );
}
