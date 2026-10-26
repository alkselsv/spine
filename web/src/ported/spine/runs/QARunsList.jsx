import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, ChevronRight, Info, ListFilter, MessageSquare, SearchX, UserRound, X } from "lucide-react";
import {
  Button, Caption, Card, DatePicker, EmptyState, Select, Skeleton, StatusDot, TextInput, interactiveBase, motion, tokens, usePal, useThemeContext,
} from "../../halaska-kit";
import { DOT_STATUS, formatDate, formatRelative } from "../spineData";
import { IdText } from "../SpineParts";
import { IconAction, Tip } from "../chat/SpineChatParts";
import { ru } from "../ru";
import { createScopedApiClient } from "../../../api/client";
import { useAuth } from "../../../app/auth";
import { loadQARuns } from "../../../features/runs/qa-run-api";
import {
  OUTCOME_TONE, QA_RUN_OUTCOMES, formatClock, formatMs, isUuid, matchesRunQuery, outcomeLabel, readRunQuery, runSummaries, stateLabel, writeRunQuery,
} from "./qaRunData";

const Q = ru.qaRuns;
const ALL = "__all__";
const LOADING_MS = 350;

const COLUMNS = [
  { key: "run", label: Q.run, width: "27%" },
  { key: "result", label: Q.result, width: "21%" },
  { key: "initiator", label: Q.initiatorColumn, width: "21%", wide: true },
  { key: "created", label: Q.createdAt, width: "15%" },
  { key: "duration", label: Q.duration, width: "16%" },
  { key: "open", label: "", width: "40px" },
];

// The kit calendar picks whole days, so "after" opens at the start of the day and "before" closes at its end.
const dayEdge = (date, end) => {
  const d = new Date(date);
  if (end) d.setHours(23, 59, 59, 999); else d.setHours(0, 0, 0, 0);
  return d.toISOString();
};
const draftOf = (q) => ({
  outcome: q.outcome ?? "", subjectId: q.subjectId ?? "",
  createdAfter: q.createdAfter ? new Date(q.createdAfter) : null, createdBefore: q.createdBefore ? new Date(q.createdBefore) : null,
});

function DateField({ label, value, onChange }) {
  const pal = usePal();
  return (
    <div style={{ position: "relative" }}>
      <DatePicker label={label} placeholder="Любая дата" value={value} onChange={onChange} />
      {value && (
        <button type="button" aria-label={`Очистить: ${label}`} onClick={() => onChange(null)} style={{
          ...interactiveBase, position: "absolute", right: 8, bottom: 9, width: 20, height: 20, borderRadius: tokens.radius.xs,
          display: "inline-flex", alignItems: "center", justifyContent: "center", background: "transparent", color: pal.textTertiary,
        }}><X size={14} strokeWidth={1.75} /></button>
      )}
    </div>
  );
}

function Filters({ draft, setDraft }) {
  const theme = useThemeContext();
  const pal = usePal();
  const set = (key) => (value) => setDraft(d => ({ ...d, [key]: value }));
  return (
    <div data-field-stroke={theme === "dark" || undefined} style={{ "--field-stroke": pal.border, display: "flex", alignItems: "flex-end", gap: 10, flexWrap: "wrap" }}>
      <Select label={Q.outcome} value={draft.outcome || ALL} style={{ width: 188 }}
        options={[{ value: ALL, label: ru.documents.all }, ...QA_RUN_OUTCOMES.map(o => ({ value: o, label: Q.outcomes[o] }))]}
        onChange={(v) => set("outcome")(v === ALL ? "" : v)} />
      <TextInput label={Q.initiator} placeholder={Q.initiatorPlaceholder} value={draft.subjectId} onChange={set("subjectId")}
        icon={<UserRound size={14} strokeWidth={1.5} />} style={{ flex: "1 1 220px", maxWidth: 340 }} />
      <DateField label={Q.createdAfter} value={draft.createdAfter} onChange={set("createdAfter")} />
      <DateField label={Q.createdBefore} value={draft.createdBefore} onChange={set("createdBefore")} />
    </div>
  );
}

function DurationBar({ ms, max }) {
  const pal = usePal();
  return (
    <span aria-hidden="true" style={{ position: "relative", width: 44, height: 6, flexShrink: 0, borderRadius: 3, background: pal.bgMuted, overflow: "hidden" }}>
      <span style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: `${Math.max(6, (ms / max) * 100)}%`, borderRadius: 3, background: pal.textTertiary }} />
    </span>
  );
}

const sub = (pal, extra) => ({ ...tokens.type.xs, color: pal.textTertiary, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", ...extra });
function Cell({ col, r, pal, ctx }) {
  const running = r.state !== "completed";
  switch (col) {
    case "run":
      return (
        <span style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
          <span aria-hidden="true" style={{
            width: 28, height: 28, flexShrink: 0, borderRadius: tokens.radius.sm, background: pal.bgMuted, color: pal.textSecondary,
            display: "inline-flex", alignItems: "center", justifyContent: "center",
          }}><Activity size={14} strokeWidth={1.5} /></span>
          <span style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
            <IdText id={r.run_id} />
            <span style={sub(pal)}>
              {ru.chat.sessionName(r.session_id.slice(-8))}{r.attempt_count > 1 ? ` · попыток: ${r.attempt_count}` : ""}
            </span>
          </span>
        </span>
      );
    case "result":
      return (
        <span style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 7, whiteSpace: "nowrap" }}>
            <StatusDot status={running ? "busy" : DOT_STATUS[OUTCOME_TONE[r.outcome]]} pulse={running} size={7} />
            {running ? Q.pending : outcomeLabel(r.outcome)}
          </span>
          <span style={sub(pal, { paddingLeft: 14 })}>
            {running ? `${Q.status}: ${stateLabel(r.state)}` : stateLabel(r.state)}
            {r.warnings?.length > 0 && (
              <span title={r.warnings.join(", ")} style={{ fontFamily: tokens.font.mono, color: pal.warning }}>
                {" · "}{r.warnings.at(-1)}{r.warnings.length > 1 ? ` +${r.warnings.length - 1}` : ""}
              </span>
            )}
          </span>
        </span>
      );
    case "initiator":
      return (
        <span style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
          <span style={{ display: "flex", flexDirection: "column", minWidth: 0, flex: 1 }}>
            <IdText id={r.subject_id} />
            <span style={sub(pal)}>{Q.subjectKinds[r.subject_kind] ?? Q.unknown}</span>
          </span>
          <span style={{ opacity: ctx.hover || ctx.filtered ? 1 : 0, transition: `opacity ${motion.fast} ${motion.easeInOut}` }}>
            <IconAction label={ctx.filtered ? "Запуски этого инициатора уже показаны" : "Показать запуски этого инициатора"} active={ctx.filtered}
              onClick={(e) => { e.stopPropagation(); ctx.onFilterSubject(r.subject_id); }}>
              <ListFilter size={13} strokeWidth={1.5} />
            </IconAction>
          </span>
        </span>
      );
    case "created":
      return (
        <span style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
          <time dateTime={r.created_at} style={{ whiteSpace: "nowrap" }}>{formatRelative(r.created_at)}</time>
          <span style={sub(pal)}>{formatDate(r.created_at, true)}</span>
        </span>
      );
    case "duration":
      return running ? <span style={{ color: pal.textTertiary }}>{Q.unknown}</span> : (
        <span style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8, fontFamily: tokens.font.mono, ...tokens.type.xs, color: pal.text }}>
            {formatMs(r.duration_ms)}<DurationBar ms={r.duration_ms} max={ctx.maxMs} />
          </span>
          <span style={sub(pal)} title={formatDate(r.completed_at, true)}>{Q.completedAt.toLowerCase()} {formatClock(r.completed_at)}</span>
        </span>
      );
    default:
      return <ChevronRight size={14} strokeWidth={1.5} color={ctx.hover ? pal.textSecondary : pal.textMuted} />;
  }
}

function onRowKeyDown(e, open) {
  if (e.target !== e.currentTarget) return;
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); return; }
  const step = e.key === "ArrowDown" || e.key === "j" ? 1 : e.key === "ArrowUp" || e.key === "k" ? -1 : 0;
  if (!step) return;
  e.preventDefault();
  (step > 0 ? e.currentTarget.nextElementSibling : e.currentTarget.previousElementSibling)?.focus();
}

function Row({ r, columns, last, recent, maxMs, filteredSubject, onOpen, onFilterSubject }) {
  const pal = usePal();
  const [hover, setHover] = useState(false);
  const [focused, setFocused] = useState(false);
  const label = `${Q.run} ${r.run_id}, ${r.state === "completed" ? outcomeLabel(r.outcome) : Q.pending}`;
  const ctx = { hover: hover || focused, maxMs, filtered: filteredSubject === r.subject_id, onFilterSubject };
  return (
    <tr tabIndex={0} data-run={r.run_id} aria-label={label} aria-current={recent || undefined}
      onClick={onOpen} onKeyDown={(e) => onRowKeyDown(e, onOpen)}
      onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{
        cursor: "pointer", outline: "none",
        borderBottom: last ? "none" : `1px solid ${pal.borderSubtle}`,
        background: hover ? pal.bgSubtle : recent ? pal.accentBg : "transparent",
        boxShadow: focused ? `inset 2px 0 0 ${pal.accent}` : "none",
        transition: `background ${motion.normal} ${motion.easeInOut}`,
      }}>
      {columns.map(col => (
        <td key={col.key} style={{ ...tokens.type.sm, color: pal.text, padding: col.key === "open" ? "0 12px 0 0" : "10px 14px", height: 56, overflow: "hidden" }}>
          <Cell col={col.key} r={r} pal={pal} ctx={ctx} />
        </td>
      ))}
    </tr>
  );
}

function LoadingRows({ columns }) {
  return (
    <div aria-busy="true" aria-label={ru.status.loading.description} style={{ padding: "4px 14px" }}>
      {Array.from({ length: 6 }, (_, i) => (
        <div key={i} style={{ display: "grid", gridTemplateColumns: columns.map(c => c.width).join(" "), gap: 14, alignItems: "center", height: 56 }}>
          {columns.map((c, j) => (c.key === "open" ? <span key={c.key} /> : <Skeleton key={c.key} height={j === 0 ? 28 : 12} width={j === 0 ? "80%" : `${50 + ((i + j) % 3) * 15}%`} rounded />))}
        </div>
      ))}
    </div>
  );
}

function Centered(props) {
  return <div style={{ padding: "56px 16px", display: "flex", justifyContent: "center", textAlign: "center" }}><EmptyState {...props} /></div>;
}

export default function QARunsList({ kb, viewport, recentRunId, onOpenRun, onOpenChat, onToast }) {
  const pal = usePal();
  const auth = useAuth();
  const [query, setQuery] = useState(readRunQuery);
  const [draft, setDraft] = useState(() => draftOf(query));
  const [invalid, setInvalid] = useState(null);
  const scope = useMemo(() => ({ workspaceId: kb.workspace, environmentId: kb.environment }), [kb.environment, kb.workspace]);
  const client = useMemo(() => createScopedApiClient({
    baseUrl: window.location.origin,
    getBearerCredential: auth.getBearerCredential ?? (() => ""),
    scope,
  }), [auth.getBearerCredential, scope]);
  const runsQuery = useQuery({
    queryKey: ["spine", "ported-qa-runs", kb.workspace, kb.environment, query],
    queryFn: ({ signal }) => loadQARuns(client, query, signal, scope),
    enabled: Boolean(auth.getBearerCredential),
    retry: false,
  });
  const runs = useMemo(() => (runsQuery.data?.items ?? []).map(r => ({
    ...r,
    duration_ms: r.completed_at ? Math.max(0, Date.parse(r.completed_at) - Date.parse(r.created_at)) : null,
  })), [runsQuery.data?.items]);
  const rows = runs.filter(r => matchesRunQuery(r, query));
  const columns = viewport < 1100 ? COLUMNS.filter(c => !c.wide) : COLUMNS;
  const maxMs = Math.max(1, ...rows.map(r => r.duration_ms ?? 0));
  const hasFilters = Object.values(query).some(Boolean);
  const loading = runsQuery.isPending || runsQuery.isFetching;

  useEffect(() => {
    if (recentRunId) document.querySelector(`[data-run="${recentRunId}"]`)?.scrollIntoView({ block: "nearest" });
  }, [recentRunId]);

  const commit = (next) => {
    setInvalid(null);
    setQuery(next);
    setDraft(draftOf(next));
    writeRunQuery(next);
  };

  const apply = (e) => {
    e.preventDefault();
    const subjectId = draft.subjectId.trim();
    if (subjectId && !isUuid(subjectId)) { setInvalid(Q.invalidInitiator); return; }
    const createdAfter = draft.createdAfter ? dayEdge(draft.createdAfter, false) : undefined;
    const createdBefore = draft.createdBefore ? dayEdge(draft.createdBefore, true) : undefined;
    if (createdAfter && createdBefore && createdAfter > createdBefore) { setInvalid(Q.invalidDateRange); return; }
    commit({ outcome: draft.outcome || undefined, subjectId: subjectId || undefined, createdAfter, createdBefore });
  };

  const open = (r) => {
    if (r.state === "completed" || r.state === "terminal") onOpenRun(r.run_id);
    else onToast("Запуск ещё выполняется: детали появятся после терминального результата");
  };

  let body;
  if (loading) body = <LoadingRows columns={columns} />;
  else if (runsQuery.error) body = (
    <Centered icon={<SearchX size={20} strokeWidth={1.25} />} title={ru.status.failed.title} description={ru.status.failed.description}
      action={<Button size="sm" variant="outline" onClick={() => void runsQuery.refetch()}>{ru.documents.retry}</Button>} />
  );
  else if (runs.length === 0) body = (
    <Centered icon={<MessageSquare size={20} strokeWidth={1.25} />} title={ru.status.empty.title} description={ru.status.empty.description}
      action={<Button size="sm" variant="outline" onClick={onOpenChat}>Открыть чат</Button>} />
  );
  else if (rows.length === 0) body = (
    <Centered icon={<SearchX size={20} strokeWidth={1.25} />} title={ru.status.empty.title} description="Под выбранные фильтры запуски не попадают."
      action={<Button size="sm" variant="outline" onClick={() => commit({})}>{Q.clearFilters}</Button>} />
  );
  else body = (
    <table style={{ width: "100%", minWidth: columns.length > 5 ? 820 : 620, borderCollapse: "collapse", tableLayout: "fixed", fontFamily: tokens.font.sans }}>
      <caption style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}>{Q.tableCaption}</caption>
      <colgroup>{columns.map(c => <col key={c.key} style={{ width: c.width }} />)}</colgroup>
      <thead>
        <tr style={{ borderBottom: `1px solid ${pal.borderSubtle}` }}>
          {columns.map(col => (
            <th key={col.key} scope="col" style={{
              ...tokens.type.xs, fontWeight: tokens.weight.semibold, color: pal.textTertiary, textAlign: "left",
              padding: "10px 14px", textTransform: "uppercase", letterSpacing: "0.05em", whiteSpace: "nowrap",
            }}>{col.label}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <Row key={r.run_id} r={r} columns={columns} last={i === rows.length - 1} recent={r.run_id === recentRunId} maxMs={maxMs}
            filteredSubject={query.subjectId} onOpen={() => open(r)} onFilterSubject={(subjectId) => commit({ ...query, subjectId })} />
        ))}
      </tbody>
    </table>
  );

  return (
    <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", padding: 24 }}>
      <Card padding={0} style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        <form aria-label={Q.filtersLabel} onSubmit={apply} style={{ display: "flex", flexDirection: "column", gap: 14, padding: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6, minHeight: 36 }}>
            <h2 style={{ margin: 0, ...tokens.type.md, fontWeight: tokens.weight.semibold, color: pal.text }}>{Q.title}</h2>
            <Tip text={Q.description}>
              <span tabIndex={0} aria-label={Q.description} style={{ display: "inline-flex", color: pal.textTertiary, cursor: "help", borderRadius: 4 }}>
                <Info size={13} strokeWidth={1.5} />
              </span>
            </Tip>
            <span style={{ flex: 1 }} />
            {hasFilters && <Button type="button" variant="ghost" size="md" onClick={() => commit({})}>{Q.clearFilters}</Button>}
            <Button type="submit" variant="primary" size="md">{Q.applyFilters}</Button>
          </div>
          <Filters draft={draft} setDraft={setDraft} />
          {invalid && <span role="alert" style={{ ...tokens.type.sm, color: pal.danger, marginTop: -4 }}>{invalid}</span>}
        </form>

        <div style={{ flex: 1, minHeight: 0, overflow: "auto", borderTop: `1px solid ${pal.borderSubtle}` }}>{body}</div>

        {!loading && rows.length > 0 && (
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "10px 16px", borderTop: `1px solid ${pal.borderSubtle}` }}>
            <Caption style={{ flex: 1, fontVariantNumeric: "tabular-nums" }}>{ru.documents.pageLimit.replace("{count}", rows.length)}</Caption>
          </div>
        )}
      </Card>
    </div>
  );
}
