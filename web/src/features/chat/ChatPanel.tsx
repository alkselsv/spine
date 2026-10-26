import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router';

import { createQaApiClient, createScopedApiClient } from '../../api/client';
import { apiQueryOptions } from '../../api/query';
import { useAuth } from '../../app/auth';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import { useScope, type Scope } from '../../app/scope';
import { formatDate, formatNumber } from '../../i18n/format';
import { ru } from '../../i18n/resources';
import { WorkspaceLayout } from '../../components/WorkspaceLayout';
import {
  createQASession,
  loadQAMessage,
  loadQAMessagePage,
  loadQASession,
  loadQASessionPage,
  qaSessionQueryKeys,
  restoreQASession,
  type QAMessage,
} from './session-api';
import { AcceptedAnswer } from './AcceptedAnswer';
import { CompletenessPresentation } from './CompletenessPresentation';
import { AskComposer } from './AskComposer';
import { AskCancellationControl } from './AskCancellationControl';
import { DiagnosticChatPanel, type DiagnosticChatPanelProps } from './DiagnosticChatPanel';
import { observeQAMessageEvents } from './qa-event-observer';
import styles from './SessionBrowserPage.module.css';

const SESSION_CURSOR = 'session_cursor';
const MESSAGE_CURSOR = 'message_cursor';
const ACTIVE_DISCLOSURE_REFRESH_MS = 15_000;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/**
 * The route's final Chat composition. Supplying the legacy transport props is
 * reserved for the explicitly selected controlled browser fixture.
 */
export function ChatPanel(props: Partial<DiagnosticChatPanelProps> = {}) {
  if (isDiagnosticProps(props)) return <DiagnosticChatPanel {...props} />;
  return <ChatExperience />;
}

function isDiagnosticProps(
  props: Partial<DiagnosticChatPanelProps>,
): props is DiagnosticChatPanelProps {
  return typeof props.sessionId === 'string' &&
    typeof props.transport === 'object' &&
    typeof props.reconnectCount === 'number';
}

function ChatExperience() {
  const auth = useAuth();
  const { scope } = useScope();
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const identityVersion = auth.identityVersion ?? '';
  const sessionCursor = readCursor(searchParams.get(SESSION_CURSOR));
  const messageCursor = readCursor(searchParams.get(MESSAGE_CURSOR));
  const previousContext = useRef({ scope, identityVersion });
  const scopeChanged =
    previousContext.current.scope.workspaceId !== scope.workspaceId ||
    previousContext.current.scope.environmentId !== scope.environmentId;
  const identityChanged = previousContext.current.identityVersion !== identityVersion;
  const scopeSearch = new URLSearchParams({
    workspace: scope.workspaceId,
    environment: scope.environmentId,
  }).toString();
  const sessionSearchParams = new URLSearchParams(scopeSearch);
  if (sessionCursor) sessionSearchParams.set(SESSION_CURSOR, sessionCursor);
  const sessionSearch = sessionSearchParams.toString();
  const client = useMemo(
    () =>
      createScopedApiClient({
        baseUrl: window.location.origin,
        getBearerCredential: auth.getBearerCredential ?? (() => ''),
        scope,
      }),
    [auth.getBearerCredential, scope],
  );
  const askClient = useMemo(
    () => createQaApiClient({
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

  const sessionsQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadQASessionPage(client, scope, sessionCursor, signal),
      queryKey: qaSessionQueryKeys.sessions(scope, sessionCursor, identityVersion),
    }),
    enabled: Boolean(auth.getBearerCredential && identityVersion && !scopeChanged),
  });
  const validSessionId = sessionId && UUID_PATTERN.test(sessionId) ? sessionId : null;
  const sessionQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadQASession(client, scope, validSessionId ?? '', signal),
      queryKey: qaSessionQueryKeys.session(scope, validSessionId ?? '', identityVersion),
    }),
    enabled: Boolean(auth.getBearerCredential && identityVersion && validSessionId && !scopeChanged),
    refetchInterval: (query) =>
      query.state.status !== 'error' ? ACTIVE_DISCLOSURE_REFRESH_MS : false,
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
    refetchInterval: (query) =>
      query.state.status !== 'error' && query.state.data?.items.some((message) => message.state !== 'terminal')
        ? 2_000
        : false,
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
  const sessionsRail = (
    <SessionList
      compact
      createError={createMutation.error}
      createPending={createMutation.isPending}
      currentSearch={sessionSearch}
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
      selectedSessionId={validSessionId}
    />
  );
  if (sessionId && !validSessionId) {
    return (
      <WorkspaceLayout
        ariaLabel={ru.chat.workspaceLabel}
        master={sessionsRail}
        detail={<div className={styles.emptyDetail}><StatusPanel state="failed" /></div>}
        mobileDetailActive
      />
    );
  }
  if (validSessionId) {
    return (
      <SessionDetails
        key={`${validSessionId}:${identityVersion}`}
        client={client}
        askClient={askClient}
        identityVersion={identityVersion}
        messageCursor={messageCursor}
        messageQuery={messagesQuery}
        onBack={() => navigate({ pathname: '/chat', search: `?${sessionSearch}` })}
        onNextMessages={() => {
          const cursor = messagesQuery.data?.next_cursor;
          if (!cursor) return;
          const next = new URLSearchParams(searchParams);
          next.set(MESSAGE_CURSOR, cursor);
          setSearchParams(next);
        }}
        onRestore={() => {
          restoreKey.current ??= createIdempotencyKey();
          restoreMutation.mutate();
        }}
        restoreError={restoreMutation.error}
        restorePending={restoreMutation.isPending}
        scope={scope}
        sessionQuery={sessionQuery}
        sessionsRail={sessionsRail}
      />
    );
  }

  return (
    <WorkspaceLayout
      ariaLabel={ru.chat.workspaceLabel}
      master={sessionsRail}
      detail={<NeutralChatDetail />}
      inspector={<NeutralInspector />}
    />
  );
}

function SessionList({
  compact = false,
  createError,
  createPending,
  currentSearch,
  onCreate,
  onNextPage,
  query,
  selectedSessionId = null,
}: {
  compact?: boolean;
  createError: unknown;
  createPending: boolean;
  currentSearch: string;
  onCreate: () => void;
  onNextPage: () => void;
  query: ReturnType<typeof useQuery<Awaited<ReturnType<typeof loadQASessionPage>>>>;
  selectedSessionId?: string | null;
}) {
  return (
    <section className={compact ? styles.sessionRail : styles.page} aria-labelledby="sessions-heading">
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
      {query.error && !query.data
        ? compact ? <StatusPanel state="degraded" /> : <ApiErrorPanel error={query.error} />
        : null}
      {query.isPending ? <StatusPanel state="loading" /> : null}
      {query.isStale && query.data ? <StatusPanel state="stale" /> : null}
      {(query.isStale || query.error || query.fetchStatus === 'paused') && query.data ? <StatusPanel state="partial" /> : null}
      {!query.isStale && !query.error && query.data?.items.length === 0 ? <StatusPanel state="empty" /> : null}

      {!query.isStale && !query.error && query.data?.items.length ? (
        <div className={styles.sessionList} aria-label={ru.chat.sessionsTitle}>
          {query.data.items.map((session) => (
            <Link
              aria-current={session.session_id === selectedSessionId ? 'page' : undefined}
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
  client,
  askClient,
  identityVersion,
  messageCursor,
  messageQuery,
  onBack,
  onNextMessages,
  onRestore,
  restoreError,
  restorePending,
  scope,
  sessionQuery,
  sessionsRail,
}: {
  client: ReturnType<typeof createScopedApiClient>;
  askClient: ReturnType<typeof createQaApiClient>;
  identityVersion: string;
  messageCursor: string | null;
  messageQuery: ReturnType<typeof useQuery<Awaited<ReturnType<typeof loadQAMessagePage>>>>;
  onBack: () => void;
  onNextMessages: () => void;
  onRestore: () => void;
  restoreError: unknown;
  restorePending: boolean;
  scope: Scope;
  sessionQuery: ReturnType<typeof useQuery<Awaited<ReturnType<typeof loadQASession>>>>;
  sessionsRail: ReactNode;
}) {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [admission, setAdmission] = useState<{ messageId: string; runId: string } | null>(null);
  const [eventObservation, setEventObservation] = useState<'idle' | 'observing' | 'reconnecting' | 'failed'>('idle');
  const [clarificationTarget, setClarificationTarget] = useState<string | null>(null);
  const [railCollapsed, setRailCollapsed] = useState(false);
  const [inspectorHost, setInspectorHost] = useState<HTMLElement | null>(null);
  const inspectorTrigger = useRef<HTMLElement | null>(null);
  const inspectorTriggerLabel = useRef<string | null>(null);
  const showSessionsButton = useRef<HTMLButtonElement | null>(null);
  const session = sessionQuery.isStale || sessionQuery.error ? undefined : sessionQuery.data;
  const linkedMessageId = searchParams.get('message_id');
  const linkedRunId = searchParams.get('run_id');
  const linkedCitationId = searchParams.get('citation_id');
  const requestedInspector = searchParams.get('inspector');
  const returnRunId = searchParams.get('return_run_id');
  const invalidDeepLink = Boolean(
    (linkedMessageId && !UUID_PATTERN.test(linkedMessageId)) ||
    (linkedRunId && !UUID_PATTERN.test(linkedRunId)) ||
    (linkedCitationId && (!linkedMessageId || !linkedRunId || !UUID_PATTERN.test(linkedCitationId))) ||
    (requestedInspector && requestedInspector !== 'run' && requestedInspector !== 'citation') ||
    (requestedInspector === 'citation' && !linkedCitationId) ||
    (returnRunId && !UUID_PATTERN.test(returnRunId)),
  );
  const inspectorMode: 'citation' | 'run' | 'none' = invalidDeepLink
    ? 'none'
    : linkedCitationId
      ? 'citation'
      : requestedInspector === 'run'
        ? 'run'
        : 'none';
  const deepLinkMessageId = invalidDeepLink ? null : linkedMessageId;
  const requestedMessageId = deepLinkMessageId ?? admission?.messageId ?? null;
  const admittedMessageQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadQAMessage(
        client,
        scope,
        session?.session_id ?? '',
        requestedMessageId ?? '',
        signal,
      ),
      queryKey: qaSessionQueryKeys.message(
        scope,
        session?.session_id ?? '',
        requestedMessageId ?? '',
        identityVersion,
      ),
    }),
    enabled: Boolean(session?.status === 'active' && requestedMessageId),
    refetchInterval: (query) =>
      query.state.status !== 'error'
        ? query.state.data?.state === 'terminal'
          ? ACTIVE_DISCLOSURE_REFRESH_MS
          : 2_000
        : false,
  });
  const admissionIsTerminal = Boolean(
    admission &&
    admittedMessageQuery.data?.message_id === admission.messageId &&
    admittedMessageQuery.data.state === 'terminal',
  );
  useEffect(() => {
    if (!admission || session?.status !== 'active') return;
    const controller = new AbortController();
    setEventObservation('observing');
    void observeQAMessageEvents({
      client: askClient,
      messageId: admission.messageId,
      onEvent: (event) => {
        setEventObservation('observing');
        void queryClient.invalidateQueries({
          queryKey: qaSessionQueryKeys.messages(scope, session.session_id, messageCursor, identityVersion),
        });
        if (event.type !== 'heartbeat') void admittedMessageQuery.refetch();
      },
      onReconnect: () => setEventObservation('reconnecting'),
      runId: admission.runId,
      scope,
      sessionId: session.session_id,
      signal: controller.signal,
    }).then(() => {
      if (!controller.signal.aborted) {
        setEventObservation('idle');
        void admittedMessageQuery.refetch();
      }
    }).catch(() => {
      if (!controller.signal.aborted) setEventObservation('failed');
    });
    return () => controller.abort();
  }, [admission, askClient, identityVersion, messageCursor, queryClient, scope, session?.session_id, session?.status]);
  const deepLinkMismatch = Boolean(
    deepLinkMessageId && admittedMessageQuery.data && (
      admittedMessageQuery.data.message_id !== deepLinkMessageId ||
      admittedMessageQuery.data.session_id !== session?.session_id ||
      (linkedRunId && admittedMessageQuery.data.run_id !== linkedRunId) ||
      (linkedCitationId &&
        admittedMessageQuery.data.disclosure_eligibility !== 'ineligible' &&
        !admittedMessageQuery.data.accepted_presentation?.citation_ids.includes(linkedCitationId))
    ),
  );
  const deepLinkNeedsActiveSession = Boolean(deepLinkMessageId && session && session.status !== 'active');
  const admittedMessageAvailable = Boolean(
    admission &&
    admittedMessageQuery.data?.message_id === admission.messageId &&
    admittedMessageQuery.data.session_id === session?.session_id,
  );
  const historyUnavailable =
    invalidDeepLink ||
    deepLinkMismatch ||
    deepLinkNeedsActiveSession ||
    Boolean(admittedMessageQuery.error) ||
    Boolean(admittedMessageQuery.isStale && admittedMessageQuery.data) ||
    Boolean(
      !deepLinkMessageId &&
      !admittedMessageAvailable &&
      (messageQuery.error || (messageQuery.isStale && messageQuery.data)),
    );
  const messages = historyUnavailable
    ? undefined
    : deepLinkMessageId
      ? admittedMessageQuery.data ? [admittedMessageQuery.data] : []
      : mergeMessages(messageQuery.data?.items ?? [], admittedMessageQuery.data);
  const clarificationMessage = messages?.find(
    (message) => message.message_id === clarificationTarget && message.terminal_outcome === 'clarification_required',
  );
  const inspectorMessage = linkedMessageId
    ? messages?.find((message) => message.message_id === linkedMessageId) ?? null
    : null;

  function openInspector(message: QAMessage, mode: 'citation' | 'run', citationId?: string | null) {
    inspectorTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    inspectorTriggerLabel.current = inspectorTrigger.current?.getAttribute('aria-label')
      ?? inspectorTrigger.current?.textContent?.trim()
      ?? null;
    const next = new URLSearchParams(searchParams);
    next.set('message_id', message.message_id);
    if (message.run_id) next.set('run_id', message.run_id);
    else next.delete('run_id');
    next.set('inspector', mode);
    if (mode === 'citation' && citationId) next.set('citation_id', citationId);
    else next.delete('citation_id');
    setSearchParams(next);
  }

  function closeInspector() {
    const previousTrigger = inspectorTrigger.current;
    const previousTriggerLabel = inspectorTriggerLabel.current;
    const next = new URLSearchParams(searchParams);
    next.delete('inspector');
    next.delete('citation_id');
    setSearchParams(next);
    requestAnimationFrame(() => {
      if (previousTrigger?.isConnected) {
        previousTrigger.focus();
        return;
      }
      const replacement = [...document.querySelectorAll<HTMLButtonElement>('button')].find((button) =>
        button.getAttribute('aria-label') === previousTriggerLabel || button.textContent?.trim() === previousTriggerLabel,
      );
      replacement?.focus();
    });
  }

  return (
    <WorkspaceLayout
      ariaLabel={ru.chat.workspaceLabel}
      master={(
        <div className={styles.railShell}>
          <button
            className={styles.collapseButton}
            onClick={() => {
              setRailCollapsed(true);
              requestAnimationFrame(() => showSessionsButton.current?.focus());
            }}
            type="button"
          >
            {ru.chat.collapseSessions}
          </button>
          {sessionsRail}
        </div>
      )}
      detail={(
    <section className={styles.threadPane} aria-labelledby="session-heading">
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
        <div className={styles.headerActions}>
          <button
            className={styles.secondaryButton}
            onClick={() => setRailCollapsed((collapsed) => !collapsed)}
            ref={showSessionsButton}
            type="button"
          >
            {railCollapsed ? ru.chat.showSessions : ru.chat.collapseSessions}
          </button>
          <button className={`${styles.secondaryButton} ${styles.mobileOnly}`} onClick={onBack} type="button">
            {ru.chat.backToSessions}
          </button>
        </div>
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
          {invalidDeepLink || deepLinkMismatch || deepLinkNeedsActiveSession ? <StatusPanel state="failed" /> : null}
          {messageQuery.error ? <ApiErrorPanel error={messageQuery.error} /> : null}
          {admittedMessageQuery.error ? <ApiErrorPanel error={admittedMessageQuery.error} /> : null}
          {deepLinkMessageId && !admittedMessageQuery.data && !admittedMessageQuery.error ? <StatusPanel state="loading" /> : null}
          {messageQuery.isPending ? <StatusPanel state="loading" /> : null}
          {(messageQuery.isStale && messageQuery.data) || (admittedMessageQuery.isStale && admittedMessageQuery.data) ? <StatusPanel state="stale" /> : null}
          {!historyUnavailable && messages?.length === 0 ? <StatusPanel state="empty" /> : null}
          {messages?.length ? (
            <div className={styles.messageList} aria-label={ru.chat.messagesTitle}>
              {messages.map((message) => (
                <MessageCard
                  client={client}
                  hideCancellationControl={
                    admission?.messageId === message.message_id && !admissionIsTerminal
                  }
                  key={message.message_id}
                  message={message}
                  onCanonicalRefresh={() => {
                    void queryClient.invalidateQueries({
                      queryKey: qaSessionQueryKeys.messages(scope, session.session_id, messageCursor, identityVersion),
                    });
                    if (requestedMessageId === message.message_id) void admittedMessageQuery.refetch();
                  }}
                  onClarify={() => setClarificationTarget(message.message_id)}
                  onCitationSelectionChange={(citationId) => {
                    if (citationId) openInspector(message, 'citation', citationId);
                    else closeInspector();
                  }}
                  onOpenRunSummary={() => openInspector(message, 'run')}
                  inspectorHost={
                    message.message_id === linkedMessageId && inspectorMode === 'citation'
                      ? inspectorHost
                      : null
                  }
                  targetCitationId={
                    message.message_id === deepLinkMessageId && !deepLinkMismatch ? linkedCitationId : null
                  }
                  returnRunId={
                    message.message_id === deepLinkMessageId && message.run_id === returnRunId ? returnRunId : null
                  }
                  scope={scope}
                />
              ))}
            </div>
          ) : null}
          {!historyUnavailable && messageQuery.data?.next_cursor ? (
            <button className={styles.secondaryButton} onClick={onNextMessages} type="button">
              {ru.chat.nextMessages}
            </button>
          ) : null}
          {messageQuery.error || admittedMessageQuery.error ? (
            <button
              className={styles.secondaryButton}
              onClick={() => {
                void messageQuery.refetch();
                if (requestedMessageId) void admittedMessageQuery.refetch();
              }}
              type="button"
            >
              {ru.chat.retryMessages}
            </button>
          ) : null}
          {!messageQuery.error && !admittedMessageQuery.error && (
            (messageQuery.isStale && messageQuery.data) ||
            (admittedMessageQuery.isStale && admittedMessageQuery.data)
          ) ? (
            <button
              className={styles.secondaryButton}
              onClick={() => {
                if (messageQuery.isStale) void messageQuery.refetch();
                if (requestedMessageId && admittedMessageQuery.isStale) void admittedMessageQuery.refetch();
              }}
              type="button"
            >
              {ru.chat.refreshMessages}
            </button>
          ) : null}
          {messageCursor ? <span className={styles.pageHint}>{ru.chat.messagePageLoaded}</span> : null}
          {eventObservation !== 'idle' ? (
            <p data-state={eventObservation} role={eventObservation === 'failed' ? 'alert' : 'status'}>
              {eventObservation === 'observing'
                ? ru.chat.eventStreamObserving
                : eventObservation === 'reconnecting'
                  ? ru.chat.eventStreamReconnecting
                  : ru.chat.eventStreamFailed}
            </p>
          ) : null}
          {admission && !admissionIsTerminal ? (
            <AskCancellationControl
              client={client}
              onCanonicalRefresh={() => {
                void admittedMessageQuery.refetch();
                void queryClient.invalidateQueries({
                  queryKey: qaSessionQueryKeys.messages(
                    scope,
                    session.session_id,
                    messageCursor,
                    identityVersion,
                  ),
                });
              }}
              runId={admission.runId}
              scope={scope}
            />
          ) : null}
        </section>
      ) : null}

      {session?.status === 'active' && !messageQuery.error && !messageQuery.isStale && messageQuery.data ? (
        <AskComposer
          client={askClient}
          clarificationTarget={clarificationMessage ? clarificationTarget : null}
          onAccepted={(admission) => {
            setAdmission({ messageId: admission.message_id, runId: admission.run_id });
            setClarificationTarget(null);
            const nextSearch = new URLSearchParams(searchParams);
            nextSearch.delete('message_id');
            nextSearch.delete('run_id');
            nextSearch.delete('citation_id');
            nextSearch.delete('inspector');
            nextSearch.delete('return_run_id');
            nextSearch.delete(MESSAGE_CURSOR);
            setSearchParams(nextSearch, { replace: true });
            void queryClient.invalidateQueries({
              queryKey: qaSessionQueryKeys.messages(scope, session.session_id, null, identityVersion),
            });
          }}
          onCancelClarification={() => setClarificationTarget(null)}
          scope={scope}
          sessionId={session.session_id}
        />
      ) : null}

      {session ? (
        <footer className={styles.identity}>
          <span>{ru.chat.canonicalSessionId}</span>
          <code>{session.session_id}</code>
          <span>{ru.chat.sessionScope(session.workspace_id, session.environment_id)}</span>
        </footer>
      ) : null}
    </section>
      )}
      inspector={(
        <ContextualInspector
          hostRef={setInspectorHost}
          message={inspectorMessage}
          mode={inspectorMode}
          onClose={closeInspector}
        />
      )}
      inspectorOpen={inspectorMode !== 'none'}
      masterCollapsed={railCollapsed}
      mobileDetailActive
    />
  );
}

function MessageCard({
  client,
  hideCancellationControl,
  inspectorHost,
  message,
  onCanonicalRefresh,
  onCitationSelectionChange,
  onClarify,
  onOpenRunSummary,
  targetCitationId,
  returnRunId,
  scope,
}: {
  client: ReturnType<typeof createScopedApiClient>;
  hideCancellationControl: boolean;
  inspectorHost: HTMLElement | null;
  message: QAMessage;
  onCanonicalRefresh: () => void;
  onCitationSelectionChange: (citationId: string | null) => void;
  onClarify: () => void;
  onOpenRunSummary: () => void;
  targetCitationId: string | null;
  returnRunId: string | null;
  scope: Scope;
}) {
  return (
    <article className={styles.messageCard}>
      <div className={styles.messageMeta}>
        <time dateTime={message.created_at}>{formatDate(new Date(message.created_at))}</time>
        <span>{messageStateLabel(message)}</span>
      </div>
      <CompletenessPresentation
        value={
          message.disclosure_eligibility === 'eligible'
          && message.terminal_outcome !== 'denied'
            ? message.completeness ?? null
            : null
        }
      />
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
          {(message.state === 'pending' || message.state === 'running') &&
          !hideCancellationControl &&
          message.run_id &&
          UUID_PATTERN.test(message.run_id) &&
          message.workspace_id === scope.workspaceId &&
          message.environment_id === scope.environmentId ? (
            <AskCancellationControl
              client={client}
              onCanonicalRefresh={onCanonicalRefresh}
              runId={message.run_id}
              scope={scope}
            />
          ) : null}
          {message.state === 'terminal' && message.terminal_outcome === 'answered' ? (
            <AcceptedAnswer
              client={client}
              inspectorHost={inspectorHost}
              message={message}
              onCitationSelectionChange={onCitationSelectionChange}
              targetCitationId={targetCitationId}
              scope={scope}
            />
          ) : null}
          {message.state === 'terminal' && message.terminal_outcome === 'clarification_required' ? (
            <button className={styles.textButton} onClick={onClarify} type="button">
              {ru.chat.answerClarification}
            </button>
          ) : null}
          {message.run_id &&
          UUID_PATTERN.test(message.run_id) &&
          UUID_PATTERN.test(message.message_id) ? (
            <div className={styles.messageActions}>
              <button className={styles.textButton} onClick={onOpenRunSummary} type="button">
                {ru.chat.inspectRunSummary}
              </button>
              <Link
                className={styles.textButton}
                to={{
                  pathname: `/qa/runs/${message.run_id}`,
                  search: `?${new URLSearchParams({
                    workspace: scope.workspaceId,
                    environment: scope.environmentId,
                    return_session_id: message.session_id,
                    return_message_id: message.message_id,
                  }).toString()}`,
                }}
              >
                {returnRunId === message.run_id ? ru.chat.backToRun : ru.chat.openRun}
              </Link>
            </div>
          ) : null}
        </>
      )}
    </article>
  );
}

function NeutralChatDetail() {
  return (
    <div className={styles.emptyDetail}>
      <p className={styles.eyebrow}>{ru.chat.threadEyebrow}</p>
      <h2>{ru.chat.noSessionTitle}</h2>
      <p>{ru.chat.noSessionDescription}</p>
    </div>
  );
}

function NeutralInspector() {
  return (
    <div className={styles.inspectorPanel} role="note">
      <p className={styles.eyebrow}>{ru.chat.inspectorTitle}</p>
      <h3>{ru.chat.inspectorEmptyTitle}</h3>
      <p>{ru.chat.inspectorEmptyDescription}</p>
    </div>
  );
}

function ContextualInspector({
  hostRef,
  message,
  mode,
  onClose,
}: {
  hostRef: (node: HTMLElement | null) => void;
  message: QAMessage | null;
  mode: 'citation' | 'run' | 'none';
  onClose: () => void;
}) {
  const closeButton = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (mode === 'none') return;
    requestAnimationFrame(() => closeButton.current?.focus());
  }, [mode]);
  if (mode === 'none') return <NeutralInspector />;
  return (
    <div
      aria-label={mode === 'citation' ? ru.chat.citationInspectorTitle : ru.chat.runInspectorTitle}
      aria-modal="false"
      className={styles.inspectorPanel}
      onKeyDown={(event) => {
        if (event.key === 'Escape') onClose();
      }}
      role="dialog"
      tabIndex={-1}
    >
      <header className={styles.inspectorHeader}>
        <div>
          <p className={styles.eyebrow}>{ru.chat.inspectorTitle}</p>
          <h3>{mode === 'citation' ? ru.chat.citationInspectorTitle : ru.chat.runInspectorTitle}</h3>
        </div>
        <button className={styles.textButton} onClick={onClose} ref={closeButton} type="button">
          {ru.chat.closeInspector}
        </button>
      </header>
      {mode === 'citation' ? (
        <div className={styles.inspectorContent} ref={hostRef} />
      ) : message ? (
        <div className={styles.runSummary}>
          <span className={styles.statusChip}>{messageStateLabel(message)}</span>
          <dl>
            <div><dt>{ru.qaRuns.detail.messageId}</dt><dd><code>{message.message_id}</code></dd></div>
            <div><dt>{ru.qaRuns.run}</dt><dd><code>{message.run_id ?? ru.qaRuns.unknown}</code></dd></div>
            <div><dt>{ru.qaRuns.detail.attempt}</dt><dd><code>{message.accepted_presentation?.attempt_id ?? ru.qaRuns.unknown}</code></dd></div>
            <div><dt>{ru.chat.messageVersion}</dt><dd>{formatNumber(message.version)}</dd></div>
          </dl>
          <CompletenessPresentation
            value={message.disclosure_eligibility === 'eligible' ? message.completeness ?? null : null}
          />
        </div>
      ) : (
        <StatusPanel state="failed" />
      )}
    </div>
  );
}

function mergeMessages(messages: QAMessage[], admittedMessage?: QAMessage): QAMessage[] {
  const byId = new Map(messages.map((message) => [message.message_id, message]));
  if (admittedMessage) byId.set(admittedMessage.message_id, admittedMessage);
  return [...byId.values()].sort((left, right) => left.ordinal - right.ordinal);
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
