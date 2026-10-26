import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, ChevronLeft, ChevronsUpDown, Clock, Copy, Info, Link2 } from "lucide-react";
import { IconButton, StatusDot, interactiveBase, tokens, usePal } from "../../halaska-kit";
import { Menu } from "../../components/ui";
import { DOT_STATUS } from "../spineData";
import { ru } from "../ru";
import { createScopedApiClient } from "../../../api/client";
import { useAuth } from "../../../app/auth";
import { loadQARunDetail } from "../../../features/runs/qa-run-api";
import { Tip } from "../chat/SpineChatParts";
import { mono } from "../chat/chatStyles";
import QARunTree from "./QARunTree";
import QARunPanel from "./QARunPanel";
import { OUTCOME_TONE, findNode, formatClock, formatMs, formatStamp, outcomeLabel, runTree, stateLabel } from "./qaRunData";

const Q = ru.qaRuns;
const R = Q.detail;
const compactId = (value) => value ? `${value.slice(0, 8)}…${value.slice(-4)}` : R.unknown;

function MetaField({ label, value, display, onCopy }) {
  const pal = usePal();
  const [done, setDone] = useState(false);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
      <span style={{ ...mono, color: pal.textTertiary }}>{label}</span>
      <button type="button" aria-label={`Скопировать: ${label}`} title={value}
        onClick={() => { onCopy(value, label); setDone(true); setTimeout(() => setDone(false), 1200); }}
        style={{
          ...interactiveBase, display: "inline-flex", alignItems: "center", gap: 8, height: 26, padding: "0 6px 0 8px", maxWidth: "100%", alignSelf: "flex-start",
          borderRadius: tokens.radius.sm, border: `1px solid ${pal.borderSubtle}`, background: pal.bgSubtle, color: pal.text, fontFamily: tokens.font.mono, ...tokens.type.xs,
        }}>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{display ?? value}</span>
        {done ? <Check size={12} strokeWidth={1.75} /> : <Copy size={12} strokeWidth={1.5} color={pal.textTertiary} />}
      </button>
    </div>
  );
}

function RunHeader({ d, runs, onOpenRun, onBack, onCopy }) {
  const pal = usePal();
  const outline = { border: `1px solid ${pal.borderSubtle}`, borderRadius: tokens.radius.sm };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, padding: "14px 16px 12px" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
          <IconButton size={28} label={R.backToRuns} onClick={onBack} icon={<ChevronLeft size={15} strokeWidth={1.5} />} />
          <h2 style={{ margin: 0, ...tokens.type.md, fontWeight: tokens.weight.semibold, color: pal.text, whiteSpace: "nowrap" }}>{R.title}</h2>
          <Tip text={R.description}>
            <span tabIndex={0} aria-label={R.description} style={{ display: "inline-flex", color: pal.textTertiary, cursor: "help", borderRadius: 4 }}>
              <Info size={13} strokeWidth={1.5} />
            </span>
          </Tip>
          <Menu align="left" minWidth={340}
            items={runs.flatMap(({ m, s }, i) => [
              s.session_id !== runs[i - 1]?.s.session_id && { heading: ru.chat.sessionName(s.session_id.slice(-8)) },
              {
                label: outcomeLabel(m.terminal_outcome), meta: `${m.run_id.slice(-8)} · ${formatClock(m.created_at)} · ${formatMs(m.run.duration_ms)}`,
                active: m.run_id === d.run_id, icon: <StatusDot status={DOT_STATUS[OUTCOME_TONE[m.terminal_outcome]]} size={7} />,
                onClick: () => onOpenRun(m.message_id),
              },
            ].filter(Boolean))}
            trigger={(open) => (
              <button type="button" aria-haspopup="menu" aria-expanded={open} aria-label={`${Q.run} ${d.run_id}`} style={{
                ...interactiveBase, display: "inline-flex", alignItems: "center", gap: 6, height: 28, padding: "0 8px", marginLeft: 4, borderRadius: tokens.radius.sm,
                border: `1px solid ${pal.borderSubtle}`, background: open ? pal.bgMuted : "transparent", color: pal.textSecondary, ...tokens.type.sm,
              }}>
                {Q.run} <span style={{ fontFamily: tokens.font.mono, ...tokens.type.xs, color: pal.text }}>{d.run_id.slice(-8)}</span>
                <ChevronsUpDown size={13} strokeWidth={1.5} color={pal.textTertiary} />
              </button>
            )} />
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span title={R.duration} style={{ display: "inline-flex", alignItems: "center", gap: 6, fontFamily: tokens.font.mono, ...tokens.type.xs, color: pal.text, marginRight: 4 }}>
            <Clock size={13} strokeWidth={1.5} color={pal.textTertiary} />{formatMs(d.duration_ms)}
          </span>
          <span style={{
            display: "inline-flex", alignItems: "center", gap: 6, height: 26, padding: "0 10px", borderRadius: tokens.radius.pill,
            border: `1px solid ${pal.borderSubtle}`, ...tokens.type.xs, color: pal.textSecondary, whiteSpace: "nowrap",
          }}>
            <StatusDot status={DOT_STATUS[OUTCOME_TONE[d.outcome]]} size={6} />{stateLabel(d.state)} · {outcomeLabel(d.outcome)}
          </span>
          <IconButton size={28} label="Скопировать ссылку на запуск" onClick={() => onCopy(window.location.href, "Ссылка на запуск")} style={outline} icon={<Link2 size={13} strokeWidth={1.5} />} />
          <IconButton size={28} label="Скопировать метаданные запуска" onClick={() => onCopy(JSON.stringify(d, null, 2), "Метаданные запуска")} style={outline} icon={<Copy size={13} strokeWidth={1.5} />} />
        </div>
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "10px 20px" }}>
        <MetaField label={R.createdAt} value={d.created_at} display={formatStamp(d.created_at)} onCopy={onCopy} />
        <MetaField label={R.traceId} value={d.trace_id} display={compactId(d.trace_id)} onCopy={onCopy} />
        <MetaField label={Q.run} value={d.run_id} display={compactId(d.run_id)} onCopy={onCopy} />
        <MetaField label={R.sessionId} value={d.session_id ?? R.unknown} display={compactId(d.session_id)} onCopy={onCopy} />
        <MetaField label={R.messageId} value={d.message_id ?? R.unknown} display={compactId(d.message_id)} onCopy={onCopy} />
      </div>
    </div>
  );
}

function readSelection() {
  const p = new URLSearchParams(window.location.search);
  return p.get("event_id") ?? p.get("attempt_id");
}

function RunView({ d, chat, client, scope, viewport, onOpenRun, onBack, onBackToMessage, onOpenCitation, onNavigate }) {
  const tree = useMemo(() => runTree(d), [d]);
  const [selected, setSelected] = useState(() => {
    const wanted = readSelection();
    return wanted && findNode(tree, wanted) ? wanted : d.final_attempt_id;
  });
  const node = findNode(tree, selected) ?? tree;
  const stacked = viewport < 1000;

  useEffect(() => {
    const url = new URL(window.location.href);
    url.searchParams.set("run", d.run_id);
    url.searchParams.delete("attempt_id");
    url.searchParams.delete("event_id");
    if (node.type === "attempt") url.searchParams.set("attempt_id", node.id);
    if (node.type === "event") { url.searchParams.set("attempt_id", node.parentId); url.searchParams.set("event_id", node.id); }
    window.history.replaceState(null, "", url);
  }, [d.run_id, node]);

  return (
    <>
      <RunHeader d={d} runs={[]} onOpenRun={onOpenRun} onBack={onBack} onCopy={chat.copy} />
      <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: stacked ? "column" : "row", gap: 8, padding: "0 16px 16px" }}>
        <div style={{ flex: stacked ? "0 0 auto" : "1 1 0", maxHeight: stacked ? "42%" : undefined, minWidth: 0, minHeight: 0, display: "flex" }}>
          <QARunTree tree={tree} others={[]} selectedId={node.id} onSelect={setSelected} onOpenRun={onOpenRun}
            onBackToMessage={() => onBackToMessage(d)} />
        </div>
        <div style={{ flex: "1 1 0", minWidth: 0, minHeight: 0, display: "flex" }}>
          <QARunPanel key={node.type} d={d} node={node} client={client} scope={scope} onSelect={setSelected} onCopy={chat.copy}
            onOpenCitation={(citationId) => onOpenCitation(d, citationId)} onNavigate={onNavigate} />
        </div>
      </div>
    </>
  );
}

function EmptyRuns({ title = ru.status.empty.title, description = ru.status.empty.description, onRetry }) {
  const pal = usePal();
  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 8, textAlign: "center", padding: 48 }}>
      <span style={{ ...mono, color: pal.textTertiary }}>{R.title}</span>
      <span style={{ fontSize: 22, fontWeight: tokens.weight.medium, letterSpacing: "-0.02em", color: pal.text }}>{title}</span>
      <span style={{ ...tokens.type.base, color: pal.textSecondary }}>{description}</span>
      {onRetry && <button type="button" onClick={onRetry}>{ru.documents.retry}</button>}
    </div>
  );
}

export default function QARunScreen({ chat, kb, runId, viewport, onOpenRun, onBack, onBackToMessage, onOpenCitation, onNavigate }) {
  const auth = useAuth();
  const scope = useMemo(() => ({ workspaceId: kb.workspace, environmentId: kb.environment }), [kb.environment, kb.workspace]);
  const client = useMemo(() => createScopedApiClient({
    baseUrl: window.location.origin,
    getBearerCredential: auth.getBearerCredential ?? (() => ""),
    scope,
  }), [auth.getBearerCredential, scope]);
  const detailQuery = useQuery({
    queryKey: ["spine", "ported-qa-run", kb.workspace, kb.environment, runId],
    queryFn: ({ signal }) => loadQARunDetail(client, runId, signal, scope),
    enabled: Boolean(auth.getBearerCredential && runId),
    retry: false,
  });
  const detail = useMemo(() => detailQuery.data ? {
    ...detailQuery.data,
    duration_ms: detailQuery.data.completed_at
      ? Math.max(0, Date.parse(detailQuery.data.completed_at) - Date.parse(detailQuery.data.created_at))
      : 0,
  } : null, [detailQuery.data]);
  return (
    <section aria-label={R.title} style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
      {detail
        ? <RunView key={detail.run_id} d={detail} chat={chat} client={client} scope={scope} viewport={viewport} onOpenRun={onOpenRun} onBack={onBack}
          onBackToMessage={onBackToMessage} onOpenCitation={onOpenCitation} onNavigate={onNavigate} />
        : detailQuery.isPending ? <EmptyRuns title={ru.status.loading.title} description={ru.status.loading.description} />
        : detailQuery.error ? <EmptyRuns title={ru.status.failed.title} description={ru.status.failed.description} onRetry={() => void detailQuery.refetch()} />
        : <EmptyRuns />}
    </section>
  );
}
