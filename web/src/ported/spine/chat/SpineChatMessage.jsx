import { useState } from "react";
import {
  ChartNoAxesColumn, Check, ChevronDown, ChevronRight, Copy, CornerDownRight, ExternalLink, Layers, ListChecks, Lock, PanelRight, TriangleAlert,
} from "lucide-react";
import { Button, Spinner, StatusDot, interactiveBase, motion, tokens, usePal } from "../../halaska-kit";
import { DOT_STATUS, idTail, locatorLabel, sourceKindLabel } from "../spineData";
import { ru } from "../ru";
import {
  CLAIM_KIND, OUTCOME, REASONS, STAGE_LABEL, TIMELINE, claimOrder, completenessRows, completenessTone, formatSeconds, formatTime, partText, plainText,
} from "./spineChatData";
import { AgentMark, FeedbackThumbs, IconAction, PanelHeading, Pill, Pop, Rows, Tip } from "./SpineChatParts";
import { mono } from "./chatStyles";

const C = ru.chat;
const R = ru.qaRuns.detail;
const REL = C.relationshipKinds;

const plural = (n, [one, few, many]) => {
  const d = n % 10;
  const h = n % 100;
  return `${n} ${d === 1 && h !== 11 ? one : d >= 2 && d <= 4 && (h < 12 || h > 14) ? few : many}`;
};
const num = (n) => Number(n).toLocaleString("ru-RU");

function UserQuestion({ m }) {
  const pal = usePal();
  const hidden = m.disclosure_eligibility === "ineligible";
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end" }}>
      <div style={{
        maxWidth: "85%", padding: "9px 14px", borderRadius: tokens.radius.md, background: pal.bgMuted,
        ...tokens.type.base, color: hidden ? pal.textTertiary : pal.text, lineHeight: 1.55, whiteSpace: "pre-wrap",
        display: "flex", flexDirection: "column", gap: 4,
      }}>
        {m.clarifies && !hidden && (
          <span style={{ ...mono, display: "inline-flex", alignItems: "center", gap: 4, alignSelf: "flex-start", color: pal.textTertiary }}>
            <CornerDownRight size={11} strokeWidth={2} />{C.clarificationReply}
          </span>
        )}
        {hidden
          ? <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}><Lock size={13} strokeWidth={1.5} />{C.messageContentHidden}</span>
          : m.question}
      </div>
    </div>
  );
}

// ─── Pills: context, completeness and claims (Agno's tools / reasoning / memories) ───

function ContextPanel({ m }) {
  const pal = usePal();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <Rows labelWidth="max-content" rows={[
        [R.evidenceCount, num(m.run.context)],
        [R.retrievalStrategy, m.run.retrieval, { mono: true }],
        [R.projectionSnapshot, m.run.snapshot, { mono: true }],
        [R.freshness, m.run.freshness === "current" ? "Актуальна" : null],
      ]} />
      {m.citations.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", borderTop: `1px solid ${pal.borderSubtle}`, paddingTop: 8, gap: 6 }}>
          <span style={{ ...mono, color: pal.textTertiary }}>В ответе</span>
          {m.citations.map((c, i) => (
            <div key={c.citation_id} style={{ display: "flex", alignItems: "baseline", gap: 10, ...tokens.type.sm }}>
              <span style={{ fontFamily: tokens.font.mono, ...tokens.type.xs, color: pal.textTertiary, width: 16 }}>{i + 1}</span>
              <span style={{ color: pal.text, flexShrink: 0 }}>{locatorLabel(c.locator)}</span>
              <span style={{ color: pal.textTertiary, flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{sourceKindLabel(c.source_kind)}</span>
              <span style={{ fontFamily: tokens.font.mono, ...tokens.type.xs, color: pal.textTertiary }}>…{idTail(c.source_revision_id)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ClaimsPanel({ m }) {
  const pal = usePal();
  const order = claimOrder(m);
  const text = (id) => m.blocks.flatMap(b => (b.type === "ul" ? b.items.flat() : b.parts)).find(p => p.claim === id)?.text;
  return (
    <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column" }}>
      {order.map((id, i) => {
        const claim = m.claims[id];
        return (
          <li key={id} style={{ display: "grid", gridTemplateColumns: "22px minmax(0, 1fr)", gap: 10, padding: "8px 0", borderTop: i ? `1px solid ${pal.borderSubtle}` : "none" }}>
            <span style={{ fontFamily: tokens.font.mono, ...tokens.type.xs, color: pal.textTertiary, paddingTop: 2 }}>{String(i + 1).padStart(2, "0")}</span>
            <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
              <span style={{ ...tokens.type.sm, color: pal.textSecondary }}>{text(id)}</span>
              <span style={{ ...tokens.type.xs, color: pal.textTertiary }}>
                {CLAIM_KIND[claim.kind]} · {claim.rel.map(r => (r.kind === "contradiction"
                  ? C.relatedClaimRelationship(REL.contradiction, order.indexOf(r.related) + 1)
                  : `${REL[r.kind]} ${r.cites.map(ci => `[${ci + 1}]`).join("")}`)).join(" · ")}
              </span>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function RunPills({ m }) {
  const pal = usePal();
  const [panel, setOpen] = useState(null);
  const pills = [
    m.run.context > 0 && { key: "context", icon: Layers, label: `Контекст · ${m.run.context}` },
    Object.keys(m.claims).length > 0 && { key: "claims", icon: ListChecks, label: plural(Object.keys(m.claims).length, ["утверждение", "утверждения", "утверждений"]) },
  ].filter(Boolean);
  if (!pills.length) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {pills.map(({ key, ...pill }) => (
          <Pill
            key={key}
            {...pill}
            open={panel === key}
            onClick={() => setOpen(o => (o === key ? null : key))}
          />
        ))}
      </div>
      {panel && (
        <div style={{
          border: `1px solid ${pal.borderSubtle}`, borderRadius: tokens.radius.sm, background: pal.bgSubtle, padding: "10px 12px",
          animation: `halaska-step-in ${motion.normal} ${motion.emphasized} both`, maxHeight: 260, overflowY: "auto",
        }}>
          {panel === "context" && <ContextPanel m={m} />}
          {panel === "claims" && <ClaimsPanel m={m} />}
        </div>
      )}
    </div>
  );
}

// ─── Status line (Agno's "Worked for N seconds") ───

function runningLabel(m) {
  if (m.cancel === "requesting") return C.cancelRequesting;
  if (m.cancel === "requested") return C.cancelRequested;
  return STAGE_LABEL[m.stage] ?? C.messageRunning;
}

function StatusLine({ m, open, onToggle, observation }) {
  const pal = usePal();
  const working = m.state !== "terminal";
  const checking = m.checking;
  const outcome = OUTCOME[m.terminal_outcome];
  if (working || checking) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <span role="status" style={{ display: "inline-flex", alignItems: "center", gap: 8, height: 24, ...tokens.type.sm, color: pal.textSecondary }}>
          <Spinner size={11} color={pal.textTertiary} />
          {checking ? C.answerChecking : runningLabel(m)}
        </span>
        {working && observation !== "idle" && (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6, ...tokens.type.xs, color: observation === "reconnecting" ? pal.warning : pal.textTertiary, paddingLeft: 19 }}>
            {observation === "reconnecting" ? C.eventStreamReconnecting : C.eventStreamObserving}
          </span>
        )}
      </div>
    );
  }
  return (
    <button type="button" aria-expanded={open} onClick={onToggle} style={{
      ...interactiveBase, display: "inline-flex", alignItems: "center", gap: 8, height: 24, padding: "0 4px", marginLeft: -4, alignSelf: "flex-start",
      borderRadius: tokens.radius.xs, background: "transparent", color: pal.textSecondary, ...tokens.type.sm,
    }}>
      <ChevronDown size={13} strokeWidth={1.75} style={{ transform: open ? "none" : "rotate(-90deg)", transition: `transform ${motion.normal} ${motion.emphasized}` }} />
      <StatusDot status={DOT_STATUS[outcome.tone]} size={6} />
      <span style={{ color: pal.text }}>{outcome.label}</span>
      <span style={{ color: pal.textTertiary }}>· {formatSeconds(m.run.duration_ms)} · {formatTime(m.created_at)}</span>
    </button>
  );
}

function Timeline({ m }) {
  const pal = usePal();
  const events = m.run.timeline.map(e => (e.kind in OUTCOME ? { ...e, kind: m.terminal_outcome, ms: m.run.duration_ms } : e));
  return (
    <ol aria-label={R.timeline} style={{
      listStyle: "none", margin: 0, padding: "2px 0 2px 12px", borderLeft: `1px solid ${pal.borderSubtle}`,
      display: "flex", flexDirection: "column", gap: 6, animation: `halaska-step-in ${motion.normal} ${motion.emphasized} both`,
    }}>
      {events.map((e, i) => (
        <li key={i} style={{ display: "grid", gridTemplateColumns: "56px minmax(0, 1fr)", gap: 8, ...tokens.type.sm, color: pal.textTertiary }}>
          <span style={{ fontFamily: tokens.font.mono, ...tokens.type.xs, paddingTop: 2 }}>+{formatSeconds(e.ms)}</span>
          <span>
            {TIMELINE[e.kind]}
            {e.kind === "context_retrieval" && e.count > 0 && ` · ${plural(e.count, ["фрагмент", "фрагмента", "фрагментов"])}`}
            {e.kind === "validation" && m.completeness && ` · ${C.completeness.validationDecisions[m.completeness.validation_decision]}`}
          </span>
        </li>
      ))}
    </ol>
  );
}

// ─── Accepted answer with claim markers ───

function Marker({ number, relation, active, onClick }) {
  const pal = usePal();
  const [hover, setHover] = useState(false);
  return (
    <Tip text={`${relation} · ${C.citationEvidenceTitle(number)}`}>
      <button type="button" aria-label={C.citationMarkerLabel(number, relation)} aria-pressed={active} onClick={onClick}
        onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
        style={{
          ...interactiveBase, display: "inline-flex", alignItems: "center", justifyContent: "center", minWidth: 16, height: 16, padding: "0 4px",
          marginLeft: 3, borderRadius: 4, position: "relative", top: -2, boxSizing: "border-box",
          fontFamily: tokens.font.mono, fontSize: 10, fontWeight: tokens.weight.semibold, lineHeight: 1,
          background: active ? pal.text : hover ? pal.border : pal.bgMuted, color: active ? pal.bg : pal.text,
        }}>{number}</button>
    </Tip>
  );
}

function Contradiction({ claimNumber }) {
  const pal = usePal();
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4, marginLeft: 6, padding: "0 6px", height: 18, borderRadius: 4, position: "relative", top: -1,
      background: pal.warningBg, color: pal.text, ...tokens.type.xs, whiteSpace: "nowrap",
    }}>
      <TriangleAlert size={11} strokeWidth={2} color={pal.warning} />
      {C.relatedClaimRelationship(REL.contradiction, claimNumber)}
    </span>
  );
}

// Each part gets its offset in the streamed text, so a partial reveal can cut it mid-sentence.
function layout(blocks) {
  let offset = 0;
  const place = (parts) => parts.map(p => {
    const start = offset;
    offset += partText(p).length;
    return { p, start };
  });
  return blocks.map(b => (b.type === "ul"
    ? { type: "ul", items: b.items.map(place) }
    : { type: b.type, parts: place(b.parts) }));
}

const firstStart = (b) => (b.type === "ul" ? b.items[0][0].start : b.parts[0].start);

function Answer({ m, selectedCitation, onCite }) {
  const pal = usePal();
  const order = claimOrder(m);
  const settled = m.state === "terminal" && !m.checking;
  const streaming = m.stage === "streaming";

  const caret = <span aria-hidden="true" style={{ display: "inline-block", width: 2, height: "1em", background: pal.text, marginLeft: 2, verticalAlign: "text-bottom", animation: "halaska-blink 1s step-end infinite" }} />;

  const renderParts = (parts) => parts.map(({ p, start }, i) => {
    const text = partText(p);
    if (start >= m.revealed) return null;
    const shown = text.slice(0, m.revealed - start);
    const atCaret = streaming && start + text.length >= m.revealed;
    if (typeof p === "string") return <span key={i}>{shown}{atCaret && caret}</span>;
    const claim = m.claims[p.claim];
    const lit = selectedCitation && claim.rel.some(r => r.cites?.some(ci => m.citations[ci].citation_id === selectedCitation));
    return (
      <span key={i} data-claim-id={p.claim} data-start-utf16={start} data-end-utf16={start + text.length}>
        <span style={lit ? { background: pal.bgMuted, boxShadow: `0 0 0 2px ${pal.bgMuted}`, borderRadius: 2, color: pal.text } : undefined}>{shown}</span>
        {atCaret && caret}
        {settled && shown.length === text.length && claim.rel.map((r, ri) => (r.kind === "contradiction"
          ? <Contradiction key={ri} claimNumber={order.indexOf(r.related) + 1} />
          : r.cites.map(ci => (
            <Marker key={`${ri}-${ci}`} number={ci + 1} relation={REL[r.kind]} active={m.citations[ci].citation_id === selectedCitation}
              onClick={() => onCite(m.citations[ci].citation_id)} />
          ))))}
      </span>
    );
  });

  const para = { margin: 0, ...tokens.type.md, lineHeight: 1.7, color: pal.textSecondary };
  return layout(m.blocks).map((b, i) => {
    if (firstStart(b) >= m.revealed) return null;
    if (b.type === "ul") {
      return (
        <ul key={i} style={{ ...para, paddingLeft: 22, display: "flex", flexDirection: "column", gap: 4 }}>
          {b.items.map((parts, j) => (parts[0].start < m.revealed ? <li key={j}>{renderParts(parts)}</li> : null))}
        </ul>
      );
    }
    return <p key={i} style={para}>{renderParts(b.parts)}</p>;
  });
}

function CompletenessNote({ c }) {
  const pal = usePal();
  const [open, setOpen] = useState(false);
  if (completenessTone(c) === "online") return null;
  const K = C.completeness;
  const text = c.conflict_status === "presented"
    ? `${K.conflict}: ${K.conflictStates[c.conflict_status]}`
    : `${K.partiality[c.partiality_status]} · ${K.validationDecisions[c.validation_decision]}`;
  return (
    <div style={{ borderRadius: tokens.radius.sm, background: pal.warningBg }}>
      <button type="button" aria-expanded={open} onClick={() => setOpen(o => !o)} style={{
        ...interactiveBase, display: "flex", gap: 8, alignItems: "flex-start", width: "100%", padding: "8px 12px", borderRadius: tokens.radius.sm,
        background: "transparent", textAlign: "left",
      }}>
        <TriangleAlert size={14} strokeWidth={1.75} color={pal.warning} style={{ flexShrink: 0, marginTop: 2 }} />
        <span style={{ flex: 1, ...tokens.type.sm, color: pal.text, lineHeight: 1.5 }}>{text}</span>
        <ChevronDown size={14} strokeWidth={1.75} color={pal.textSecondary} style={{ flexShrink: 0, marginTop: 2, transform: open ? "rotate(180deg)" : "none", transition: `transform ${motion.normal} ${motion.emphasized}` }} />
      </button>
      {open && (
        <div style={{ padding: "2px 12px 10px 34px", animation: `halaska-step-in ${motion.normal} ${motion.emphasized} both` }}>
          <Rows labelWidth="max-content" align="left" rows={completenessRows(c)} />
        </div>
      )}
    </div>
  );
}

// ─── Sources ───

function SourceChip({ c, number, active, onClick }) {
  const pal = usePal();
  const [hover, setHover] = useState(false);
  return (
    <button type="button" onClick={onClick} aria-pressed={active} title={c.excerpt}
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{
        ...interactiveBase, display: "inline-flex", alignItems: "center", gap: 8, height: 30, padding: "0 10px 0 6px", borderRadius: tokens.radius.sm,
        background: active || hover ? pal.bgMuted : pal.bgSubtle, border: `1px solid ${active ? pal.border : "transparent"}`, maxWidth: 280, minWidth: 0,
      }}>
      <span aria-hidden="true" style={{
        minWidth: 18, height: 18, borderRadius: 4, background: active ? pal.text : pal.bgMuted, color: active ? pal.bg : pal.text, flexShrink: 0,
        display: "inline-flex", alignItems: "center", justifyContent: "center", fontSize: 10, fontWeight: 700, fontFamily: tokens.font.mono,
      }}>{number}</span>
      <span style={{ ...tokens.type.sm, color: pal.text, whiteSpace: "nowrap" }}>{locatorLabel(c.locator)}</span>
      <span style={{ ...tokens.type.sm, color: pal.textTertiary, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", minWidth: 0 }}>
        {sourceKindLabel(c.source_kind)} · <span style={{ fontFamily: tokens.font.mono, ...tokens.type.xs }}>…{idTail(c.source_object_id).slice(-6)}</span>
      </span>
    </button>
  );
}

function Sources({ m, selectedCitation, onCite }) {
  const pal = usePal();
  const shown = m.citations.slice(0, 3);
  const rest = m.citations.slice(3);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <span style={{ ...tokens.type.sm, fontWeight: tokens.weight.medium, color: pal.text }}>Источники</span>
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        {shown.map((c, i) => <SourceChip key={c.citation_id} c={c} number={i + 1} active={c.citation_id === selectedCitation} onClick={() => onCite(c.citation_id)} />)}
        {rest.length > 0 && (
          <Pop trigger={
            <button type="button" aria-haspopup="dialog" style={{ ...interactiveBase, ...mono, display: "inline-flex", alignItems: "center", gap: 6, height: 30, padding: "0 8px", borderRadius: tokens.radius.sm, background: "transparent", color: pal.textSecondary }}>
              +{rest.length} ещё <ChevronRight size={12} strokeWidth={1.75} />
            </button>
          }>
            {(close) => rest.map((c, i) => (
              <button key={c.citation_id} type="button" onClick={() => { onCite(c.citation_id); close(); }} style={{ ...interactiveBase, display: "flex", width: "100%", gap: 8, padding: "6px 8px", borderRadius: tokens.radius.sm, background: "transparent", textAlign: "left", ...tokens.type.sm, color: pal.text }}>
                <span style={{ fontFamily: tokens.font.mono, color: pal.textTertiary }}>{i + 4}</span>{locatorLabel(c.locator)}
              </button>
            ))}
          </Pop>
        )}
      </div>
    </div>
  );
}

// ─── Actions ───

function RunMetrics({ m }) {
  const u = m.run.usage;
  return (
    <>
      <PanelHeading>{R.usage}</PanelHeading>
      <Rows rows={[
        [R.duration, formatSeconds(m.run.duration_ms), { mono: true }],
        [R.invocations, num(u.model_invocations), { mono: true }],
        [R.inputTokens, num(u.input_tokens), { mono: true }],
        [R.outputTokens, num(u.output_tokens), { mono: true }],
        [R.cachedInputTokens, num(u.cached_input_tokens), { mono: true }],
        [R.cost, `${u.cost} ${u.currency}`, { mono: true }],
      ]} />
    </>
  );
}

function Actions({ m, chat, onOpenRun }) {
  const [copied, setCopied] = useState(false);
  const answered = m.terminal_outcome === "answered" && m.disclosure_eligibility === "eligible";
  const copy = () => {
    chat.copy(plainText(m), "Ответ");
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
  };
  const key = `answer:${m.answer_id}`;
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 2, marginLeft: -5 }}>
        {answered && <IconAction label={copied ? "Скопировано" : "Копировать ответ"} onClick={copy}>{copied ? <Check size={14} strokeWidth={1.75} /> : <Copy size={14} strokeWidth={1.5} />}</IconAction>}
        <IconAction label={C.inspectRunSummary} active={chat.inspector?.mode === "run" && chat.inspector.messageId === m.message_id} onClick={() => chat.openRun(m.message_id)}>
          <PanelRight size={14} strokeWidth={1.5} />
        </IconAction>
        {answered && (
          <Pop width={290} trigger={<IconAction label="Метрики запуска"><ChartNoAxesColumn size={14} strokeWidth={1.5} /></IconAction>}>
            <RunMetrics m={m} />
          </Pop>
        )}
        <IconAction label={C.openRun} onClick={() => onOpenRun(m.message_id)}><ExternalLink size={14} strokeWidth={1.5} /></IconAction>
      </div>
      {answered && <FeedbackThumbs entry={chat.feedback[key]} label={C.feedbackAnswerLabel} onSubmit={(outcome) => chat.submitFeedback(key, outcome)} />}
    </div>
  );
}

// ─── Outcomes without an accepted answer ───

function Outcome({ m, chat }) {
  const pal = usePal();
  if (m.terminal_outcome === "clarification_required") {
    const active = chat.clarifyTarget === m.message_id;
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12, alignItems: "flex-start" }}>
        <p style={{ margin: 0, ...tokens.type.md, lineHeight: 1.7, color: pal.textSecondary }}>{m.clarification_prompt}</p>
        <Button size="sm" variant={active ? "secondary" : "outline"} icon={<CornerDownRight size={13} strokeWidth={1.75} />}
          onClick={() => chat.setClarifyTarget(active ? null : m.message_id)}>
          {active ? C.cancelClarification : C.answerClarification}
        </Button>
      </div>
    );
  }
  const reason = m.terminal_outcome === "cancelled" ? REASONS.request_cancelled : REASONS[m.reason];
  if (!reason) return null;
  return <p style={{ margin: 0, ...tokens.type.md, lineHeight: 1.7, color: pal.textSecondary }}>{reason}</p>;
}

function HiddenNote() {
  const pal = usePal();
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8, ...tokens.type.sm, color: pal.textTertiary }}>
      <Lock size={13} strokeWidth={1.5} />{C.messageContentHidden}
    </span>
  );
}

export default function SpineChatMessage({ m, chat, onOpenRun }) {
  const [timeline, setTimeline] = useState(false);
  const hidden = m.disclosure_eligibility === "ineligible";
  const terminal = m.state === "terminal" && !m.checking;
  const answered = m.terminal_outcome === "answered" && !hidden;
  const hasRunPills = m.run.context > 0 || Object.keys(m.claims).length > 0;
  const showAnswer = !hidden && (answered || (m.state !== "terminal" && m.revealed > 0));
  const selectedCitation = chat.inspector?.mode === "citation" && chat.inspector.messageId === m.message_id ? chat.inspector.citationId : null;
  const cite = (citationId) => chat.openCitation(m.message_id, citationId);
  return (
    <article id={`msg-${m.message_id}`} data-msg={m.message_id} aria-label={`${C.inputLabel} ${m.ordinal}`} style={{ display: "flex", flexDirection: "column", gap: 18, scrollMarginTop: 24 }}>
      <UserQuestion m={m} />
      <div style={{ display: "grid", gridTemplateColumns: "22px minmax(0, 1fr)", columnGap: 10, rowGap: 12 }}>
        {answered && terminal && hasRunPills && <><span /><RunPills m={m} /></>}
        <span style={{ alignSelf: "start", marginTop: 1 }}><AgentMark /></span>
        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <StatusLine m={m} open={timeline} onToggle={() => setTimeline(t => !t)} observation={chat.observation} />
            {timeline && terminal && <Timeline m={m} />}
          </div>
          {answered && terminal && m.completeness && <CompletenessNote c={m.completeness} />}
          {showAnswer && <div data-accepted-answer-text style={{ display: "contents" }}><Answer m={m} selectedCitation={selectedCitation} onCite={cite} /></div>}
          {terminal && !hidden && <Outcome m={m} chat={chat} />}
          {terminal && hidden && <HiddenNote />}
          {answered && terminal && m.citations.length > 0 && <Sources m={m} selectedCitation={selectedCitation} onCite={cite} />}
          {terminal && <Actions m={m} chat={chat} onOpenRun={onOpenRun} />}
        </div>
      </div>
    </article>
  );
}
