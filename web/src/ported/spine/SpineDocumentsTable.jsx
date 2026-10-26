import { useState } from "react";
import { ChevronLeft, ChevronRight, CircleAlert, CircleCheck, FileText, Inbox, MessageSquare, SearchX, Sheet as SheetIcon, Upload } from "lucide-react";
import { Button, Caption, Card, EmptyState, Skeleton, StatusDot, motion, tokens, usePal } from "../halaska-kit";
import { DocumentStateBadge, IdText } from "./SpineParts";
import { ActiveFilterChips, FiltersPopover, PresetTabs } from "./SpineHeader";
import { DOT_STATUS, documentState, formatDate, formatRelative, metadataState, optionLabel, sourceKindLabel } from "./spineData";
import { ru } from "./ru";

const D = ru.documents;
const C = D.columns;
const KIND_ICON = { document: FileText, record: SheetIcon, message: MessageSquare };

const COLUMNS = [
  { key: "source", label: C.type, width: "36%" },
  { key: "state", label: C.state, width: "30%" },
  { key: "access", label: C.access, width: "17%", wide: true },
  { key: "updated", label: C.updated, width: "17%" },
];

const HEALTH_NOTE = { stale: "проекция устарела", degraded: "проекция ограничена" };
const NOTE_TONE = { stale: "warning", degraded: "danger" };

// Publication and projection health folded under the state badge. "Ready" already means published,
// so healthy ready rows get no second line and only rows that need attention carry one.
function stateNote(doc) {
  const p = doc.publication;
  const key = documentState(doc).key;
  if (!p) return key === "uploaded" ? { text: optionLabel("processing_state", doc.operational_metadata?.processing_state) } : null;
  const health = p.health === "degraded" ? "degraded" : p.freshness === "stale" ? "stale" : null;
  const parts = [];
  if (p.status === "published" && key !== "ready") parts.push(optionLabel("publication_status", p.status));
  if (health) parts.push(HEALTH_NOTE[health]);
  if (!parts.length) return null;
  const text = parts.join(" · ");
  return { tone: health ? NOTE_TONE[health] : null, text: text[0].toUpperCase() + text.slice(1) };
}

const CELLS = {
  source: (doc, pal) => {
    const Icon = KIND_ICON[doc.source_kind] ?? FileText;
    const kind = sourceKindLabel(doc.source_kind);
    const revisions = doc.revisions.length;
    return (
      <span style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
        <span role="img" aria-label={kind} title={kind} style={{
          width: 28, height: 28, flexShrink: 0, borderRadius: tokens.radius.sm, background: pal.bgMuted,
          display: "inline-flex", alignItems: "center", justifyContent: "center", color: pal.textSecondary,
        }}><Icon size={14} strokeWidth={1.5} /></span>
        <span style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
          <IdText id={doc.source_object_id} />
          <Caption style={{ ...tokens.type.xs, whiteSpace: "nowrap" }}>
            {kind}{revisions > 1 ? ` · ревизий: ${revisions}` : ""}
          </Caption>
        </span>
      </span>
    );
  },
  state: (doc) => <DocumentStateBadge doc={doc} note={stateNote(doc)} />,
  access: (doc, pal) => {
    const policy = doc.operational_metadata?.access_policy;
    const meta = metadataState(doc);
    return (
      <span style={{ display: "flex", flexDirection: "column", minWidth: 0 }} aria-label={D.accessMetadataTitle}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontFamily: tokens.font.mono, ...tokens.type.xs, color: pal.text }}>
          {meta.key !== "available" && <StatusDot status={DOT_STATUS[meta.tone]} size={6} label={meta.label} />}
          {policy ? `v${policy.version} · gen ${policy.generation}` : D.unknown}
        </span>
        <Caption style={{ ...tokens.type.xs, fontFamily: tokens.font.mono, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {doc.operational_metadata?.security_domain?.reference ?? "—"}
        </Caption>
      </span>
    );
  },
  updated: (doc, pal) => (
    <span title={formatDate(doc.current_revision?.observed_at ?? doc.created_at, true)} style={{ color: pal.textSecondary, whiteSpace: "nowrap" }}>
      {formatRelative(doc.current_revision?.observed_at ?? doc.created_at)}
    </span>
  ),
};

// Arrow keys walk the rows; with the panel open the selection follows focus, like a mail list.
function onRowKeyDown(e, { onSelect, onFocusMove, onClose }) {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(); return; }
  if (e.key === "Escape") { onClose(); return; }
  const step = e.key === "ArrowDown" || e.key === "j" ? 1 : e.key === "ArrowUp" || e.key === "k" ? -1 : 0;
  if (!step) return;
  e.preventDefault();
  const next = step > 0 ? e.currentTarget.nextElementSibling : e.currentTarget.previousElementSibling;
  if (next) { next.focus(); onFocusMove(next.dataset.id); }
}

function Row({ doc, columns, selected, onSelect, onFocusMove, onClose, last }) {
  const pal = usePal();
  const [hover, setHover] = useState(false);
  const [focused, setFocused] = useState(false);
  const cell = { ...tokens.type.sm, color: pal.text, padding: "10px 14px", height: 56, overflow: "hidden" };
  return (
    <tr onClick={onSelect} tabIndex={0} aria-selected={selected} data-id={doc.source_object_id}
      aria-label={`${sourceKindLabel(doc.source_kind)} ${doc.source_object_id}`}
      onKeyDown={(e) => onRowKeyDown(e, { onSelect, onFocusMove, onClose })}
      onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{
        cursor: "pointer", outline: "none",
        borderBottom: last ? "none" : `1px solid ${pal.borderSubtle}`,
        background: selected ? pal.accentBg : hover ? pal.bgSubtle : "transparent",
        boxShadow: focused ? `inset 2px 0 0 ${pal.accent}` : "none",
        transition: `background ${motion.normal} ${motion.easeInOut}`,
      }}>
      {columns.map(col => <td key={col.key} style={cell}>{CELLS[col.key](doc, pal)}</td>)}
    </tr>
  );
}

function LoadingRows({ columns }) {
  return (
    <div role="region" aria-busy="true" aria-label={ru.status.loading.title} style={{ padding: "4px 14px" }}>
      {Array.from({ length: 7 }, (_, i) => (
        <div key={i} style={{ display: "grid", gridTemplateColumns: columns.map(c => c.width).join(" "), gap: 14, alignItems: "center", height: 56 }}>
          {columns.map((c, j) => <Skeleton key={c.key} height={j === 0 ? 28 : 12} width={j === 0 ? "80%" : `${50 + ((i + j) % 3) * 15}%`} rounded />)}
        </div>
      ))}
    </div>
  );
}

function CenteredEmpty(props) {
  const { role = "region", ...emptyProps } = props;
  return (
    <div role={role} aria-label={emptyProps.title} style={{ padding: "56px 16px", display: "flex", justifyContent: "center", textAlign: "center" }}>
      <EmptyState {...emptyProps} />
    </div>
  );
}

export default function SpineDocumentsTable({ kb, compact, onUpload }) {
  const pal = usePal();
  const columns = compact ? COLUMNS.filter(c => !c.wide) : COLUMNS;
  const state = kb.interfaceState;
  const error = ["permissionDenied", "validationFailed", "failed"].includes(state);

  let body;
  if (state === "loading") body = <LoadingRows columns={columns} />;
  else if (error) body = (
    <CenteredEmpty role="alert" icon={<CircleAlert size={20} strokeWidth={1.25} />} title={ru.status[state].title} description={ru.status[state].description}
      action={<Button size="sm" variant="outline" onClick={kb.retry}>{D.retry}</Button>} />
  );
  else if (state === "empty") body = (
    <CenteredEmpty icon={<Inbox size={20} strokeWidth={1.25} />} title={ru.status.empty.title} description={ru.status.empty.description}
      action={<Button size="sm" icon={<Upload size={13} strokeWidth={1.5} />} onClick={onUpload}>{D.upload.title}</Button>} />
  );
  else if (kb.filtered.length === 0) body = kb.hasFilters ? (
    <CenteredEmpty icon={<SearchX size={20} strokeWidth={1.25} />} title={ru.status.empty.title} description="Под выбранные фильтры документы не попадают."
      action={<Button size="sm" variant="outline" onClick={kb.clearFilters}>{D.clearFilters}</Button>} />
  ) : kb.preset === "attention" ? (
    <CenteredEmpty icon={<CircleCheck size={20} strokeWidth={1.25} />} title="Всё в порядке" description="Нет документов, требующих внимания."
      action={<Button size="sm" variant="outline" onClick={() => kb.setPreset("all")}>Показать все</Button>} />
  ) : (
    <CenteredEmpty icon={<Inbox size={20} strokeWidth={1.25} />} title={ru.status.empty.title} description="В этом срезе документов нет."
      action={<Button size="sm" variant="outline" onClick={() => kb.setPreset("all")}>Показать все</Button>} />
  );
  else body = (
    <table style={{ width: "100%", minWidth: compact ? 460 : 640, borderCollapse: "collapse", tableLayout: "fixed", fontFamily: tokens.font.sans }}>
      <caption style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}>{D.tableCaption}</caption>
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
        {kb.pageRows.map((doc, i) => (
          <Row key={doc.source_object_id} doc={doc} columns={columns} last={i === kb.pageRows.length - 1}
            selected={doc.source_object_id === kb.selectedId}
            onSelect={() => kb.setSelectedId(doc.source_object_id === kb.selectedId ? null : doc.source_object_id)}
            onFocusMove={(id) => { if (kb.selectedId) kb.setSelectedId(id); }}
            onClose={() => kb.setSelectedId(null)} />
        ))}
      </tbody>
    </table>
  );

  const dataShown = !error && state !== "loading" && state !== "empty";
  const showFooter = dataShown && kb.filtered.length > 0;

  return (
    <Card padding={0} style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 10, padding: 16 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          {dataShown && <PresetTabs kb={kb} />}
          <div style={{ display: "flex", gap: 8, marginLeft: "auto" }}>
            {dataShown && <FiltersPopover kb={kb} />}
            <Button variant="primary" size="md" icon={<Upload size={14} strokeWidth={1.5} />} onClick={onUpload}>{D.upload.title}</Button>
          </div>
        </div>
        {dataShown && <ActiveFilterChips kb={kb} />}
      </div>

      <div style={{ flex: 1, minHeight: 0, overflow: "auto", borderTop: `1px solid ${pal.borderSubtle}` }}>{body}</div>

      {showFooter && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "10px 16px", borderTop: `1px solid ${pal.borderSubtle}` }}>
          <Caption style={{ flex: 1, fontVariantNumeric: "tabular-nums" }}>
            {kb.cursor + 1}–{kb.cursor + kb.pageRows.length} из {kb.filtered.length}
          </Caption>
          {(kb.cursor > 0 || kb.hasNext) && <>
            <Button variant="outline" size="sm" disabled={kb.cursor === 0} icon={<ChevronLeft size={13} strokeWidth={1.5} />} onClick={kb.prevPage}>Назад</Button>
            <Button variant="outline" size="sm" disabled={!kb.hasNext} onClick={kb.nextPage}>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>Далее<ChevronRight size={13} strokeWidth={1.5} /></span>
            </Button>
          </>}
        </div>
      )}
    </Card>
  );
}
