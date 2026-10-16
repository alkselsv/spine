import type { RouteObject } from 'react-router';
import { Link, useParams, useSearchParams } from 'react-router';
import { useMemo, useState } from 'react';

import { ChatPanel } from '../features/chat/ChatPanel';
import { createQualificationChatHarness } from '../features/chat/qualification';
import { ru } from '../i18n/resources';
import { AdministratorBoundary } from './auth';
import styles from './AppShell.module.css';
import { AppShell } from './AppShell';
import { readBrowserQualificationFixture } from './fixture';
import { useScope } from './scope';
import { StatusPanel } from './status';

export { ApiErrorPanel, StatusPanel } from './status';
export type { InterfaceState } from './status';

function OverviewPage() {
  const [searchParams] = useSearchParams();
  return (
    <section className={styles.pageSection} aria-labelledby="overview-heading">
      <p className={styles.eyebrow}>{ru.page.eyebrow}</p>
      <h2 id="overview-heading">{ru.page.overviewTitle}</h2>
      <p className={styles.lede}>{ru.page.overviewDescription}</p>
      <StatusPanel state="ready" />
      <Link
        className={styles.textLink}
        to={{ pathname: '/knowledge/documents', search: searchParams.toString() }}
      >
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

function ChatPage() {
  const { sessionId = 'demo-session' } = useParams();
  const { scope } = useScope();
  const [reconnectCount, setReconnectCount] = useState(0);
  const fixture = readBrowserQualificationFixture();
  const harness = useMemo(
    () =>
      fixture === 'controlled'
        ? createQualificationChatHarness(scope, (attempt) => setReconnectCount(attempt))
        : null,
    [fixture, scope],
  );

  if (!harness) return <PlaceholderPage title={ru.page.chatSession} />;
  return (
    <ChatPanel
      reconnectCount={reconnectCount}
      sessionId={sessionId}
      transport={harness.transport}
    />
  );
}

function DocumentDetailPage() {
  const { documentId } = useParams();
  const [searchParams] = useSearchParams();
  return (
    <section className={styles.pageSection} aria-labelledby="document-detail-heading">
      <p className={styles.eyebrow}>{ru.page.eyebrow}</p>
      <h2 id="document-detail-heading">{ru.page.documentDetails}</h2>
      <p className={styles.lede}>{ru.documents.detailDescription}</p>
      <p>
        {ru.documents.detailSourceId}: <code>{documentId}</code>
      </p>
      <Link
        className={styles.textLink}
        to={{ pathname: '/knowledge/documents', search: searchParams.toString() }}
      >
        {ru.documents.backToList}
      </Link>
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
        lazy: async () => {
          const module = await import('../features/knowledge/DocumentsPage');
          return { Component: module.DocumentsPage };
        },
      },
      {
        path: 'knowledge/documents/:documentId',
        element: <DocumentDetailPage />,
      },
      {
        path: 'chat/:sessionId',
        element: <ChatPage />,
      },
      {
        path: 'runs',
        lazy: async () => {
          const module = await import('../features/knowledge/IngestionRunPage');
          return { Component: module.IngestionRunPage };
        },
      },
      {
        path: 'runs/:runId',
        lazy: async () => {
          const module = await import('../features/knowledge/IngestionRunPage');
          return { Component: module.IngestionRunPage };
        },
      },
      { path: 'settings', element: <PlaceholderPage title={ru.page.settings} /> },
    ],
  },
];
