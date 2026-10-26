import { useEffect, useMemo, useState } from "react";
import { Activity, ArrowUpRight, ChevronDown, ChevronUp, Play } from "lucide-react";
import { StatusDot, interactiveBase, motion, tokens, usePal } from "../../halaska-kit";
import { Menu } from "../../components/ui";
import { DOT_STATUS } from "../spineData";
import { ru } from "../ru";
import { mono } from "../chat/chatStyles";
import { ATTEMPT_STATE, OUTCOME_TONE, formatClock, formatMs, formatOffset, outcomeLabel } from "./qaRunData";

const R = ru.qaRuns.detail;
const INDENT = 18;
const ROW = 32;
const BAR = 72;

const EVENT_DOT = { started: "offline", context_bound: "offline", completed: "online", failed: "error", cancelled: "offline" };

function NodeMark({ node }) {
  const pal = usePal();
  const tile = { width: 20, height: 20, borderRadius: 5, flexShrink: 0, display: "inline-flex", alignItems: "center", justifyContent: "center" };
  if (node.type === "run") return <span aria-hidden="true" style={{ ...tile, background: pal.text, color: pal.bg }}><Activity size={12} strokeWidth={1.75} /></span>;
  if (node.type === "attempt") {
    return (
      <span aria-hidden="true" style={{ ...tile, background: node.error ? pal.dangerBg : pal.bgMuted, color: node.error ? pal.danger : pal.textSecondary }}>
        <Play size={10} strokeWidth={2} />
      </span>
    );
  }
  return <span aria-hidden="true" style={tile}><StatusDot status={EVENT_DOT[node.event.kind]} size={7} /></span>;
}

function nodeLabel(node) {
  if (node.type === "run") return ru.qaRuns.run;
  if (node.type === "attempt") return `${R.attempt} ${node.attempt.attempt_number}`;
  return node.event.kind;
}

// Where the node sits on the run's clock: a span for the run and attempts, a tick for an instant event.
function TimeBar({ node, total }) {
  const pal = usePal();
  const left = (node.from / total) * 100;
  const color = node.error ? pal.danger : pal.textTertiary;
  return (
    <span aria-hidden="true" style={{ position: "relative", width: BAR, height: 8, flexShrink: 0, borderRadius: 2, background: pal.bgSubtle }}>
      {node.type === "event"
        ? <span style={{ position: "absolute", left: `calc(${left}% - 1px)`, top: 0, bottom: 0, width: 2, borderRadius: 1, background: color }} />
        : <span style={{ position: "absolute", left: `${left}%`, width: `${Math.max(2, (node.ms / total) * 100)}%`, top: 2, bottom: 2, borderRadius: 2, background: color, opacity: node.type === "run" ? 0.45 : 0.8 }} />}
    </span>
  );
}

function visibleRows(root, collapsed) {
  const rows = [];
  const walk = (node, depth) => {
    rows.push({ node, depth });
    if (!collapsed.has(node.id)) node.children.forEach(c => walk(c, depth + 1));
  };
  walk(root, 0);
  return rows.map((r, i) => ({ ...r, guides: Array.from({ length: r.depth }, (_, l) => (rows[i + 1]?.depth ?? 0) > l) }));
}

function TreeRow({ row, total, selected, expanded, focusable, onSelect, onToggle }) {
  const pal = usePal();
  const [hover, setHover] = useState(false);
  const { node, depth, guides } = row;
  const parent = node.children.length > 0;
  const label = nodeLabel(node);
  const value = node.type === "event" ? formatOffset(node.at) : formatMs(node.ms);
  const sub = node.type === "attempt" ? ATTEMPT_STATE[node.attempt.state]
    : node.type === "event" ? node.event.reason
    : null;
  return (
    <div role="treeitem" id={`node-${node.id}`} aria-level={depth + 1} aria-selected={selected} aria-expanded={parent ? expanded : undefined}
      aria-label={`${label}${sub ? `, ${sub}` : ""}, ${value}`} tabIndex={focusable ? 0 : -1}
      onClick={() => onSelect(node.id)} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{
        position: "relative", display: "flex", alignItems: "center", gap: 10, height: ROW, padding: `0 6px 0 ${8 + depth * INDENT}px`,
        borderRadius: tokens.radius.sm, cursor: "pointer", outline: "none",
        background: selected ? pal.bgMuted : hover ? pal.bgSubtle : "transparent",
        transition: `background ${motion.fast} ${motion.easeInOut}`,
      }}>
      {guides.map((through, level) => (through || level === depth - 1) && (
        <span key={level} aria-hidden="true" style={{
          position: "absolute", left: 8 + level * INDENT + 9, top: 0, bottom: through ? 0 : ROW / 2, borderLeft: `1px dotted ${pal.border}`,
        }} />
      ))}
      <NodeMark node={node} />
      <span style={{ flex: 1, minWidth: 0, display: "inline-flex", alignItems: "baseline", gap: 8, whiteSpace: "nowrap", overflow: "hidden" }}>
        <span style={{
          ...tokens.type.sm, fontFamily: node.type === "event" ? tokens.font.mono : tokens.font.sans,
          fontWeight: node.type === "run" ? tokens.weight.medium : tokens.weight.regular,
          color: selected || node.type === "run" ? pal.text : pal.textSecondary,
        }}>{label}</span>
        {node.type === "run" && <span style={{ fontFamily: tokens.font.mono, ...tokens.type.xs, color: pal.textTertiary }}>{node.id.slice(-8)}</span>}
        {sub && (
          <span style={{
            ...tokens.type.xs, fontFamily: node.type === "event" ? tokens.font.mono : tokens.font.sans,
            color: node.error ? pal.danger : pal.textTertiary, overflow: "hidden", textOverflow: "ellipsis",
          }}>{sub}</span>
        )}
      </span>
      <TimeBar node={node} total={total} />
      <span style={{ width: 56, textAlign: "right", fontFamily: tokens.font.mono, ...tokens.type.xs, color: pal.textTertiary, flexShrink: 0 }}>{value}</span>
      {parent ? (
        <button type="button" tabIndex={-1} aria-label={expanded ? `Свернуть: ${label}` : `Развернуть: ${label}`}
          onClick={(e) => { e.stopPropagation(); onToggle(node.id); }}
          style={{
            ...interactiveBase, width: 20, height: 20, flexShrink: 0, display: "inline-flex", alignItems: "center", justifyContent: "center",
            borderRadius: tokens.radius.xs, background: "transparent", color: pal.textSecondary,
          }}>{expanded ? <ChevronUp size={13} strokeWidth={1.75} /> : <ChevronDown size={13} strokeWidth={1.75} />}</button>
      ) : <span style={{ width: 20, flexShrink: 0 }} />}
    </div>
  );
}

export default function QARunTree({ tree, others, selectedId, onSelect, onOpenRun, onBackToMessage }) {
  const pal = usePal();
  const [collapsed, setCollapsed] = useState(() => new Set());
  const rows = useMemo(() => visibleRows(tree, collapsed), [tree, collapsed]);
  const toggle = (id) => setCollapsed(s => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const attempts = tree.children.length;
  const events = tree.children.reduce((n, a) => n + a.children.length, 0);

  useEffect(() => {
    document.getElementById(`node-${selectedId}`)?.scrollIntoView({ block: "nearest" });
  }, [selectedId]);

  const onKeyDown = (e) => {
    const i = rows.findIndex(r => r.node.id === selectedId);
    const row = rows[i];
    if (!row) return;
    const focus = (id) => { onSelect(id); requestAnimationFrame(() => document.getElementById(`node-${id}`)?.focus()); };
    const open = row.node.children.length > 0 && !collapsed.has(row.node.id);
    if (e.key === "ArrowDown" && i < rows.length - 1) { e.preventDefault(); focus(rows[i + 1].node.id); }
    else if (e.key === "ArrowUp" && i > 0) { e.preventDefault(); focus(rows[i - 1].node.id); }
    else if (e.key === "Home") { e.preventDefault(); focus(rows[0].node.id); }
    else if (e.key === "End") { e.preventDefault(); focus(rows[rows.length - 1].node.id); }
    else if (e.key === "ArrowRight" && row.node.children.length) {
      e.preventDefault();
      if (!open) toggle(row.node.id); else focus(rows[i + 1].node.id);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      if (open) toggle(row.node.id); else if (row.node.parentId) focus(row.node.parentId);
    }
  };

  return (
    <section aria-label={R.timeline} style={{
      flex: 1, minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column",
      border: `1px solid ${pal.borderSubtle}`, borderRadius: tokens.radius.sm, background: pal.bg, overflow: "hidden",
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, minHeight: 44, boxSizing: "border-box", padding: "8px 12px", borderBottom: `1px solid ${pal.borderSubtle}` }}>
        <span style={{ ...mono, color: pal.textTertiary }}>{R.timeline}</span>
        <span style={{ ...tokens.type.sm, color: pal.textSecondary }}>{R.attempts}: {attempts} · событий: {events}</span>
      </div>

      <div role="tree" aria-label={R.timeline} onKeyDown={onKeyDown} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: 6 }}>
        {rows.map(r => (
          <TreeRow key={r.node.id} row={r} total={tree.ms} selected={r.node.id === selectedId} expanded={!collapsed.has(r.node.id)}
            focusable={r.node.id === selectedId || (!rows.some(x => x.node.id === selectedId) && r === rows[0])}
            onSelect={onSelect} onToggle={toggle} />
        ))}
      </div>

      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, padding: "6px 8px", borderTop: `1px solid ${pal.borderSubtle}` }}>
        <button type="button" onClick={onBackToMessage} style={{
          ...interactiveBase, ...tokens.type.sm, height: 28, padding: "0 6px", display: "inline-flex", alignItems: "center", gap: 6, borderRadius: tokens.radius.sm, background: "transparent", color: pal.text,
        }}>{R.backToMessage} <ArrowUpRight size={13} strokeWidth={1.5} /></button>
        {others.length > 0 && (
          <Menu align="right" side="top" minWidth={300}
            items={[{ heading: "Другие запуски сессии" }, ...others.map(({ m }) => ({
              label: `${outcomeLabel(m.terminal_outcome)} · ${m.run_id.slice(-8)}`, meta: `${formatClock(m.created_at)} · ${formatMs(m.run.duration_ms)}`,
              icon: <StatusDot status={DOT_STATUS[OUTCOME_TONE[m.terminal_outcome]]} size={7} />, onClick: () => onOpenRun(m.message_id),
            }))]}
            trigger={(open) => (
              <button type="button" aria-haspopup="menu" aria-expanded={open} style={{
                ...interactiveBase, ...tokens.type.sm, height: 28, padding: "0 6px", borderRadius: tokens.radius.sm, background: open ? pal.bgMuted : "transparent", color: pal.textSecondary,
              }}>Ещё запуски сессии: {others.length}</button>
            )} />
        )}
      </div>
    </section>
  );
}
