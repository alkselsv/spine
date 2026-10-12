import type { RouteObject } from 'react-router';
import { Link } from 'react-router';

import styles from './AppShell.module.css';
import { AppShell } from './AppShell';

function OverviewPage() {
  return (
    <section className={styles.pageSection} aria-labelledby="overview-heading">
      <p className={styles.eyebrow}>R1 workspace</p>
      <h2 id="overview-heading">Control Plane ready</h2>
      <p className={styles.lede}>
        The frontend foundation is ready for the Knowledge, Chat and Runs slices.
      </p>
      <Link className={styles.textLink} to="/knowledge/documents">
        Open Knowledge
      </Link>
    </section>
  );
}

function PlaceholderPage({ title }: { title: string }) {
  return (
    <section className={styles.pageSection} aria-labelledby="placeholder-heading">
      <p className={styles.eyebrow}>R1 workspace</p>
      <h2 id="placeholder-heading">{title}</h2>
      <p className={styles.lede}>This route is reserved for a later vertical slice.</p>
    </section>
  );
}

export const appRoutes: RouteObject[] = [
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <OverviewPage /> },
      {
        path: 'knowledge/documents',
        element: <PlaceholderPage title="Knowledge" />,
      },
      {
        path: 'knowledge/documents/:documentId',
        element: <PlaceholderPage title="Document details" />,
      },
      { path: 'chat', element: <PlaceholderPage title="Chat" /> },
      {
        path: 'chat/:sessionId',
        element: <PlaceholderPage title="Chat session" />,
      },
      { path: 'runs', element: <PlaceholderPage title="Runs" /> },
      { path: 'runs/:runId', element: <PlaceholderPage title="Run details" /> },
      { path: 'settings', element: <PlaceholderPage title="Settings" /> },
    ],
  },
];
