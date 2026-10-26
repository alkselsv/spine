import { Button, Select } from '@base-ui/react';
import { useEffect, useState, type ComponentType, type SVGProps } from 'react';
import { NavLink, Outlet, useLocation, useNavigate, useSearchParams } from 'react-router';

import { createScopedApiClient, type SpineApiError } from '../api/client';
import {
  ActivityIcon,
  BookIcon,
  ChatIcon,
  ChevronIcon,
  HomeIcon,
  LayersIcon,
  MoonIcon,
  RefreshIcon,
  SettingsIcon,
  SunIcon,
} from '../components/icons';
import { ru } from '../i18n/resources';
import { useAuth } from './auth';
import { localizeApiError } from './errors';
import { useBrowserRuntime } from './runtime-context';
import { useTheme } from './theme';
import {
  parseScopeSearch,
  ScopeContext,
  scopeQueryKey,
  updateScopeSearch,
  type EnvironmentId,
  type Scope,
  type WorkspaceId,
} from './scope';
import styles from './AppShell.module.css';

interface NavigationItem {
  label: string;
  to: string;
  icon: ComponentType<Omit<SVGProps<SVGSVGElement>, 'children'>>;
  end?: boolean;
}

const navigationGroups: readonly { label?: string; items: readonly NavigationItem[] }[] = [
  { items: [{ label: ru.navigation.overview, to: '/', icon: HomeIcon, end: true }] },
  {
    label: ru.navigation.groupWork,
    items: [
      { label: ru.navigation.chat, to: '/chat', icon: ChatIcon },
      { label: ru.navigation.knowledge, to: '/knowledge/documents', icon: BookIcon },
    ],
  },
  {
    label: ru.navigation.groupObserve,
    items: [
      { label: ru.navigation.qaRuns, to: '/qa/runs', icon: ActivityIcon },
      { label: ru.navigation.runs, to: '/runs', icon: LayersIcon },
    ],
  },
];

const footerNavigation: readonly NavigationItem[] = [
  { label: ru.navigation.settings, to: '/settings', icon: SettingsIcon },
];

type ConnectionStatus = 'connected' | 'checking' | 'checked' | 'failed';

const connectionCopy: Record<ConnectionStatus, string> = {
  connected: ru.connection.connected,
  checking: ru.connection.checking,
  checked: ru.connection.checked,
  failed: ru.connection.failed,
};

export function AppShell() {
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const auth = useAuth();
  const runtime = useBrowserRuntime();
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('connected');
  const [connectionError, setConnectionError] = useState<SpineApiError | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => location.pathname.startsWith('/chat'));
  const scope = parseScopeSearch(`?${searchParams.toString()}`, runtime.scope);

  useEffect(() => {
    setSidebarCollapsed(location.pathname.startsWith('/chat'));
  }, [location.pathname]);

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

  const search = searchParams.toString();

  return (
    <ScopeContext.Provider value={{ scope, queryKey: (resource) => scopeQueryKey(resource, scope) }}>
      <div className={styles.shell} data-sidebar-collapsed={sidebarCollapsed}>
        <aside className={styles.sidebar}>
          <div className={styles.brandBlock}>
            <span className={styles.brandMark} aria-hidden="true">
              S
            </span>
            <div className={styles.brandText}>
              <h1>{ru.shellTitle}</h1>
              <p className={styles.brand}>{ru.brand}</p>
            </div>
            <button
              aria-label={sidebarCollapsed ? 'Развернуть навигацию' : 'Свернуть навигацию'}
              className={styles.sidebarToggle}
              onClick={() => setSidebarCollapsed((collapsed) => !collapsed)}
              type="button"
            >
              {sidebarCollapsed ? '›' : '‹'}
            </button>
          </div>

          <nav className={styles.navigation} aria-label={ru.navigation.label}>
            {navigationGroups.map((group, index) => (
              <div className={styles.navGroup} key={group.label ?? index}>
                {group.label ? <p className={styles.navigationLabel}>{group.label}</p> : null}
                {group.items.map((item) => (
                  <ShellNavLink item={item} key={item.to} search={search} />
                ))}
              </div>
            ))}
            <div className={`${styles.navGroup} ${styles.navGroupFooter}`}>
              {footerNavigation.map((item) => (
                <ShellNavLink item={item} key={item.to} search={search} />
              ))}
            </div>
          </nav>

          <SidebarFooter role={auth.role} />
        </aside>

        <div className={styles.frame}>
          <header className={styles.topbar}>
            <div
              aria-label="Рабочая область и среда"
              className={styles.scopeControls}
              role="group"
            >
              <ScopeSelector
                ariaLabel={ru.scope.workspaceLabel}
                options={runtime.scope.workspaceOptions}
                value={scope.workspaceId}
                onValueChange={(value) => changeScope({ workspaceId: value as WorkspaceId })}
              />
              <span className={styles.scopeDivider} aria-hidden="true">
                /
              </span>
              <ScopeSelector
                ariaLabel={ru.scope.environmentLabel}
                options={runtime.scope.environmentOptions}
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
                <RefreshIcon className={styles.refreshIcon} />
                {connectionStatus === 'checking' ? ru.connection.checking : ru.connection.check}
              </Button>
            </div>
          </header>
          <main className={styles.content} data-route={location.pathname.startsWith('/chat') ? 'chat' : 'default'}>
            {connectionError ? <ConnectionErrorPanel error={connectionError} /> : null}
            <Outlet />
          </main>
        </div>
      </div>
    </ScopeContext.Provider>
  );
}

function ShellNavLink({ item, search }: { item: NavigationItem; search: string }) {
  const IconComponent = item.icon;
  return (
    <NavLink
      className={({ isActive }) =>
        isActive ? `${styles.navLink} ${styles.navLinkActive}` : styles.navLink
      }
      end={item.end}
      to={{ pathname: item.to, search }}
    >
      <IconComponent className={styles.navIcon} />
      <span>{item.label}</span>
    </NavLink>
  );
}

function SidebarFooter({ role }: { role: 'administrator' | 'member' | undefined }) {
  const [theme, toggleTheme] = useTheme();
  const roleLabel = role ? ru.roles[role] : null;
  const isDark = theme === 'dark';
  return (
    <div className={styles.sidebarFooter}>
      {roleLabel ? (
        <span className={styles.identity}>
          <span className={styles.avatar} aria-hidden="true">
            {roleLabel.slice(0, 1)}
          </span>
          {roleLabel}
        </span>
      ) : (
        <span />
      )}
      <button
        aria-label={isDark ? ru.theme.enableLight : ru.theme.enableDark}
        className={styles.themeToggle}
        onClick={toggleTheme}
        title={isDark ? ru.theme.enableLight : ru.theme.enableDark}
        type="button"
      >
        {isDark ? <SunIcon /> : <MoonIcon />}
      </button>
    </div>
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
          <Select.Icon className={styles.scopeIcon}>
            <ChevronIcon />
          </Select.Icon>
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
