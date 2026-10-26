import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import { createQaApiClient, createScopedApiClient, SpineApiError } from "../../../api/client";
import { useAuth } from "../../../app/auth";
import { createAskCommandActions } from "../../../features/chat/ask-command";
import { createAskCancellationActions } from "../../../features/chat/cancellation-api";
import { inspectAcceptedCitation } from "../../../features/chat/citation-inspection";
import { submitQaFeedback } from "../../../features/chat/feedback-api";
import { loadQARunDetail } from "../../../features/runs/qa-run-api";
import {
  createQASession, loadQAMessage, loadQAMessagePage, loadQASession, loadQASessionPage, restoreQASession,
} from "../../../features/chat/session-api";
import { ru } from "../ru";

const C = ru.chat;

function answerParts(message) {
  const text = message.answer ?? "";
  const presentation = message.accepted_presentation;
  if (!presentation?.claims.length) return text ? [{ type: "p", parts: [text] }] : [];
  const claims = [...presentation.claims].sort((a, b) => a.start_offset - b.start_offset);
  const parts = [];
  let offset = 0;
  for (const claim of claims) {
    if (claim.start_offset > offset) parts.push(text.slice(offset, claim.start_offset));
    parts.push({ text: text.slice(claim.start_offset, claim.end_offset), claim: claim.claim_id });
    offset = claim.end_offset;
  }
  if (offset < text.length) parts.push(text.slice(offset));
  return [{ type: "p", parts }];
}

function mappedClaims(message) {
  const presentation = message.accepted_presentation;
  if (!presentation) return {};
  const citationIndex = new Map(presentation.citation_ids.map((id, index) => [id, index]));
  return Object.fromEntries(presentation.claims.map(claim => [claim.claim_id, {
    kind: claim.kind,
    rel: presentation.relationships.filter(r => r.claim_id === claim.claim_id).map(r => ({
      kind: r.kind,
      related: r.related_claim_id ?? undefined,
      cites: r.citation_ids.map(id => citationIndex.get(id)).filter(index => index !== undefined),
    })),
  }]));
}

export function mapApiMessage(message, evidenceByCitation = {}, detail = null) {
  const presentation = message.accepted_presentation;
  const unavailableEvidence = (presentation?.citation_ids ?? []).some(citationId => evidenceByCitation[citationId]?.unavailable);
  const citations = (presentation?.citation_ids ?? []).map(citationId => {
    const evidence = evidenceByCitation[citationId];
    return {
      citation_id: citationId,
      source_object_id: evidence?.source_object_id ?? citationId,
      source_revision_id: evidence?.source_revision_id ?? citationId,
      source_kind: "document",
      revision_number: evidence?.revision_number ?? 0,
      locator: { kind: "custom", value: evidence?.locator_reference ?? "Защищённая ссылка" },
      excerpt: evidence?.excerpt ?? "Откройте источник, чтобы проверить актуальные evidence и полномочия.",
    };
  });
  const terminal = message.state === "terminal";
  const duration = detail?.completed_at
    ? Math.max(0, Date.parse(detail.completed_at) - Date.parse(detail.created_at))
    : Math.max(0, Date.parse(message.updated_at) - Date.parse(message.created_at));
  const finalAttempt = detail?.attempts.find(attempt => attempt.attempt_id === detail.final_attempt_id)
    ?? detail?.attempts.at(-1)
    ?? null;
  const outcomeReason = {
    abstained: "no_usable_evidence",
    denied: "authorization_denied",
    failed: "internal_failure",
    cancelled: "request_cancelled",
  }[message.terminal_outcome];
  return {
    ...message,
    disclosure_eligibility: unavailableEvidence ? "ineligible" : message.disclosure_eligibility,
    clarifies: null,
    answer_id: presentation?.answer_id ?? null,
    reason: outcomeReason ?? null,
    clarification_prompt: message.terminal_outcome === "clarification_required" ? message.answer : null,
    citations,
    claims: mappedClaims(message),
    blocks: answerParts(message),
    completeness: message.completeness ?? null,
    revealed: terminal ? Infinity : (message.answer?.length ?? 0),
    stage: terminal ? null : message.state === "pending" ? "pending" : "answering",
    checking: false,
    run: {
      attempt_id: finalAttempt?.attempt_id ?? presentation?.attempt_id ?? message.run_id,
      attempt_number: finalAttempt?.attempt_number ?? 1,
      versions: {
        agent: finalAttempt?.execution_pin?.agent_version?.version ?? "—",
        prompt: finalAttempt?.execution_pin?.prompt_version?.version ?? "—",
        model: finalAttempt?.execution_pin?.model_profile?.version ?? "—",
      },
      duration_ms: duration,
      snapshot: finalAttempt?.context?.projection_snapshot_id ?? "—",
      retrieval: finalAttempt?.context?.retrieval_strategy ?? "—",
      freshness: finalAttempt?.context?.freshness ?? null,
      context: finalAttempt?.context?.evidence_count ?? citations.length,
      usage: finalAttempt?.usage ?? { model_invocations: 0, input_tokens: 0, output_tokens: 0, cached_input_tokens: 0, cost: "0", currency: "—" },
      timeline: detail?.diagnostic_events.map(event => ({
        kind: event.kind,
        ms: Math.max(0, Date.parse(event.occurred_at) - Date.parse(detail.created_at)),
      })) ?? [
        { kind: "run_started", ms: 0 },
        ...(terminal ? [{ kind: message.terminal_outcome ?? "completed", ms: duration }] : []),
      ],
    },
  };
}

export function useSpineChat(toast, { workspace, environment }) {
  const location = useLocation();
  const navigate = useNavigate();
  const auth = useAuth();
  const queryClient = useQueryClient();
  const scope = useMemo(() => ({ workspaceId: workspace, environmentId: environment }), [workspace, environment]);
  const enabled = Boolean(auth.getBearerCredential && auth.identityVersion);
  const client = useMemo(() => createScopedApiClient({
    baseUrl: window.location.origin,
    getBearerCredential: auth.getBearerCredential ?? (() => ""),
    scope,
  }), [auth.getBearerCredential, scope]);
  const askClient = useMemo(() => createQaApiClient({
    baseUrl: window.location.origin,
    getBearerCredential: auth.getBearerCredential ?? (() => ""),
    scope,
  }), [auth.getBearerCredential, scope]);
  const askActions = useMemo(() => createAskCommandActions(askClient, scope), [askClient, scope]);
  const routeSessionId = location.pathname.match(/^\/chat\/([^/]+)$/)?.[1] ?? null;
  const [selectedId, setSelectedId] = useState(routeSessionId);
  const [inspector, setInspector] = useState(null);
  const [clarifyTarget, setClarifyTarget] = useState(null);
  const [draft, setDraft] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [creating, setCreating] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [feedback, setFeedback] = useState({});
  const [focusId, setFocusId] = useState(null);
  const [citationEvidence, setCitationEvidence] = useState({});
  const [admittedMessages, setAdmittedMessages] = useState([]);
  const [pendingCitation, setPendingCitation] = useState(null);
  const previousScope = useRef(`${workspace}/${environment}`);
  const inspectorClosing = useRef(false);

  const sessionsQuery = useQuery({
    queryKey: ["spine", "ported-chat-sessions", workspace, environment, auth.identityVersion],
    queryFn: ({ signal }) => loadQASessionPage(client, scope, null, signal),
    enabled,
    retry: false,
  });
  const directSessionQuery = useQuery({
    queryKey: ["spine", "ported-chat-session", workspace, environment, routeSessionId, auth.identityVersion],
    queryFn: ({ signal }) => loadQASession(client, scope, routeSessionId, signal),
    enabled: enabled && Boolean(routeSessionId),
    retry: false,
  });
  const sessions = useMemo(() => {
    const byId = new Map((sessionsQuery.data?.items ?? []).map(session => [session.session_id, session]));
    if (directSessionQuery.data) byId.set(directSessionQuery.data.session_id, directSessionQuery.data);
    return [...byId.values()];
  }, [directSessionQuery.data, sessionsQuery.data?.items]);
  const session = sessions.find(s => s.session_id === selectedId) ?? null;
  const messagesQuery = useQuery({
    queryKey: ["spine", "ported-chat-messages", workspace, environment, selectedId, auth.identityVersion],
    queryFn: ({ signal }) => loadQAMessagePage(client, scope, selectedId, null, signal),
    enabled: enabled && Boolean(selectedId && session?.status === "active"),
    retry: false,
    refetchInterval: query => query.state.data?.items.some(m => m.state !== "terminal") ? 1_000 : false,
  });
  const rawMessages = useMemo(() => {
    const loaded = messagesQuery.data?.items ?? [];
    const byId = new Map(admittedMessages.map(message => [message.message_id, message]));
    loaded.forEach(message => byId.set(message.message_id, message));
    return [...byId.values()].sort((a, b) => a.ordinal - b.ordinal);
  }, [admittedMessages, messagesQuery.data?.items]);
  const runQueries = useQueries({
    queries: rawMessages.filter(message => message.run_id).map(message => ({
      queryKey: ["spine", "ported-chat-run-detail", workspace, environment, message.run_id],
      queryFn: ({ signal }) => loadQARunDetail(client, message.run_id, signal, scope),
      enabled,
      retry: false,
      staleTime: 30_000,
    })),
  });
  const runDetails = useMemo(() => Object.fromEntries(runQueries
    .filter(query => query.data)
    .map(query => [query.data.run_id, query.data])), [runQueries]);
  const thread = useMemo(
    () => rawMessages.map(message => mapApiMessage(message, citationEvidence, runDetails[message.run_id])),
    [citationEvidence, rawMessages, runDetails],
  );
  const running = thread.find(m => m.state !== "terminal") ?? null;
  const messagesBySession = useMemo(
    () => Object.fromEntries(sessions.map(s => [s.session_id, s.session_id === selectedId ? thread : []])),
    [selectedId, sessions, thread],
  );
  const busySessions = new Set(running && selectedId ? [selectedId] : []);

  useEffect(() => {
    if (!selectedId && sessions[0]) setSelectedId(sessions[0].session_id);
  }, [selectedId, sessions]);
  useEffect(() => {
    const nextScope = `${workspace}/${environment}`;
    if (previousScope.current === nextScope) return;
    previousScope.current = nextScope;
    setSelectedId(null);
    setAdmittedMessages([]);
    setInspector(null);
    setClarifyTarget(null);
  }, [workspace, environment]);
  useEffect(() => {
    if (!pendingCitation || !thread.some(m => m.message_id === pendingCitation.messageId)) return;
    void openCitation(pendingCitation.messageId, pendingCitation.citationId, pendingCitation.returnRunId);
    setPendingCitation(null);
  }, [pendingCitation, thread]);
  useEffect(() => {
    const search = new URLSearchParams(location.search);
    if (inspectorClosing.current) {
      if (search.has("inspector") || search.has("citation_id") || search.has("message_id")) return;
      inspectorClosing.current = false;
    }
    if (inspector || !thread.length) return;
    const messageId = search.get("message_id");
    const citationId = search.get("citation_id");
    const mode = search.get("inspector");
    if (!messageId || !thread.some(message => message.message_id === messageId)) return;
    if (citationId || mode === "citation") {
      if (citationId) void openCitation(messageId, citationId);
      return;
    }
    if (mode === "run" || search.get("run_id")) setInspector({ mode: "run", messageId });
  }, [inspector, location.search, thread]);

  function writeInspector(nextInspector) {
    const search = new URLSearchParams(location.search);
    if (!nextInspector) {
      search.delete("message_id");
      search.delete("run_id");
      search.delete("citation_id");
      search.delete("inspector");
    } else {
      const message = thread.find(item => item.message_id === nextInspector.messageId);
      search.set("message_id", nextInspector.messageId);
      if (message?.run_id) search.set("run_id", message.run_id);
      search.set("inspector", nextInspector.mode);
      if (nextInspector.mode === "citation") search.set("citation_id", nextInspector.citationId);
      else search.delete("citation_id");
      if (nextInspector.returnRunId) search.set("return_run_id", nextInspector.returnRunId);
    }
    navigate({ pathname: selectedId ? `/chat/${selectedId}` : "/chat", search: search.toString() }, { replace: true });
  }

  function selectSession(id) {
    setSelectedId(id);
    setInspector(null);
    setClarifyTarget(null);
    navigate({ pathname: `/chat/${id}`, search: location.search }, { replace: true });
  }

  async function createSession() {
    if (creating || !enabled) return;
    setCreating(true);
    try {
      const created = await createQASession(client, scope, `qa-session-${crypto.randomUUID()}`);
      await queryClient.invalidateQueries({ queryKey: ["spine", "ported-chat-sessions", workspace, environment] });
      selectSession(created.session_id);
    } catch (error) {
      toast(error instanceof SpineApiError ? error.safeMessage : "Не удалось создать сессию.", "warning");
    } finally {
      setCreating(false);
    }
  }

  async function restoreSession() {
    if (!session || restoring) return;
    setRestoring(true);
    try {
      await restoreQASession(client, scope, session.session_id, session.version, `qa-restore-${crypto.randomUUID()}`);
      await queryClient.invalidateQueries({ queryKey: ["spine", "ported-chat-sessions", workspace, environment] });
    } catch (error) {
      toast(error instanceof SpineApiError ? error.safeMessage : "Не удалось восстановить сессию.", "warning");
    } finally {
      setRestoring(false);
    }
  }

  async function ask(text = draft) {
    const question = text.trim();
    if (!question || !session || running || submitting) return;
    setSubmitting(true);
    try {
      const result = await askActions.submit(session.session_id, question, clarifyTarget ?? undefined);
      if (result.kind !== "accepted") throw new Error(result.message ?? "Не удалось отправить вопрос.");
      setDraft("");
      setClarifyTarget(null);
      try {
        const admitted = await loadQAMessage(
          client,
          scope,
          session.session_id,
          result.admission.message_id,
          new AbortController().signal,
        );
        setAdmittedMessages(current => [
          ...current.filter(message => message.message_id !== admitted.message_id),
          admitted,
        ]);
      } catch {
        await messagesQuery.refetch();
      }
    } catch (error) {
      toast(error instanceof SpineApiError ? error.safeMessage : error instanceof Error ? error.message : "Не удалось отправить вопрос.", "warning");
    } finally {
      setSubmitting(false);
    }
  }

  async function stop() {
    if (!running?.run_id) return;
    const result = await createAskCancellationActions(client, scope).cancel(running.run_id);
    if (result.kind === "failed") toast(result.message, "warning");
    await messagesQuery.refetch();
  }

  async function openCitation(messageId, citationId, returnRunId = null) {
    const message = thread.find(m => m.message_id === messageId);
    setInspector({ mode: "citation", messageId, citationId, checking: true });
    writeInspector({ mode: "citation", messageId, citationId, returnRunId });
    const presentation = message?.accepted_presentation;
    if (!presentation) return;
    try {
      const result = await inspectAcceptedCitation(client, scope, {
        answerId: presentation.answer_id,
        attemptId: presentation.attempt_id,
        runId: presentation.run_id,
      }, citationId, new AbortController().signal);
      if (result.status === "available") {
        setCitationEvidence(current => ({ ...current, [citationId]: result.evidence }));
      } else {
        setCitationEvidence(current => ({ ...current, [citationId]: { unavailable: true } }));
      }
    } catch {
      toast("Источник сейчас недоступен.", "warning");
    } finally {
      setInspector(current => current?.citationId === citationId ? { ...current, checking: false } : current);
    }
  }

  async function submitFeedback(key, outcome, comment) {
    if (feedback[key]) return;
    const message = thread.find(m => `answer:${m.answer_id}` === key);
    if (!message?.answer_id || !message.run_id) return;
    setFeedback(current => ({ ...current, [key]: { outcome, status: "submitting" } }));
    try {
      await submitQaFeedback(client, {
        request: {
          target_kind: "answer",
          target_id: message.answer_id,
          outcome: `answer.${outcome}`,
          run_id: message.run_id,
          message_id: message.message_id,
          ...(comment?.trim() ? { comment: comment.trim() } : {}),
        },
        idempotencyKey: crypto.randomUUID(),
        signal: new AbortController().signal,
      });
      setFeedback(current => ({ ...current, [key]: { outcome, status: "recorded" } }));
      toast(C.feedbackRecorded, "success");
    } catch (error) {
      setFeedback(current => { const next = { ...current }; delete next[key]; return next; });
      toast(error instanceof SpineApiError ? error.safeMessage : "Не удалось сохранить отзыв.", "warning");
    }
  }

  async function copy(text, what) {
    try { await navigator.clipboard.writeText(text); toast(`${what} скопирован`); }
    catch { toast("Буфер обмена недоступен", "warning"); }
  }

  return {
    sessions, session, selectSession, createSession, creating, restoreSession, restoring, busySessions,
    thread, running, observation: running ? "observing" : "idle", messagesBySession,
    showMessage: (sessionId, messageId) => { selectSession(sessionId); setInspector({ mode: "run", messageId }); setFocusId(messageId); },
    showCitation: (sessionId, messageId, citationId, returnRunId = null) => {
      selectSession(sessionId);
      setFocusId(messageId);
      setPendingCitation({ messageId, citationId, returnRunId });
    },
    focusId, clearFocus: () => setFocusId(null),
    draft, setDraft, ask, submitting, stop,
    clarifyTarget, setClarifyTarget,
    inspector,
    openCitation,
    openRun: messageId => {
      setInspector({ mode: "run", messageId });
      writeInspector({ mode: "run", messageId });
    },
    closeInspector: () => {
      inspectorClosing.current = true;
      setInspector(null);
      writeInspector(null);
    },
    feedback, submitFeedback,
    copy, refresh: () => { void sessionsQuery.refetch(); void messagesQuery.refetch(); },
    interfaceState: !enabled
      ? "degraded"
      : (routeSessionId ? directSessionQuery.isPending : sessionsQuery.isPending)
        ? "loading"
        : (routeSessionId ? directSessionQuery.error : sessionsQuery.error)
          ? "failed"
          : "ready",
  };
}
