import { INITIAL_DOCUMENTS, uuid } from "../spineData";
import { ru } from "../ru";

const C = ru.chat;
const NOW = Date.now();
const minutesAgo = (m) => new Date(NOW - m * 60_000).toISOString();

// Citations point at documents from the Knowledge prototype, so the inspector can open them there.
const docById = (i) => INITIAL_DOCUMENTS.find(d => d.source_object_id === uuid(i + 1));

function cite(seed, docIndex, locator, excerpt) {
  const d = docById(docIndex);
  return {
    citation_id: uuid(seed),
    source_object_id: d.source_object_id,
    source_kind: d.source_kind,
    source_revision_id: d.current_revision.source_revision_id,
    revision_number: d.revisions.length,
    locator,
    excerpt,
  };
}

export const EXHAUSTIVE = {
  partiality_status: "exhaustive", coverage_status: "complete", conflict_status: "none",
  partial_fallback_authority: null, serving_decision: "serve", validation_decision: "complete",
};
const PARTIAL = {
  partiality_status: "partial", coverage_status: "insufficient", conflict_status: "none",
  partial_fallback_authority: "accepted_preference", serving_decision: "serve", validation_decision: "incomplete_authorized_coverage",
};
const CONFLICT = {
  partiality_status: "exhaustive", coverage_status: "complete", conflict_status: "presented",
  partial_fallback_authority: null, serving_decision: "serve", validation_decision: "grounded_conflict",
};

// Answer text is authored as blocks of parts; a part with `claim` is an accepted claim span.
const RETENTION = {
  citations: [
    cite(8101, 2, { kind: "page", value: "7" }, "Срок хранения исходных файлов — 36 месяцев; логическое удаление не затрагивает историю ревизий."),
    cite(8102, 12, { kind: "page", value: "4" }, "Tombstone-ревизия добавляется к источнику; предыдущие SourceRevision остаются в истории и доступны для аудита."),
  ],
  claims: {
    c1: { kind: "factual", rel: [{ kind: "independent", cites: [0] }] },
    c2: { kind: "factual", rel: [{ kind: "corroborating", cites: [0, 1] }] },
    c3: { kind: "derived", rel: [{ kind: "derivation_input", cites: [1] }] },
  },
  blocks: [
    { type: "p", parts: [{ text: "Исходные файлы хранятся 36 месяцев.", claim: "c1" }] },
    { type: "p", parts: [
      { text: "Логическое удаление не затрагивает историю: сервер добавляет новую tombstone-ревизию, а предыдущие ревизии остаются доступны для аудита.", claim: "c2" },
      " ",
      { text: "Поэтому удалённый документ можно проследить по истории, но он больше не участвует в ответах.", claim: "c3" },
    ] },
  ],
  completeness: EXHAUSTIVE,
  context: 12,
};

const REGULATION = {
  citations: [
    cite(8201, 2, { kind: "page", value: "3" }, "Изменения вступают в силу после утверждения владельцем Security Domain и повторной индексации проекции."),
    cite(8202, 2, { kind: "page", value: "1" }, "Регламент описывает порядок приёма, проверки и публикации операционных документов в рабочей области."),
  ],
  claims: {
    c1: { kind: "factual", rel: [{ kind: "joint", cites: [0, 1] }] },
    c2: { kind: "factual", rel: [{ kind: "independent", cites: [0] }] },
    c3: { kind: "factual", rel: [{ kind: "independent", cites: [0] }] },
  },
  blocks: [
    { type: "p", parts: [{ text: "Изменения регламента приёма и публикации документов вступают в силу не сразу, а после двух шагов:", claim: "c1" }] },
    { type: "ul", items: [
      [{ text: "утверждения владельцем Security Domain;", claim: "c2" }],
      [{ text: "повторной индексации проекции.", claim: "c3" }],
    ] },
    { type: "p", parts: ["До завершения индексации ответы опираются на предыдущую опубликованную версию."] },
  ],
  completeness: EXHAUSTIVE,
  context: 9,
};

const REGULATION_STAGING = {
  ...REGULATION,
  citations: [
    cite(8501, 14, { kind: "page", value: "3" }, "Изменения вступают в силу после утверждения владельцем Security Domain и повторной индексации проекции."),
    cite(8502, 14, { kind: "page", value: "1" }, "Регламент описывает порядок приёма, проверки и публикации операционных документов в рабочей области."),
  ],
};

const LIMIT_EAST = {
  citations: [
    cite(8301, 3, { kind: "sheet_row", value: "Лист1:13" }, "Контрагент: АО «Восток» · Лимит: 850 000 ₽ · Статус: на проверке"),
  ],
  claims: {
    c1: { kind: "factual", rel: [{ kind: "independent", cites: [0] }] },
    c2: { kind: "factual", rel: [{ kind: "independent", cites: [0] }] },
  },
  blocks: [
    { type: "p", parts: [{ text: "Лимит АО «Восток» — 850 000 ₽.", claim: "c1" }, " ", { text: "Контрагент находится на проверке, поэтому лимит может измениться.", claim: "c2" }] },
    { type: "p", parts: ["Таблица лимитов проиндексирована по устаревшей проекции — более свежих строк в ответе может не быть."] },
  ],
  completeness: PARTIAL,
  context: 4,
};

const LIMIT_NORTH = {
  citations: [
    cite(8311, 3, { kind: "sheet_row", value: "Лист1:12" }, "Контрагент: ООО «Север» · Лимит: 1 200 000 ₽ · Статус: активен"),
  ],
  claims: {
    c1: { kind: "factual", rel: [{ kind: "independent", cites: [0] }] },
    c2: { kind: "factual", rel: [{ kind: "independent", cites: [0] }] },
  },
  blocks: [
    { type: "p", parts: [{ text: "Лимит ООО «Север» — 1 200 000 ₽.", claim: "c1" }, " ", { text: "Контрагент активен.", claim: "c2" }] },
    { type: "p", parts: ["Таблица лимитов проиндексирована по устаревшей проекции — более свежих строк в ответе может не быть."] },
  ],
  completeness: PARTIAL,
  context: 4,
};

const RELEASE = {
  citations: [
    cite(8401, 12, { kind: "page", value: "2" }, "Релиз проекции запланирован на вторник после закрытия окна изменений."),
    cite(8402, 4, { kind: "message", value: "thread/42#5" }, "Подтверждаю: релиз проекции переносится на четверг после сверки с источником истины."),
  ],
  claims: {
    c1: { kind: "factual", rel: [{ kind: "independent", cites: [0] }] },
    c2: { kind: "factual", rel: [{ kind: "independent", cites: [1] }, { kind: "contradiction", related: "c1" }] },
  },
  blocks: [
    { type: "p", parts: ["Источники расходятся."] },
    { type: "p", parts: [{ text: "В плане релизов дата — вторник, после закрытия окна изменений.", claim: "c1" }] },
    { type: "p", parts: [{ text: "В рабочей переписке подтверждён перенос на четверг после сверки с источником истины.", claim: "c2" }] },
  ],
  completeness: CONFLICT,
  context: 7,
  reconnect: true,
};

const ANSWERS = [
  { match: /хран|удален|удалени/i, answer: RETENTION },
  { match: /вступ|регламент|изменени/i, answer: REGULATION },
  { match: /восток/i, answer: LIMIT_EAST },
  { match: /север/i, answer: LIMIT_NORTH },
  { match: /лимит|контрагент/i, clarify: "Уточните контрагента: в источниках есть лимиты для ООО «Север» и АО «Восток»." },
  { match: /релиз/i, answer: RELEASE },
];

export const SUGGESTIONS = [
  "Какой срок хранения исходных файлов?",
  "Когда изменения регламента вступают в силу?",
  "Какой лимит у контрагента?",
  "Когда релиз проекции?",
];

// Answers stay grounded: anything the sources don't cover ends in an abstention.
export function scriptFor(question) {
  const hit = ANSWERS.find(a => a.match.test(question));
  if (!hit) return { outcome: "abstained", reason: "no_usable_evidence" };
  if (hit.clarify) return { outcome: "clarification_required", prompt: hit.clarify };
  return { outcome: "answered", ...hit.answer };
}

// ─── Message factory (shape of QAMessageResponse plus the run/citation data the inspectors load) ───

const VERSIONS = { agent: "answer-question@1.8.2", prompt: "grounded-qa@14", model: "standard-ru@2026.10" };

function run(seed, { ms, context = 0, outcome = "answered", tokens = [0, 0], attempts = 1 }) {
  const validation = outcome === "answered";
  return {
    attempt_id: uuid(seed + 1),
    attempt_number: attempts,
    versions: VERSIONS,
    duration_ms: ms,
    snapshot: uuid(seed + 2),
    retrieval: "hybrid_search",
    freshness: context ? "current" : null,
    context,
    usage: { model_invocations: validation ? 2 : 1, input_tokens: tokens[0], output_tokens: tokens[1], cached_input_tokens: Math.round(tokens[0] * 0.4), cost: (tokens[0] * 0.0000012 + tokens[1] * 0.0000048).toFixed(4), currency: "USD" },
    timeline: [
      { kind: "run_started", ms: 0 },
      { kind: "context_retrieval", ms: Math.round(ms * 0.08), count: context },
      ...(validation ? [{ kind: "answer_generation", ms: Math.round(ms * 0.62) }, { kind: "validation", ms: Math.round(ms * 0.9) }] : []),
      { kind: outcome, ms },
    ],
  };
}

// `retried` runs fail their first attempt on the provider and succeed or fail for good on the second.
export function makeMessage({ seed, sessionId, ordinal, ago, question, script, clarifies = null, hidden = false, retried = false }) {
  const tokens = script.outcome === "answered" ? [2400 + script.context * 180, 140 + script.blocks.length * 60] : [1800, 40];
  const ms = (script.outcome === "answered" ? 4200 + script.context * 180 : 2100) + (retried ? 1600 : 0);
  return {
    message_id: uuid(seed),
    session_id: sessionId,
    ordinal,
    created_at: minutesAgo(ago),
    question,
    clarifies,
    state: "terminal",
    terminal_outcome: script.outcome,
    reason: script.reason ?? null,
    disclosure_eligibility: hidden ? "ineligible" : "eligible",
    run_id: uuid(seed + 10),
    trace_id: uuid(seed + 20).replace(/-/g, ""),
    answer_id: script.outcome === "answered" ? uuid(seed + 30) : null,
    version: script.outcome === "answered" ? 4 : 3,
    clarification_prompt: script.prompt ?? null,
    citations: script.citations ?? [],
    claims: script.claims ?? {},
    blocks: script.blocks ?? [],
    completeness: script.completeness ?? null,
    run: run(seed + 40, { ms, context: script.context ?? 0, outcome: script.outcome, tokens, attempts: retried ? 2 : 1 }),
    revealed: Infinity,
  };
}

// ─── Sessions ───

const S1 = uuid(7001);
const S2 = uuid(7002);
const S3 = uuid(7003);
const S4 = uuid(7004);
const S5 = uuid(7005);

const session = (id, scope, status, version, ago, createdAgo) => {
  const [workspace_id, environment_id] = scope.split("/");
  return { session_id: id, workspace_id, environment_id, status, version, updated_at: minutesAgo(ago), created_at: minutesAgo(createdAgo) };
};

export const INITIAL_SESSIONS = [
  session(S1, "primary/local", "active", 11, 4, 52),
  session(S2, "primary/local", "active", 5, 95, 140),
  session(S3, "primary/local", "archived", 7, 60 * 30, 60 * 34),
  session(S4, "primary/staging", "active", 3, 22, 30),
  session(S5, "sales/local", "active", 2, 60 * 5, 60 * 5),
];

const ask = (q, n) => scriptFor(q) && { question: q, script: scriptFor(q), ordinal: n };
const s1Clarify = uuid(6103);

export const INITIAL_MESSAGES = {
  [S1]: [
    makeMessage({ seed: 6101, sessionId: S1, ago: 48, ...ask("Какой срок хранения исходных файлов и что происходит с историей при удалении?", 1) }),
    makeMessage({ seed: 6102, sessionId: S1, ago: 41, ...ask("Когда изменения регламента вступают в силу?", 2) }),
    makeMessage({ seed: 6103, sessionId: S1, ago: 30, ...ask("Какой лимит у контрагента?", 3) }),
    makeMessage({ seed: 6104, sessionId: S1, ago: 29, ...ask("Для АО «Восток»", 4), clarifies: s1Clarify }),
    makeMessage({ seed: 6105, sessionId: S1, ago: 4, ...ask("Когда релиз проекции?", 5), retried: true }),
  ],
  [S2]: [
    makeMessage({ seed: 6201, sessionId: S2, ago: 132, ...ask("Кто утвердил бюджет на 2027 год?", 1) }),
    makeMessage({ seed: 6202, sessionId: S2, ago: 120, question: "Покажи лимиты отдела продаж", ordinal: 2, script: { outcome: "denied", reason: "authorization_denied" } }),
    makeMessage({ seed: 6203, sessionId: S2, ago: 110, question: "Сводка по инцидентам безопасности", ordinal: 3, script: { outcome: "answered", ...REGULATION }, hidden: true }),
    makeMessage({ seed: 6204, sessionId: S2, ago: 101, question: "Сравни версии регламента за год", ordinal: 4, script: { outcome: "failed", reason: "internal_failure" }, retried: true }),
    makeMessage({ seed: 6205, sessionId: S2, ago: 96, question: "Перечисли все документы в карантине", ordinal: 5, script: { outcome: "cancelled", reason: "request_cancelled" } }),
  ],
  [S3]: [
    makeMessage({ seed: 6301, sessionId: S3, ago: 60 * 33, ...ask("Что происходит с документом при логическом удалении?", 1) }),
  ],
  [S4]: [
    makeMessage({ seed: 6401, sessionId: S4, ago: 25, ordinal: 1, question: "Когда изменения регламента вступают в силу?", script: { outcome: "answered", ...REGULATION_STAGING } }),
  ],
  [S5]: [],
};

// ─── Labels ───

export const OUTCOME = {
  answered: { tone: "online", label: C.messageAnswered },
  clarification_required: { tone: "pending", label: C.messageClarificationRequired },
  abstained: { tone: "pending", label: C.messageAbstained },
  denied: { tone: "error", label: C.messageDenied },
  failed: { tone: "error", label: C.messageFailed },
  cancelled: { tone: "offline", label: C.messageCancelled },
};

export const REASONS = {
  no_usable_evidence: "В опубликованных источниках области нет фрагментов, на которые можно опереться.",
  insufficient_evidence: "Найденных фрагментов недостаточно, чтобы подтвердить ответ.",
  unmet_freshness: "Проекция не удовлетворяет требованию свежести.",
  authorization_denied: C.askDenied,
  internal_failure: "Сервис не смог завершить запуск. Защищённые данные не раскрыты.",
  request_cancelled: "Запрос остановлен до выдачи ответа.",
};

export const TIMELINE = {
  run_started: "Запрос принят",
  context_retrieval: "Контекст собран",
  answer_generation: "Ответ сформирован",
  validation: "Утверждения проверены",
  answered: "Ответ выдан",
  clarification_required: "Запрошено уточнение",
  abstained: "Ответ не выдан",
  denied: "Доступ запрещён",
  failed: "Запуск завершился ошибкой",
  cancelled: "Запуск остановлен",
};

export const CLAIM_KIND = { factual: "Факт", derived: "Вывод" };

export const STAGE_LABEL = {
  pending: C.messagePending,
  accepted: "Запрос принят…",
  retrieval: "Ищем контекст…",
  answering: "Формируем и проверяем ответ…",
  streaming: "Выдаём проверенный ответ…",
};

export const sessionTitle = (s) => C.sessionName(s.session_id.slice(-8));

export function messageStateLabel(m) {
  if (m.state === "pending") return C.messagePending;
  if (m.state === "running") return C.messageRunning;
  return OUTCOME[m.terminal_outcome]?.label ?? C.messagePending;
}

export const completenessTone = (c) => (c.conflict_status === "presented" || c.partiality_status !== "exhaustive" ? "pending" : "online");

export function completenessRows(c) {
  const K = C.completeness;
  return [
    [K.coverage, K.coverageStates[c.coverage_status]],
    [K.conflict, K.conflictStates[c.conflict_status]],
    [K.fallback, c.partial_fallback_authority ? K.fallbackAuthorities[c.partial_fallback_authority] : K.notAuthorized],
    [K.serving, K.servingDecisions[c.serving_decision]],
    [K.validation, K.validationDecisions[c.validation_decision]],
  ];
}

// Claim order fixes citation numbering, the way Spine numbers accepted citation_ids.
export function claimOrder(m) {
  const ids = [];
  const walk = (parts) => parts.forEach(p => { if (typeof p !== "string" && p.claim && !ids.includes(p.claim)) ids.push(p.claim); });
  m.blocks.forEach(b => (b.type === "ul" ? b.items.forEach(walk) : walk(b.parts)));
  return ids;
}

export const plainText = (m) => m.blocks.map(b => (b.type === "ul"
  ? b.items.map(parts => `- ${parts.map(p => (typeof p === "string" ? p : p.text)).join("")}`).join("\n")
  : b.parts.map(p => (typeof p === "string" ? p : p.text)).join(""))).join("\n\n");

export const partText = (p) => (typeof p === "string" ? p : p.text);
export const textLength = (m) => m.blocks.reduce((n, b) => n + (b.type === "ul" ? b.items.flat() : b.parts).reduce((k, p) => k + partText(p).length, 0), 0);

export const formatSeconds = (ms) => `${(ms / 1000).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} с`;
export const formatTime = (iso) => new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(new Date(iso));
