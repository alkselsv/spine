import { ru } from "./ru";

const D = ru.documents;
const OPT = D.filterOptions;

export const WORKSPACES = [
  { id: "primary", label: "Основная" },
  { id: "sales", label: "Продажи" },
];
export const ENVIRONMENTS = [
  { id: "local", label: "Local" },
  { id: "staging", label: "Staging" },
];

export const FILTER_KEYS = ["source_kind", "lifecycle", "processing_state", "publication_status", "publication_health"];
export const ALL = "__all__";

export const FILTER_OPTIONS = Object.fromEntries(
  FILTER_KEYS.map(key => [key, Object.entries(OPT[key]).map(([value, label]) => ({ value, label }))]),
);

export const SCENARIOS = [
  { value: "ready", label: "Готово" },
  { value: "loading", label: "Загрузка" },
  { value: "empty", label: "Пусто" },
  { value: "partial", label: "Частичные данные" },
  { value: "stale", label: "Данные устарели" },
  { value: "degraded", label: "Ограничения сервиса" },
  { value: "permissionDenied", label: "Нет доступа" },
  { value: "validationFailed", label: "Ошибка проверки" },
  { value: "failed", label: "Ошибка загрузки" },
];

export const TOMBSTONE_REASONS = [
  { value: "source_deleted", label: D.documentActions.sourceDeleted },
  { value: "legal_erasure", label: D.documentActions.legalErasure },
  { value: "provider_deleted", label: D.documentActions.providerDeleted },
  { value: "retention_expired", label: D.documentActions.retentionExpired },
];

const ADMISSION = { accepted: "Принят", pending: "Ожидает допуска", rejected: "Отклонён", quarantined: "Карантин" };
const COVERAGE = { complete: "Полное", partial: "Частичное", failed: "Не получено" };
const FRESHNESS = { current: D.current, stale: D.states.stale };

// Deterministic UUIDs so ids stay stable across reloads.
export function uuid(seed) {
  let x = (seed * 2654435761) >>> 0;
  const hex = Array.from({ length: 32 }, () => {
    x = (x * 1103515245 + 12345) >>> 0;
    return (x >>> 24).toString(16).padStart(2, "0")[1];
  }).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

const NOW = Date.now();
const minutesAgo = (m) => new Date(NOW - m * 60_000).toISOString();

const POLICIES = [
  { version: 4, generation: 17, policy_version_id: uuid(901) },
  { version: 3, generation: 12, policy_version_id: uuid(902) },
];
const DOMAINS = [
  { reference: "sd/operations", version: "2026.10", binding_version: 3 },
  { reference: "sd/finance", version: "2026.09", binding_version: 5 },
  { reference: "sd/support", version: "2026.10", binding_version: 2 },
];

const SEGMENTS = {
  document: [
    { locator: { kind: "page", value: "1" }, text: "Регламент описывает порядок приёма, проверки и публикации операционных документов в рабочей области." },
    { locator: { kind: "page", value: "3" }, text: "Изменения вступают в силу после утверждения владельцем Security Domain и повторной индексации проекции." },
    { locator: { kind: "page", value: "7" }, text: "Срок хранения исходных файлов — 36 месяцев; логическое удаление не затрагивает историю ревизий." },
  ],
  record: [
    { locator: { kind: "sheet_row", value: "Лист1:12" }, text: "Контрагент: ООО «Север» · Лимит: 1 200 000 ₽ · Статус: активен" },
    { locator: { kind: "sheet_row", value: "Лист1:13" }, text: "Контрагент: АО «Восток» · Лимит: 850 000 ₽ · Статус: на проверке" },
  ],
  message: [
    { locator: { kind: "message", value: "thread/42#5" }, text: "Подтверждаю: релиз проекции переносится на четверг после сверки с источником истины." },
  ],
};

const STATES = {
  ready: { lifecycle: "current", processing: "processed", publication: "published", health: "healthy", freshness: "current" },
  stale: { lifecycle: "current", processing: "processed", publication: "published", health: "healthy", freshness: "stale" },
  degraded: { lifecycle: "current", processing: "processed", publication: "published", health: "degraded", freshness: "current" },
  parsing: { lifecycle: "processing", processing: "processing", publication: null },
  indexing: { lifecycle: "processing", processing: "projection_requested", publication: null },
  received: { lifecycle: "observed", processing: "received", publication: null },
  admission: { lifecycle: "observed", processing: "admission_pending", publication: null },
  failed: { lifecycle: "observed", processing: "processing_failed", publication: null },
  rejected: { lifecycle: "rejected", processing: "rejected", publication: null },
  tombstoned: { lifecycle: "tombstoned", processing: "processed", publication: null },
  unavailable: { lifecycle: "current", processing: "processed", publication: "unavailable", health: "degraded", freshness: "stale" },
};

export function withState(doc, stateKey, extra = {}) {
  const s = STATES[stateKey];
  const done = s.processing === "processed";
  const publication = s.publication
    ? {
        status: s.publication, health: s.health, freshness: s.freshness,
        projection_kind: "hybrid_search", snapshot_id: doc.publication?.snapshot_id ?? `snap_${doc.source_object_id.slice(0, 8)}`,
        indexed: s.publication === "published",
      }
    : null;
  const revisions = doc.revisions.map((r, i) => (i === 0 ? { ...r, lifecycle: s.lifecycle } : r));
  const admitted = done || s.lifecycle === "processing";
  return {
    ...doc,
    revisions,
    current_revision: {
      ...doc.current_revision,
      lifecycle: s.lifecycle,
      accepted_at: doc.current_revision.accepted_at ?? (admitted ? new Date().toISOString() : null),
      admission_status: admitted ? "accepted" : doc.current_revision.admission_status,
    },
    operational_metadata: {
      ...doc.operational_metadata,
      lifecycle: s.lifecycle, processing_state: s.processing,
      publication_status: s.publication, health: s.health ?? null, freshness: s.freshness ?? null,
    },
    publication,
    coverage: done && stateKey !== "tombstoned" ? doc.coverage ?? "complete" : stateKey === "failed" ? "failed" : null,
    ...extra,
  };
}

const defaultParser = (kind) => (kind === "record" ? "xlsx-tabular 2.1" : kind === "message" ? "thread-normalizer 1.4" : "docling 2.31");

export function createDocument(kind, scope) {
  const now = new Date().toISOString();
  const revision = { source_revision_id: crypto.randomUUID(), lifecycle: "observed", observed_at: now };
  return withState({
    source_object_id: crypto.randomUUID(),
    source_kind: kind,
    scope,
    created_at: now,
    identity: { identity_mode: "upload", upload_identity: `sha256:${crypto.randomUUID().replace(/-/g, "").slice(0, 24)}` },
    revisions: [revision],
    current_revision: { source_revision_id: revision.source_revision_id, observed_at: now, accepted_at: null, accepted_generation: null, admission_status: "pending" },
    operational_metadata: { status: "available", diagnostic_code: null, access_policy: POLICIES[0], security_domain: DOMAINS[0] },
    parser: defaultParser(kind),
    segments: SEGMENTS[kind],
  }, "received");
}

function prependRevision(doc, extra = {}) {
  const now = new Date().toISOString();
  const revision = { source_revision_id: crypto.randomUUID(), lifecycle: "observed", observed_at: now, ...extra };
  return {
    ...doc,
    revisions: [revision, ...doc.revisions.map((r, i) => (i === 0 && r.lifecycle !== "tombstoned" ? { ...r, lifecycle: "superseded" } : r))],
    current_revision: {
      ...doc.current_revision,
      source_revision_id: revision.source_revision_id,
      observed_at: now,
      accepted_at: now,
      accepted_generation: (doc.current_revision.accepted_generation ?? 0) + 1,
      admission_status: "accepted",
    },
    coverage: null,
  };
}

export const replaceRevision = (doc) => withState(prependRevision(doc), "parsing");
export const tombstoneDocument = (doc, reason) => withState(prependRevision(doc, { reason }), "tombstoned");

function makeDoc(i, { kind = "document", state, ago, revisions = 1, admission, coverage, parser, domain = 0, policy = 0, metadataStatus = "available", diagnostic = null, scope = "primary/local" }) {
  const id = uuid(i + 1);
  const revs = Array.from({ length: revisions }, (_, r) => ({
    source_revision_id: uuid(100 + i * 10 + r),
    lifecycle: r === 0 ? STATES[state].lifecycle : "superseded",
    observed_at: minutesAgo(ago + r * 60 * 24 * 9),
  }));
  const base = {
    source_object_id: id,
    source_kind: kind,
    scope,
    created_at: revs[revs.length - 1].observed_at,
    identity: { identity_mode: "upload", upload_identity: `sha256:${uuid(500 + i).replace(/-/g, "").slice(0, 24)}` },
    revisions: revs,
    current_revision: {
      source_revision_id: revs[0].source_revision_id,
      observed_at: revs[0].observed_at,
      accepted_at: ["received", "admission", "rejected"].includes(state) ? null : minutesAgo(Math.max(0, ago - 1)),
      accepted_generation: ["received", "admission", "rejected"].includes(state) ? null : 3 + revisions,
      admission_status: admission ?? (state === "rejected" ? "rejected" : ["received", "admission"].includes(state) ? "pending" : "accepted"),
    },
    operational_metadata: {
      status: metadataStatus,
      diagnostic_code: diagnostic,
      access_policy: POLICIES[policy],
      security_domain: DOMAINS[domain],
    },
    parser: parser ?? defaultParser(kind),
    coverage,
    segments: SEGMENTS[kind],
  };
  return withState(base, state);
}

export const INITIAL_DOCUMENTS = [
  makeDoc(0, { state: "parsing", ago: 2 }),
  makeDoc(1, { state: "indexing", ago: 6, domain: 1 }),
  makeDoc(2, { state: "ready", ago: 38, revisions: 3 }),
  makeDoc(3, { kind: "record", state: "stale", ago: 95, domain: 1, revisions: 2 }),
  makeDoc(4, { kind: "message", state: "degraded", ago: 160, domain: 2 }),
  makeDoc(14, { state: "ready", ago: 20, scope: "primary/staging" }),
  makeDoc(15, { kind: "record", state: "indexing", ago: 4, domain: 1, scope: "primary/staging" }),
  makeDoc(16, { state: "ready", ago: 300, domain: 1, scope: "sales/local" }),
  makeDoc(17, { kind: "message", state: "failed", ago: 45, domain: 2, scope: "sales/local" }),
  makeDoc(18, { kind: "record", state: "ready", ago: 900, domain: 1, scope: "sales/local" }),
  makeDoc(5, { state: "failed", ago: 240 }),
  makeDoc(6, { state: "ready", ago: 60 * 7, coverage: "partial", policy: 1 }),
  makeDoc(7, { state: "rejected", ago: 60 * 11 }),
  makeDoc(8, { kind: "message", state: "admission", ago: 60 * 14, domain: 2 }),
  makeDoc(9, { kind: "record", state: "ready", ago: 60 * 26, domain: 1 }),
  makeDoc(10, { state: "unavailable", ago: 60 * 30, domain: 1 }),
  makeDoc(11, { state: "tombstoned", ago: 60 * 52, revisions: 2 }),
  makeDoc(12, { state: "ready", ago: 60 * 74, revisions: 2 }),
  makeDoc(13, { kind: "record", state: "ready", ago: 60 * 96, policy: 1 }),
].map(d => d.operational_metadata.lifecycle === "tombstoned"
  ? { ...d, revisions: d.revisions.map((r, i) => (i === 0 ? { ...r, reason: "retention_expired" } : r)) }
  : d);

// ─── Labels (ported from DocumentsPage.tsx / DocumentAccessMetadata.tsx) ───

export const optionLabel = (key, value) => (value ? OPT[key][value] ?? D.unknown : D.unknown);
export const sourceKindLabel = (value) => OPT.source_kind[value] ?? "Источник";
export const admissionLabel = (value) => ADMISSION[value] ?? D.unknown;
export const coverageLabel = (value) => COVERAGE[value] ?? D.unknown;
export const freshnessLabel = (value) => FRESHNESS[value] ?? D.unknown;
export const reasonLabel = (value) => TOMBSTONE_REASONS.find(r => r.value === value)?.label ?? D.unknown;

export const lifecycleOf = (doc) => doc.operational_metadata?.lifecycle ?? doc.current_revision?.lifecycle;

export function healthLabel(publication) {
  if (publication?.health === "degraded") return OPT.publication_health.degraded;
  if (publication?.health === "healthy" && publication.freshness === "stale") return D.states.stale;
  if (publication?.health === "healthy") return OPT.publication_health.healthy;
  return D.unknown;
}

// Returns the label plus a StatusBadge tone, mirroring documentStateLabel() precedence.
export function documentState(doc) {
  const lifecycle = lifecycleOf(doc);
  const processing = doc.operational_metadata?.processing_state;
  const publication = doc.publication?.status;
  const S = D.states;
  if (publication === "unavailable") return { key: "unavailable", label: S.unavailable, tone: "error" };
  if (publication === "unpublished") return { key: "unpublished", label: S.unpublished, tone: "offline" };
  if (lifecycle === "rejected" || processing === "rejected") return { key: "rejected", label: S.rejected, tone: "error" };
  if (lifecycle === "tombstoned") return { key: "tombstoned", label: S.tombstoned, tone: "offline" };
  if (processing === "processing_failed") return { key: "failed", label: S.processingFailed, tone: "error" };
  if (processing === "projection_requested") return { key: "indexing", label: S.indexing, tone: "pending", pulse: true };
  if (lifecycle === "processing" || processing === "processing") return { key: "parsing", label: S.parsing, tone: "pending", pulse: true };
  if (publication === "published" && (processing === "processed" || lifecycle === "current")) return { key: "ready", label: S.ready, tone: "online" };
  if (processing) return { key: "uploaded", label: S.uploaded, tone: "pending" };
  return { key: "unknown", label: D.unknown, tone: "default" };
}

// StatusBadge tones → StatusDot statuses (the dot has "busy" where the badge has "pending").
export const DOT_STATUS = { online: "online", pending: "busy", error: "error", offline: "offline", default: "offline" };

export const PRESETS = [
  { value: "all", label: "Все" },
  { value: "inWork", label: "В работе" },
  { value: "attention", label: "Требуют внимания" },
  { value: "ready", label: "Готовы" },
];

export function needsAttention(doc) {
  return ["failed", "rejected", "unavailable"].includes(documentState(doc).key) || metadataState(doc).key !== "available";
}

export function matchesPreset(doc, preset) {
  if (preset === "all") return true;
  const attention = needsAttention(doc);
  if (preset === "attention") return attention;
  const key = documentState(doc).key;
  if (preset === "inWork") return !attention && ["parsing", "indexing", "uploaded"].includes(key);
  return !attention && key === "ready";
}

export const isInFlight = (doc) => ["parsing", "indexing"].includes(documentState(doc).key);

export function metadataState(doc) {
  const m = doc.operational_metadata;
  const health = m?.health ?? doc.publication?.health;
  const freshness = m?.freshness ?? doc.publication?.freshness;
  if (!m || m.status === "unavailable") return { key: "unavailable", label: D.states.unavailable, tone: "error" };
  if (m.status === "degraded" || health === "degraded" || m.diagnostic_code) return { key: "degraded", label: OPT.publication_health.degraded, tone: "pending" };
  if (freshness === "stale") return { key: "stale", label: D.states.stale, tone: "pending" };
  return { key: "available", label: D.available, tone: "online" };
}

export function metadataAction(doc) {
  const m = doc.operational_metadata;
  if (m?.diagnostic_code === "policy_unavailable") return D.policyAction;
  if (!m || m.status === "unavailable") return D.currentStateAction;
  const state = metadataState(doc).key;
  return state === "stale" || state === "degraded" ? D.retryAction : null;
}

export function formatDate(iso, withTime = false) {
  if (!iso) return D.unknown;
  const date = new Date(iso);
  if (Number.isNaN(date.valueOf())) return D.unknown;
  return new Intl.DateTimeFormat("ru-RU", withTime ? { dateStyle: "medium", timeStyle: "short" } : { dateStyle: "medium" }).format(date);
}

export function formatRelative(iso) {
  const minutes = Math.round((Date.now() - new Date(iso).valueOf()) / 60_000);
  if (minutes < 1) return "только что";
  if (minutes < 60) return `${minutes} мин назад`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)} ч назад`;
  return formatDate(iso);
}

export const shortId = (id) => `${id.slice(0, 8)}…${id.slice(-4)}`;
// The last UUID group is what tells documents apart at a glance.
export const idHead = (id) => id.slice(0, -12);
export const idTail = (id) => id.slice(-12);

export const PIPELINE = ["Допуск", "Разбор", "Индексация", "Публикация"];
const STAGE = { uploaded: 0, parsing: 1, indexing: 2 };
export const pipelineStage = (doc) => STAGE[documentState(doc).key] ?? null;

// What went wrong and the single command that resolves it; null when the document needs nothing.
export function documentIssue(doc) {
  const key = documentState(doc).key;
  const C = D.commands;
  if (key === "failed") return {
    tone: "danger", title: "Обработка не удалась",
    description: "Разбор текущей ревизии завершился ошибкой. Повтор операции разрешён.", action: "reprocess", actionLabel: C.retry,
  };
  if (key === "rejected") return {
    tone: "danger", title: "Ревизия отклонена",
    description: "Файл не прошёл правила допуска. Загрузите исправленную версию.", action: "replace", actionLabel: D.documentActions.submitReplacement,
  };
  if (key === "unavailable") return {
    tone: "danger", title: "Документ недоступен для поиска",
    description: "Проекция недоступна, ответы его не учитывают. Обработайте ревизию заново.", action: "reprocess", actionLabel: C.reprocess,
  };
  const meta = metadataState(doc);
  const policy = doc.operational_metadata?.diagnostic_code === "policy_unavailable";
  if (meta.key === "unavailable") return {
    tone: "danger", title: "Метаданные доступа недоступны", description: metadataAction(doc), action: null,
  };
  if (meta.key === "degraded") return {
    tone: "danger", title: "Проекция ограничена",
    description: `Часть метаданных доступа не получена, поиск может пропускать документ. ${metadataAction(doc) ?? ""}`.trim(),
    action: policy ? null : "reprocess", actionLabel: C.reprocess,
  };
  if (meta.key === "stale") return {
    tone: "warning", title: "Проекция устарела",
    description: "Поиск отвечает по прошлой версии индекса. Обработайте ревизию заново, чтобы обновить проекцию.", action: "reprocess", actionLabel: C.reprocess,
  };
  return null;
}

export function locatorLabel({ kind, value }) {
  if (kind === "page") return `${D.locatorPage} ${value}`;
  if (kind === "sheet_row") {
    const [sheet, row] = value.split(":");
    return `${D.locatorSheet} ${sheet}, ${D.locatorRow} ${row}`;
  }
  return value;
}

// ─── Upload validation (limits from DocumentUpload) ───

const MB = 1024 * 1024;
export const UPLOAD_ACCEPT = ".pdf,.docx,.xlsx,.md,.markdown,.txt";
const UPLOAD_EXT = ["pdf", "docx", "xlsx", "md", "markdown", "txt"];

export function validateBatch(files) {
  const V = D.upload.validation;
  if (!files.length) return V.empty_batch;
  if (files.length > 20) return V.too_many_files;
  if (files.some(f => !UPLOAD_EXT.includes(f.name.split(".").pop().toLowerCase()))) return V.unsupported_type;
  if (files.some(f => f.size > 10 * MB)) return V.file_too_large;
  if (files.reduce((s, f) => s + f.size, 0) > 50 * MB) return V.batch_too_large;
  return null;
}

export const fileKind = (name) => (/\.xlsx$/i.test(name) ? "record" : "document");

export function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < MB) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / MB).toFixed(1)} MB`;
}

export const SAMPLE_FILES = [
  { name: "регламент-приёмки.pdf", size: 2.4 * MB },
  { name: "лимиты-контрагентов.xlsx", size: 640 * 1024 },
  { name: "runbook-проекции.md", size: 38 * 1024 },
];
