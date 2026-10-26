import { uuid } from "../spineData";
import { INITIAL_SESSIONS } from "../chat/spineChatData";
import { ru } from "../ru";

const Q = ru.qaRuns;

// The current user asks in every session except the colleague's, so the initiator filter has two subjects to tell apart.
const COLLEAGUE_SESSION = INITIAL_SESSIONS[1].session_id;
export const subjectOf = (session) => uuid(session.session_id === COLLEAGUE_SESSION ? 8102 : 8101);

// Stable ids derived from a message's run id, so a run reads the same on every visit.
const seedOf = (id, salt) => parseInt(id.replace(/-/g, "").slice(0, 7), 16) + salt * 7919;
const derive = (id, salt) => uuid(seedOf(id, salt));
const digest = (id, salt) => `sha256:${derive(id, salt).replace(/-/g, "")}`;
const at = (iso, ms) => new Date(new Date(iso).valueOf() + ms).toISOString();

const SAFE_REASON = {
  no_usable_evidence: "insufficient_evidence",
  insufficient_evidence: "insufficient_evidence",
  unmet_freshness: "context_unavailable",
  authorization_denied: "access_denied",
  internal_failure: "internal_failure",
  request_cancelled: "cancelled_by_request",
};

const DISPATCH_MS = 120;
const FIRST_ATTEMPT_MS = 1100;
const BACKOFF_MS = 400;

function attemptOf(m, { id, number, startMs, endMs, final, retryReason }) {
  const outcome = final ? m.terminal_outcome : "failed";
  const state = !final ? "failed" : outcome === "failed" ? "failed" : outcome === "cancelled" ? "cancelled" : "succeeded";
  const reason = !final ? retryReason : SAFE_REASON[m.reason] ?? (outcome === "cancelled" ? "cancelled_by_request" : null);
  const bound = outcome !== "denied";
  const started = at(m.created_at, startMs);
  const completed = at(m.created_at, endMs);
  const latency = endMs - startMs;
  const terminalKind = state === "succeeded" ? "completed" : state;
  const timeline = [
    { event_id: derive(id, 1), kind: "started", occurred_at: started, reason: null },
    bound && { event_id: derive(id, 2), kind: "context_bound", occurred_at: at(started, Math.round(latency * 0.18)), reason: null },
    { event_id: derive(id, 3), kind: terminalKind, occurred_at: completed, reason: state === "succeeded" ? null : reason },
  ].filter(Boolean);
  const v = m.run.versions;
  const [agentName, agentVersion] = v.agent.split("@");
  const projectionSnapshot = m.run.snapshot;
  const usage = final ? m.run.usage : {
    model_invocations: 1, input_tokens: m.run.usage.input_tokens, output_tokens: 0,
    cached_input_tokens: 0, cost: (m.run.usage.input_tokens * 0.0000012).toFixed(4), currency: "USD",
  };
  return {
    attempt_id: id,
    attempt_number: number,
    state,
    outcome,
    reason,
    started_at: started,
    completed_at: completed,
    latency_ms: latency,
    execution_pin: {
      capability_id: agentName,
      agent_version: { version: agentVersion, digest: digest(m.run_id, 11) },
      prompt_version: { version: v.prompt, digest: digest(m.run_id, 12) },
      model_profile: { version: v.model, digest: digest(m.run_id, 13) },
      context_profile_binding: "qa.grounded",
      context_profile_version: 3,
      projection: { projection_snapshot_id: projectionSnapshot },
      pin_digest: digest(m.run_id, 14),
    },
    context: bound ? {
      retrieval_strategy: m.run.retrieval,
      canonical_as_of_time: at(m.created_at, -42 * 60_000),
      canonical_as_of_generation: 41,
      freshness: m.completeness?.partiality_status === "partial" ? "stale" : "current",
      health: m.completeness?.partiality_status === "partial" ? "degraded" : "healthy",
      evidence_count: m.run.context,
      projection_snapshot_id: projectionSnapshot,
    } : null,
    usage,
    timeline,
    completeness: final && m.completeness ? {
      ...m.completeness,
      target_scope_id: derive(m.run_id, 21), target_scope_version: 7,
      observed_coverage_id: derive(m.run_id, 22), observed_coverage_version: 3,
    } : null,
  };
}

// QARunDetailResponse for a terminal chat message; content never travels with it.
export function runDetail(m, session) {
  const retried = m.run.attempt_number > 1;
  const total = Math.max(m.run.duration_ms, DISPATCH_MS + (retried ? FIRST_ATTEMPT_MS + BACKOFF_MS : 0) + 200);
  const attempts = retried
    ? [
      attemptOf(m, { id: derive(m.run_id, 5), number: 1, startMs: DISPATCH_MS, endMs: DISPATCH_MS + FIRST_ATTEMPT_MS, final: false, retryReason: "provider_unavailable" }),
      attemptOf(m, { id: m.run.attempt_id, number: 2, startMs: DISPATCH_MS + FIRST_ATTEMPT_MS + BACKOFF_MS, endMs: total, final: true }),
    ]
    : [attemptOf(m, { id: m.run.attempt_id, number: 1, startMs: DISPATCH_MS, endMs: total, final: true })];
  const events = attempts.flatMap(a => a.timeline);
  return {
    run_id: m.run_id,
    workspace_id: session.workspace_id,
    environment_id: session.environment_id,
    subject_kind: "human",
    subject_id: subjectOf(session),
    state: "completed",
    outcome: m.terminal_outcome,
    final_attempt_id: m.run.attempt_id,
    command_digest: digest(m.run_id, 32),
    created_at: m.created_at,
    completed_at: at(m.created_at, total),
    duration_ms: total,
    trace_id: m.trace_id,
    correlation_id: derive(m.run_id, 33),
    causation_id: derive(m.message_id, 34),
    session_id: m.session_id,
    message_id: m.message_id,
    answer_id: m.answer_id,
    warnings: [...new Set(events.filter(e => e.reason && ["failed", "cancelled"].includes(e.kind)).map(e => e.reason))],
    diagnostic_events: events,
    attempts,
    evidence_eligible: m.disclosure_eligibility === "eligible",
    accepted_evidence: m.terminal_outcome === "answered" ? m.citations.map((c, i) => ({
      evidence_id: derive(c.citation_id, 41 + i),
      citation_id: c.citation_id,
      locator_kind: c.locator.kind,
      locator_value: c.locator.value,
      source_object_id: c.source_object_id,
      source_revision_id: c.source_revision_id,
      revision_number: c.revision_number,
      source_kind: c.source_kind,
      evidence_digest: digest(c.citation_id, 42),
    })) : [],
  };
}

// Every terminal message in the scope's sessions is a run: grouped by session, newest first.
export function scopeRuns(chat) {
  return chat.sessions.flatMap(s => (chat.messagesBySession[s.session_id] ?? [])
    .filter(m => m.state === "terminal")
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .map(m => ({ m, s })));
}

// ─── List: QARunSummaryResponse per question, newest first across the scope ───

export const QA_RUN_OUTCOMES = ["answered", "clarification_required", "abstained", "denied", "failed", "cancelled"];
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const isUuid = (value) => UUID_RE.test(value);
export const LIST_PARAMS = ["outcome", "subject_id", "created_after", "created_before"];

export function runSummaries(chat) {
  return chat.sessions.flatMap(s => (chat.messagesBySession[s.session_id] ?? []).map(m => {
    if (m.state !== "terminal") {
      return {
        run_id: m.run_id, workspace_id: s.workspace_id, environment_id: s.environment_id, subject_kind: "human", subject_id: subjectOf(s),
        state: "running", outcome: null, final_attempt_id: null, created_at: m.created_at, completed_at: null,
        trace_id: m.trace_id, attempt_count: m.state === "pending" ? 0 : 1, session_id: s.session_id,
      };
    }
    const d = runDetail(m, s);
    return {
      run_id: d.run_id, workspace_id: d.workspace_id, environment_id: d.environment_id, subject_kind: d.subject_kind, subject_id: d.subject_id,
      state: d.state, outcome: d.outcome, final_attempt_id: d.final_attempt_id, created_at: d.created_at, completed_at: d.completed_at,
      trace_id: d.trace_id, attempt_count: d.attempts.length, session_id: d.session_id, duration_ms: d.duration_ms, warnings: d.warnings,
    };
  })).sort((a, b) => b.created_at.localeCompare(a.created_at));
}

export function readRunQuery(search = window.location.search) {
  const p = new URLSearchParams(search);
  const date = (key) => (p.get(key) && !Number.isNaN(Date.parse(p.get(key))) ? p.get(key) : undefined);
  const query = {
    outcome: QA_RUN_OUTCOMES.includes(p.get("outcome")) ? p.get("outcome") : undefined,
    subjectId: isUuid(p.get("subject_id") ?? "") ? p.get("subject_id") : undefined,
    createdAfter: date("created_after"),
    createdBefore: date("created_before"),
  };
  if (query.createdAfter && query.createdBefore && Date.parse(query.createdAfter) > Date.parse(query.createdBefore)) query.createdBefore = undefined;
  return query;
}

export function writeRunQuery(query) {
  const url = new URL(window.location.href);
  LIST_PARAMS.forEach(k => url.searchParams.delete(k));
  if (query.outcome) url.searchParams.set("outcome", query.outcome);
  if (query.subjectId) url.searchParams.set("subject_id", query.subjectId);
  if (query.createdAfter) url.searchParams.set("created_after", query.createdAfter);
  if (query.createdBefore) url.searchParams.set("created_before", query.createdBefore);
  window.history.replaceState(null, "", url);
}

export const matchesRunQuery = (r, q) => (!q.outcome || r.outcome === q.outcome)
  && (!q.subjectId || r.subject_id === q.subjectId.toLowerCase())
  && (!q.createdAfter || Date.parse(r.created_at) >= Date.parse(q.createdAfter))
  && (!q.createdBefore || Date.parse(r.created_at) <= Date.parse(q.createdBefore));

// ─── Tree: run → attempts → safe timeline events ───

export function runTree(d) {
  const t0 = new Date(d.created_at).valueOf();
  const offset = (iso) => new Date(iso).valueOf() - t0;
  const attempts = d.attempts.map(a => {
    const start = new Date(a.started_at).valueOf();
    return {
      id: a.attempt_id, type: "attempt", parentId: d.run_id, attempt: a, ms: a.latency_ms, from: offset(a.started_at),
      error: a.state === "failed",
      children: a.timeline.map(e => ({
        id: e.event_id, type: "event", parentId: a.attempt_id, attempt: a, event: e, children: [],
        at: new Date(e.occurred_at).valueOf() - start, from: offset(e.occurred_at),
        error: e.kind === "failed",
      })),
    };
  });
  return { id: d.run_id, type: "run", parentId: null, ms: d.duration_ms, from: 0, error: d.outcome === "failed", children: attempts };
}

export function findNode(node, id) {
  if (node.id === id) return node;
  for (const c of node.children) {
    const hit = findNode(c, id);
    if (hit) return hit;
  }
  return null;
}

// ─── Labels ───

export const stateLabel = (value) => (value === "completed" || value === "terminal" ? Q.terminal : value === "running" ? Q.pending : value || Q.detail.unknown);
export const outcomeLabel = (value) => (value ? Q.outcomes[value] : Q.detail.unknown);

// Spine prints attempt states and timeline kinds as their codes; these glosses sit next to the code.
export const ATTEMPT_STATE = { running: Q.pending, succeeded: "Успешна", failed: "Сбой", cancelled: "Отменена" };
export const EVENT_GLOSS = {
  started: "Попытка начата с закреплёнными версиями",
  context_bound: "Контекст привязан к snapshot проекции",
  completed: "Результат попытки сохранён",
  failed: "Попытка завершилась сбоем",
  cancelled: "Попытка остановлена по запросу",
};
export const REASON_GLOSS = {
  insufficient_evidence: "Недостаточно evidence для ответа",
  access_denied: "Доступ запрещён политикой",
  configuration_unavailable: "Конфигурация недоступна",
  context_unavailable: "Контекст недоступен",
  provider_unavailable: "Провайдер модели недоступен, попытка повторена",
  validation_failed: "Проверка не пройдена",
  retry_exhausted: "Повторы исчерпаны",
  cancelled_by_request: "Остановлено по запросу",
  internal_failure: "Внутренний сбой без раскрытия данных",
};
export const OUTCOME_TONE = { answered: "online", clarification_required: "pending", abstained: "pending", denied: "error", failed: "error", cancelled: "offline" };

export const formatMs = (ms) => (ms < 1000 ? `${ms} мс` : `${(ms / 1000).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} с`);
export const formatOffset = (ms) => `+${formatMs(ms)}`;
export const formatStamp = (iso) => new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "medium" }).format(new Date(iso));
export const formatClock = (iso) => new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(iso));
export const shortRef = (id) => `${id.slice(0, 8)}…`;

// OperationalCorrelationView for the run; the feedback source appears once the answer is rated in chat.
export function correlation(d, feedback) {
  const rated = d.answer_id ? feedback[`answer:${d.answer_id}`] : null;
  const records = [
    { source_kind: "canonical_run", source_schema_version: "answer-question-run:v1", source_record_id: d.run_id, observed_at: d.completed_at,
      run_state: d.state, run_outcome: d.outcome, run_id: d.run_id, message_id: d.message_id, trace_id: d.trace_id },
    { source_kind: "operational_metric", source_schema_version: "operational-observation:v1", source_record_id: derive(d.run_id, 51), observed_at: d.completed_at,
      metric_measurement: "qa_run_latency_ms", metric_coverage: "complete", metric_value: String(d.duration_ms), trace_id: d.trace_id, correlation_id: d.correlation_id },
    { source_kind: "operational_metric", source_schema_version: "operational-observation:v1", source_record_id: derive(d.run_id, 52), observed_at: d.completed_at,
      metric_measurement: "qa_run_cost", metric_coverage: "complete", metric_value: d.attempts.reduce((n, a) => n + Number(a.usage.cost), 0).toFixed(4), metric_currency: "USD" },
    { source_kind: "delivery_metadata", source_schema_version: "outbox-delivery:v1", source_record_id: derive(d.run_id, 53), observed_at: d.completed_at,
      delivery_status: "delivered", delivery_attempt_count: d.attempts.length, delivery_last_error_code: d.attempts.length > 1 ? "generation_unavailable" : null },
    rated?.status === "recorded" && { source_kind: "usefulness_feedback", source_schema_version: "usefulness-feedback:v1", source_record_id: derive(d.run_id, 54),
      observed_at: new Date().toISOString(), feedback_outcome: rated.outcome, message_id: d.message_id },
  ].filter(Boolean);
  const expected = d.answer_id ? ["canonical_run", "operational_metric", "delivery_metadata", "usefulness_feedback"] : ["canonical_run", "operational_metric", "delivery_metadata"];
  const missing = expected.filter(k => !records.some(r => r.source_kind === k));
  return {
    reference_id: d.run_id,
    coverage: missing.length ? "partial" : "complete",
    missing_sources: missing,
    records,
    audit_event_id: derive(d.run_id, 55),
    request_trace_id: derive(d.run_id, 56),
  };
}
