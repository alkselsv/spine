import type { RouteObject } from 'react-router';
import { Link } from 'react-router';

import { ru } from '../i18n/resources';
import { AdministratorBoundary } from './auth';
import styles from './AppShell.module.css';
import { AppShell } from './AppShell';
import { localizeApiError } from './errors';

export type InterfaceState =
  | 'loading'
  | 'empty'
  | 'partial'
  | 'stale'
  | 'degraded'
  | 'permissionDenied'
  | 'validationFailed'
  | 'failed'
  | 'ready';

export function StatusPanel({ state }: { state: InterfaceState }) {
  const copy = ru.status[state];
  return (
    <div
      aria-label={copy.title}
      className={`${styles.statusPanel} ${styles[`statusPanel_${state}`]}`}
      role="region"
    >
      <strong>{copy.title}</strong>
      <span>{copy.description}</span>
    </div>
  );
}

export function ApiErrorPanel({ error }: { error: unknown }) {
  const copy = localizeApiError(error);
  return (
    <div
      className={`${styles.statusPanel} ${styles[`statusPanel_${copy.state}`]}`}
      role="alert"
    >
      <strong>{copy.title}</strong>
      <span>{copy.description}</span>
      {copy.traceId ? <small>Код обращения: {copy.traceId}</small> : null}
    </div>
  );
}

function OverviewPage() {
  return (
    <section className={styles.pageSection} aria-labelledby="overview-heading">
      <p className={styles.eyebrow}>{ru.page.eyebrow}</p>
      <h2 id="overview-heading">{ru.page.overviewTitle}</h2>
      <p className={styles.lede}>{ru.page.overviewDescription}</p>
      <StatusPanel state="ready" />
      <Link className={styles.textLink} to="/knowledge/documents">
        {ru.page.openKnowledge}
      </Link>
    </section>
  );
}

function PlaceholderPage({ title }: { title: string }) {
  return (
    <section className={styles.pageSection} aria-labelledby="placeholder-heading">
      <p className={styles.eyebrow}>{ru.page.eyebrow}</p>
      <h2 id="placeholder-heading">{title}</h2>
      <p className={styles.lede}>{ru.page.reserved}</p>
      <StatusPanel state="ready" />
    </section>
  );
}

export const appRoutes: RouteObject[] = [
  {
    path: '/',
    element: (
      <AdministratorBoundary>
        <AppShell />
      </AdministratorBoundary>
    ),
    children: [
      { index: true, element: <OverviewPage /> },
      {
        path: 'knowledge/documents',
        element: <PlaceholderPage title={ru.page.knowledge} />,
      },
      {
        path: 'knowledge/documents/:documentId',
        element: <PlaceholderPage title={ru.page.documentDetails} />,
      },
      {
        path: 'chat/:sessionId',
        element: <PlaceholderPage title={ru.page.chatSession} />,
      },
      { path: 'runs/:runId', element: <PlaceholderPage title={ru.page.runDetails} /> },
      { path: 'settings', element: <PlaceholderPage title={ru.page.settings} /> },
    ],
  },
];
