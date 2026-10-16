import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useRef } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router';

import { createScopedApiClient } from '../../api/client';
import { apiQueryOptions } from '../../api/query';
import { useAuth } from '../../app/auth';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import { useScope } from '../../app/scope';
import { formatDate } from '../../i18n/format';
import { ru } from '../../i18n/resources';
import {
  createQASession,
  loadQAMessagePage,
  loadQASession,
  loadQASessionPage,
  qaSessionQueryKeys,
  restoreQASession,
  type QAMessage,
} from './session-api';
import styles from './SessionBrowserPage.module.css';

const SESSION_CURSOR = 'session_cursor';
const MESSAGE_CURSOR = 'message_cursor';
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function SessionBrowserPage() {
  const auth = useAuth();
  const { scope } = useScope();
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const identityVersion = auth.identityVersion ?? '';
  const previousContext = useRef({ scope, identityVersion });
  const scopeChanged =
    previousContext.current.scope.workspaceId !== scope.workspaceId ||
    previousContext.current.scope.environmentId !== scope.environmentId;
  const identityChanged = previousContext.current.identityVersion !== identityVersion;
  const scopeSearch = new URLSearchParams({
    workspace: scope.workspaceId,
    environment: scope.environmentId,
  }).toString();
  const client = useMemo(
    () =>
      createScopedApiClient({
        baseUrl: window.location.origin,
        getBearerCredential: auth.getBearerCredential ?? (() => ''),
        scope,
      }),
    [auth.getBearerCredential, scope],
  );

  useEffect(() => {
    const previous = previousContext.current;
    if (scopeChanged) {
      void queryClient.removeQueries({ queryKey: qaSessionQueryKeys.scope(previous.scope) });
      navigate({ pathname: '/chat', search: `?${scopeSearch}` }, { replace: true });
    } else if (identityChanged && previous.identityVersion) {
      void queryClient.removeQueries({
        queryKey: qaSessionQueryKeys.identity(previous.scope, previous.identityVersion),
      });
    }
    previousContext.current = { scope, identityVersion };
  }, [identityChanged, identityVersion, navigate, queryClient, scope, scopeChanged, scopeSearch]);

  const sessionCursor = readCursor(searchParams.get(SESSION_CURSOR));
  const messageCursor = readCursor(searchParams.get(MESSAGE_CURSOR));
  const sessionsQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadQASessionPage(client, scope, sessionCursor, signal),
      queryKey: qaSessionQueryKeys.sessions(scope, sessionCursor, identityVersion),
    }),
    enabled: Boolean(auth.getBearerCredential && identityVersion && !sessionId && !scopeChanged),
  });
  const validSessionId = sessionId && UUID_PATTERN.test(sessionId) ? sessionId : null;
  const sessionQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadQASession(client, scope, validSessionId ?? '', signal),
      queryKey: qaSessionQueryKeys.session(scope, validSessionId ?? '', identityVersion),
    }),
    enabled: Boolean(auth.getBearerCredential && identityVersion && validSessionId && !scopeChanged),
  });
  const messagesQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadQAMessagePage(client, scope, validSessionId ?? '', messageCursor, signal),
      queryKey: qaSessionQueryKeys.messages(scope, validSessionId ?? '', messageCursor, identityVersion),
    }),
    enabled: Boolean(
      auth.getBearerCredential &&
        identityVersion &&
        validSessionId &&
        sessionQuery.data?.status === 'active' &&
        !scopeChanged,
    ),
  });

  const createKey = useRef<string | null>(null);
  const createMutation = useMutation({
    mutationFn: () => createQASession(client, scope, createKey.current ?? createIdempotencyKey()),
    onSuccess: async (created) => {
      createKey.current = null;
      await queryClient.invalidateQueries({ queryKey: qaSessionQueryKeys.scope(scope) });
      navigate({ pathname: `/chat/${created.session_id}`, search: `?${scopeSearch}` });
    },
  });
  const restoreKey = useRef<string | null>(null);
  const restoreMutation = useMutation({
    mutationFn: () => {
      if (!sessionQuery.data) throw new Error('The Q&A session is unavailable.');
      restoreKey.current ??= createIdempotencyKey();
      return restoreQASession(
        client,
        scope,
        sessionQuery.data.session_id,
        sessionQuery.data.version,
        restoreKey.current,
      );
    },
    onSuccess: async (restored) => {
      restoreKey.current = null;
      queryClient.setQueryData(
        qaSessionQueryKeys.session(scope, restored.session_id, identityVersion),
        restored,
      );
      await queryClient.invalidateQueries({
        queryKey: qaSessionQueryKeys.messages(scope, restored.session_id, null, identityVersion),
      });
    },
  });

  if (!auth.getBearerCredential || !identityVersion) return <StatusPanel state="degraded" />;
  if (scopeChanged) return <StatusPanel state="loading" />;
  if (sessionId) {
    if (!validSessionId) {
      return <StatusPanel state="failed" />;
    }
    return (
      <SessionDetails
        messageCursor={messageCursor}
        messageQuery={messagesQuery}
        onBack={() => navigate({ pathname: '/chat', search: `?${scopeSearch}` })}
        onNextMessages={() => {
          const cursor = messagesQuery.data?.next_cursor;
          if (!cursor) return;
          const next = new URLSearchParams(searchParams);
          next.delete(SESSION_CURSOR);
          next.set(MESSAGE_CURSOR, cursor);
          setSearchParams(next);
        }}
        onRestore={() => {
          restoreKey.current ??= createIdempotencyKey();
          restoreMutation.mutate();
        }}
        restoreError={restoreMutation.error}
        restorePending={restoreMutation.isPending}
        sessionQuery={sessionQuery}
      />
    );
  }

  return (
    <SessionList
      createError={createMutation.error}
      createPending={createMutation.isPending}
      currentSearch={scopeSearch}
      onCreate={() => {
        createKey.current ??= createIdempotencyKey();
        createMutation.mutate();
      }}
      onNextPage={() => {
        const cursor = sessionsQuery.data?.next_cursor;
        if (!cursor) return;
        const next = new URLSearchParams(searchParams);
        next.delete(MESSAGE_CURSOR);
        next.set(SESSION_CURSOR, cursor);
        setSearchParams(next);
      }}
      query={sessionsQuery}
    />
  );
}

function SessionList({
  createError,
  createPending,
  currentSearch,
  onCreate,
  onNextPage,
  query,
}: {
  createError: unknown;
  createPending: boolean;
  currentSearch: string;
  onCreate: () => void;
  onNextPage: () => void;
  query: ReturnType<typeof useQuery<Awaited<ReturnType<typeof loadQASessionPage>>>>;
}) {
  return (
    <section className={styles.page} aria-labelledby="sessions-heading">
      <header className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>{ru.page.eyebrow}</p>
          <h2 id="sessions-heading">{ru.chat.sessionsTitle}</h2>
          <p className={styles.lede}>{ru.chat.sessionsDescription}</p>
        </div>
        <button className={styles.primaryButton} disabled={createPending} onClick={onCreate} type="button">
          {createPending ? ru.chat.creatingSession : createError ? ru.chat.retryCreateSession : ru.chat.createSession}
        </button>
      </header>

      {createError ? <ApiErrorPanel error={createError} /> : null}
      {query.error && !query.data ? <ApiErrorPanel error={query.error} /> : null}
      {query.isPending ? <StatusPanel state="loading" /> : null}
      {query.isStale && query.data ? <StatusPanel state="stale" /> : null}
      {query.error && query.data ? <StatusPanel state="partial" /> : null}
      {!query.isStale && !query.error && query.data?.items.length === 0 ? <StatusPanel state="empty" /> : null}

      {!query.isStale && !query.error && query.data?.items.length ? (
        <div className={styles.sessionList} aria-label={ru.chat.sessionsTitle}>
          {query.data.items.map((session) => (
            <Link
              className={styles.sessionLink}
              key={session.session_id}
              to={{ pathname: `/chat/${session.session_id}`, search: `?${currentSearch}` }}
            >
              <span className={styles.sessionHeading}>
                <strong>{ru.chat.sessionName(session.session_id.slice(-8))}</strong>
                <span className={styles.sessionStatus} data-state={session.status}>
                  {session.status === 'active' ? ru.chat.sessionActive : ru.chat.sessionArchived}
                </span>
              </span>
              <span className={styles.sessionDate}>{formatDate(new Date(session.updated_at))}</span>
            </Link>
          ))}
        </div>
      ) : null}

      {!query.isStale && !query.error && query.data?.next_cursor ? (
        <button className={styles.secondaryButton} onClick={onNextPage} type="button">
          {ru.documents.nextPage}
        </button>
      ) : null}
      {query.error ? (
        <button className={styles.secondaryButton} onClick={() => void query.refetch()} type="button">
          {ru.chat.retrySessions}
        </button>
      ) : null}
      {query.isStale && query.data ? (
        <button className={styles.secondaryButton} onClick={() => void query.refetch()} type="button">
          {ru.chat.refreshSessions}
        </button>
      ) : null}
    </section>
  );
}

function SessionDetails({
  messageCursor,
  messageQuery,
  onBack,
  onNextMessages,
  onRestore,
  restoreError,
  restorePending,
  sessionQuery,
}: {
  messageCursor: string | null;
  messageQuery: ReturnType<typeof useQuery<Awaited<ReturnType<typeof loadQAMessagePage>>>>;
  onBack: () => void;
  onNextMessages: () => void;
  onRestore: () => void;
  restoreError: unknown;
  restorePending: boolean;
  sessionQuery: ReturnType<typeof useQuery<Awaited<ReturnType<typeof loadQASession>>>>;
}) {
  const session = sessionQuery.isStale || sessionQuery.error ? undefined : sessionQuery.data;
  const messages = messageQuery.isStale || messageQuery.error ? undefined : messageQuery.data?.items;
  return (
    <section className={styles.page} aria-labelledby="session-heading">
      <header className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>{ru.chat.sessionsTitle}</p>
          <h2 id="session-heading">
            {session ? ru.chat.sessionName(session.session_id.slice(-8)) : ru.chat.sessionDetailsTitle}
          </h2>
          {session ? (
            <p className={styles.lede}>
              {ru.chat.sessionUpdated(formatDate(new Date(session.updated_at)))} · {ru.chat.sessionVersion(session.version)}
            </p>
          ) : null}
        </div>
        <button className={styles.secondaryButton} onClick={onBack} type="button">
          {ru.chat.backToSessions}
        </button>
      </header>

      {sessionQuery.error ? <ApiErrorPanel error={sessionQuery.error} /> : null}
      {sessionQuery.error ? (
        <button className={styles.secondaryButton} onClick={() => void sessionQuery.refetch()} type="button">
          {ru.chat.retrySession}
        </button>
      ) : null}
      {sessionQuery.isPending ? <StatusPanel state="loading" /> : null}
      {sessionQuery.isStale && sessionQuery.data ? <StatusPanel state="stale" /> : null}
      {sessionQuery.isStale && sessionQuery.data ? (
        <button className={styles.secondaryButton} onClick={() => void sessionQuery.refetch()} type="button">
          {ru.chat.refreshSession}
        </button>
      ) : null}

      {session?.status === 'archived' ? (
        <div className={styles.recoveryPanel}>
          <strong>{ru.chat.archivedTitle}</strong>
          <span>{ru.chat.archivedDescription}</span>
          {restoreError ? <ApiErrorPanel error={restoreError} /> : null}
          <button className={styles.primaryButton} disabled={restorePending} onClick={onRestore} type="button">
            {restorePending ? ru.chat.restoringSession : ru.chat.restoreSession}
          </button>
        </div>
      ) : null}

      {session?.status === 'active' ? (
        <section className={styles.messagePanel} aria-labelledby="messages-heading">
          <h3 id="messages-heading">{ru.chat.messagesTitle}</h3>
          {messageQuery.error && !messages ? <ApiErrorPanel error={messageQuery.error} /> : null}
          {messageQuery.isPending ? <StatusPanel state="loading" /> : null}
          {messageQuery.isStale && messageQuery.data ? <StatusPanel state="stale" /> : null}
          {messageQuery.error && messages ? <StatusPanel state="partial" /> : null}
          {!messageQuery.isStale && messages?.length === 0 ? <StatusPanel state="empty" /> : null}
          {messages?.length ? (
            <div className={styles.messageList} aria-label={ru.chat.messagesTitle}>
              {messages.map((message) => <MessageCard key={message.message_id} message={message} />)}
            </div>
          ) : null}
          {!messageQuery.isStale && !messageQuery.error && messageQuery.data?.next_cursor ? (
            <button className={styles.secondaryButton} onClick={onNextMessages} type="button">
              {ru.chat.nextMessages}
            </button>
          ) : null}
          {messageQuery.error ? (
            <button className={styles.secondaryButton} onClick={() => void messageQuery.refetch()} type="button">
              {ru.chat.retryMessages}
            </button>
          ) : null}
          {messageQuery.isStale && messageQuery.data ? (
            <button className={styles.secondaryButton} onClick={() => void messageQuery.refetch()} type="button">
              {ru.chat.refreshMessages}
            </button>
          ) : null}
          {messageCursor ? <span className={styles.pageHint}>{ru.chat.messagePageLoaded}</span> : null}
        </section>
      ) : null}

      {session ? (
        <footer className={styles.identity}>
          <span>{ru.chat.canonicalSessionId}</span>
          <code>{session.session_id}</code>
          <span>{ru.chat.sessionScope(session.workspace_id, session.environment_id)}</span>
        </footer>
      ) : null}
    </section>
  );
}

function MessageCard({ message }: { message: QAMessage }) {
  return (
    <article className={styles.messageCard}>
      <div className={styles.messageMeta}>
        <time dateTime={message.created_at}>{formatDate(new Date(message.created_at))}</time>
        <span>{messageStateLabel(message)}</span>
      </div>
      {message.disclosure_eligibility === 'ineligible' ? (
        <p className={styles.protectedNotice}>{ru.chat.messageContentHidden}</p>
      ) : (
        <>
          {message.question ? (
            <div>
              <span className={styles.messageLabel}>{ru.chat.questionLabel}</span>
              <p>{message.question}</p>
            </div>
          ) : null}
        </>
      )}
    </article>
  );
}

function messageStateLabel(message: QAMessage): string {
  if (message.state === 'pending') return ru.chat.messagePending;
  if (message.state === 'running') return ru.chat.messageRunning;
  switch (message.terminal_outcome) {
    case 'answered': return ru.chat.messageAnswered;
    case 'clarification_required': return ru.chat.messageClarificationRequired;
    case 'abstained': return ru.chat.messageAbstained;
    case 'denied': return ru.chat.messageDenied;
    case 'failed': return ru.chat.messageFailed;
    case 'cancelled': return ru.chat.messageCancelled;
    default: return ru.chat.messageOutcomeUnknown;
  }
}

function readCursor(cursor: string | null): string | null {
  return cursor && cursor.length <= 768 ? cursor : null;
}

function createIdempotencyKey(): string {
  return window.crypto.randomUUID();
}
