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

type ConnectionStatus = 'connected' | 'checking' | 'checked' | 'failed';

const connectionCopy: Record<ConnectionStatus, string> = {
  connected: ru.connection.connected,
  checking: ru.connection.checking,
  checked: ru.connection.checked,
  failed: ru.connection.failed,
};

export function AppShell() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const auth = useAuth();
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('connected');
  const [connectionError, setConnectionError] = useState<SpineApiError | null>(null);
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
    setConnectionStatus('connected');
  }

  async function checkConnection() {
    if (!auth.getBearerCredential) {
      setConnectionStatus('checked');
      return;
    }

    setConnectionStatus('checking');
    setConnectionError(null);
    try {
      const client = createScopedApiClient({
        baseUrl: window.location.origin,
        getBearerCredential: auth.getBearerCredential,
        scope,
      });
      await client.GET('/api/v1/health/ready');
      setConnectionStatus('checked');
    } catch (error) {
      setConnectionError(error instanceof Error ? (error as SpineApiError) : null);
      setConnectionStatus('failed');
    }
  }

  return (
    <ScopeContext.Provider value={{ scope, queryKey: (resource) => scopeQueryKey(resource, scope) }}>
      <div className={styles.shell}>
        <header className={styles.header}>
          <div className={styles.brandBlock}>
            <p className={styles.brand}>{ru.brand}</p>
            <h1>{ru.shellTitle}</h1>
          </div>
          <div className={styles.context}>
            <div
              aria-label="Рабочая область и среда"
              className={styles.scopeControls}
              role="group"
            >
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
            </div>
            <div aria-label="Состояние соединения" className={styles.connectionControls} role="group">
              <span
                className={styles.status}
                data-state={connectionStatus}
                role="status"
                aria-live="polite"
              >
                <span className={styles.statusDot} aria-hidden="true" />
                {connectionCopy[connectionStatus]}
              </span>
              <Button
                className={styles.refreshButton}
                disabled={connectionStatus === 'checking'}
                onClick={() => void checkConnection()}
                type="button"
              >
                {connectionStatus === 'checking' ? ru.connection.checking : ru.connection.check}
              </Button>
            </div>
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
