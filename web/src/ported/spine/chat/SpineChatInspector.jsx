import { useEffect, useRef, useState } from "react";
import { useLocation } from "react-router";
import { ArrowUpRight, ExternalLink, X } from "lucide-react";
import { Button, IconButton, Spinner, StatusDot, interactiveBase, motion, tokens, usePal } from "../../halaska-kit";
import { caps } from "../../components/ui";
import { IdText } from "../SpineParts";
import { DOT_STATUS, locatorLabel, sourceKindLabel } from "../spineData";
import { ru } from "../ru";
import { OUTCOME, completenessRows, formatSeconds, formatTime, messageStateLabel } from "./spineChatData";
import { mono } from "./chatStyles";

const C = ru.chat;
const R = ru.qaRuns.detail;
const PREVIEW = 6;

function Section({ title, children }) {
  const pal = usePal();
  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <span style={{ ...mono, color: pal.textTertiary }}>{title}</span>
      {children}
    </section>
  );
}

function Pairs({ rows }) {
  const pal = usePal();
  return (
    <dl style={{ margin: 0, display: "flex", flexDirection: "column", gap: 10 }}>
      {rows.filter(Boolean).map(([k, v]) => (
        <div key={k} style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
          <dt style={{ ...tokens.type.xs, color: pal.textTertiary }}>{k}</dt>
          <dd style={{ margin: 0, minWidth: 0, ...tokens.type.sm, color: pal.text }}>{v ?? <span style={{ color: pal.textTertiary }}>{ru.qaRuns.unknown}</span>}</dd>
        </div>
      ))}
    </dl>
  );
}

function CitationFeedback({ chat, citationId }) {
  const pal = usePal();
  const [comment, setComment] = useState("");
  const key = `citation:${citationId}`;
  const entry = chat.feedback[key];
  return (
    <fieldset disabled={!!entry} style={{ margin: 0, padding: 0, border: "none", display: "flex", flexDirection: "column", gap: 8 }}>
      <legend style={{ ...mono, color: pal.textTertiary, padding: 0, marginBottom: 10 }}>{C.feedbackCitationLabel}</legend>
      {!entry && (
        <textarea value={comment} maxLength={1000} rows={2} aria-label={C.feedbackCommentLabel} placeholder={C.feedbackCommentLabel}
          onChange={(e) => setComment(e.target.value)} style={{
            resize: "vertical", padding: "8px 10px", borderRadius: tokens.radius.sm, border: `1px solid ${pal.borderSubtle}`,
            background: pal.bg, color: pal.text, fontFamily: tokens.font.sans, ...tokens.type.sm, outline: "none",
          }} />
      )}
      <div style={{ display: "flex", gap: 6 }}>
        {["helpful", "incorrect"].map(outcome => {
          const chosen = entry?.outcome === outcome;
          return (
            <Button key={outcome} size="sm" variant={chosen ? "primary" : "outline"} loading={chosen && entry.status === "submitting"}
              disabled={!!entry && !chosen} onClick={() => chat.submitFeedback(key, outcome)}>
              {outcome === "helpful" ? C.feedbackHelpful : C.feedbackIncorrect}
            </Button>
          );
        })}
      </div>
      <span role="status" style={{ ...tokens.type.xs, color: pal.textTertiary, lineHeight: 1.45 }}>
        {entry?.status === "recorded" ? C.feedbackRecorded : entry ? C.feedbackSubmitting : C.feedbackCommentHint}
      </span>
    </fieldset>
  );
}

function CitationBody({ chat, message, citationId, onOpenDocument }) {
  const pal = usePal();
  const index = message.citations.findIndex(c => c.citation_id === citationId);
  const c = message.citations[index];
  const relations = Object.values(message.claims).flatMap(cl => cl.rel).filter(r => r.cites?.includes(index)).map(r => C.relationshipKinds[r.kind]);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {[...new Set(relations)].map(r => (
          <span key={r} style={{ ...tokens.type.xs, padding: "2px 8px", borderRadius: tokens.radius.pill, background: pal.bgMuted, color: pal.textSecondary }}>{r}</span>
        ))}
      </div>
      <blockquote style={{
        margin: 0, padding: "12px 14px", borderRadius: tokens.radius.sm, background: pal.bgSubtle, borderLeft: `2px solid ${pal.text}`,
        ...tokens.type.base, lineHeight: 1.6, color: pal.text,
      }}>{c.excerpt}</blockquote>
      <Pairs rows={[
        [C.citationSourceLocation, locatorLabel(c.locator)],
        [sourceKindLabel(c.source_kind), <IdText key="s" id={c.source_object_id} />],
        [`${C.citationSourceRevision} · ${c.revision_number}`, <IdText key="r" id={c.source_revision_id} />],
      ]} />
      <Button variant="outline" size="sm" icon={<ArrowUpRight size={13} strokeWidth={1.75} />} onClick={() => onOpenDocument(c.source_object_id)} style={{ alignSelf: "flex-start" }}>
        Открыть в знаниях
      </Button>
      <div style={{ height: 1, background: pal.borderSubtle }} />
      <CitationFeedback key={citationId} chat={chat} citationId={citationId} />
    </div>
  );
}

function RunBody({ message, onOpenRun }) {
  const pal = usePal();
  const outcome = OUTCOME[message.terminal_outcome];
  const u = message.run.usage;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
      <div style={{ display: "grid", gridTemplateColumns: "11px minmax(0, 1fr)", columnGap: 8, rowGap: 2, alignItems: "center" }}>
        <StatusDot status={DOT_STATUS[outcome?.tone ?? "pending"]} size={7} />
        <span style={{ ...tokens.type.sm, fontWeight: tokens.weight.medium, color: pal.text }}>{messageStateLabel(message)}</span>
        <span />
        <span style={{ ...tokens.type.xs, color: pal.textTertiary }}>{formatSeconds(message.run.duration_ms)} · {formatTime(message.created_at)}</span>
      </div>
      <Section title={R.runIdentity}>
        <Pairs rows={[
          [R.messageId, <IdText key="m" id={message.message_id} />],
          [ru.qaRuns.run, <IdText key="r" id={message.run_id} />],
          [`${R.attempt} ${message.run.attempt_number}`, <IdText key="a" id={message.run.attempt_id} />],
          [R.traceId, <span key="t" style={{ fontFamily: tokens.font.mono, ...tokens.type.xs, overflowWrap: "anywhere" }}>{message.trace_id}</span>],
          [C.messageVersion, message.version],
        ]} />
      </Section>
      {message.completeness && (
        <Section title={C.completeness.title}>
          <strong style={{ ...tokens.type.sm, fontWeight: tokens.weight.semibold, color: pal.text }}>{C.completeness.partiality[message.completeness.partiality_status]}</strong>
          <Pairs rows={completenessRows(message.completeness)} />
        </Section>
      )}
      <Section title={R.versions}>
        <Pairs rows={[
          [R.agentVersion, <code key="a" style={{ fontFamily: tokens.font.mono, ...tokens.type.xs }}>{message.run.versions.agent}</code>],
          [R.promptVersion, <code key="p" style={{ fontFamily: tokens.font.mono, ...tokens.type.xs }}>{message.run.versions.prompt}</code>],
          [R.modelVersion, <code key="m" style={{ fontFamily: tokens.font.mono, ...tokens.type.xs }}>{message.run.versions.model}</code>],
          message.run.context > 0 && [R.projectionSnapshot, <IdText key="s" id={message.run.snapshot} />],
          message.run.context > 0 && [R.evidenceCount, message.run.context],
        ]} />
      </Section>
      <Section title={R.usage}>
        <Pairs rows={[
          [R.invocations, u.model_invocations],
          [`${R.inputTokens} / ${R.outputTokens.toLowerCase()}`, `${u.input_tokens.toLocaleString("ru-RU")} / ${u.output_tokens.toLocaleString("ru-RU")}`],
          [R.cost, `${u.cost} ${u.currency}`],
        ]} />
      </Section>
      <Button variant="outline" size="sm" icon={<ExternalLink size={13} strokeWidth={1.75} />} onClick={() => onOpenRun(message.message_id)} style={{ alignSelf: "flex-start" }}>{C.openRun}</Button>
    </div>
  );
}

export function InspectorPanel({ chat, onOpenDocument, onOpenRun }) {
  const pal = usePal();
  const location = useLocation();
  const closeRef = useRef(null);
  const returnFocusRef = useRef(null);
  const { inspector } = chat;
  const message = chat.thread.find(m => m.message_id === inspector.messageId);
  useEffect(() => {
    returnFocusRef.current = document.activeElement;
    closeRef.current?.querySelector("button")?.focus({ preventScroll: true });
    return () => returnFocusRef.current?.focus?.({ preventScroll: true });
  }, []);
  if (!message) return null;
  const number = message.citations.findIndex(c => c.citation_id === inspector.citationId) + 1;
  return (
    <div role="dialog" aria-modal="false" aria-label={inspector.mode === "citation" ? C.citationInspectorTitle : C.runInspectorTitle}
      onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); chat.closeInspector(); } }}
      style={{ display: "flex", flexDirection: "column", minHeight: 0, height: "100%", animation: `halaska-fade-in ${motion.normal} ${motion.easeOut} both` }}>
      <header style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 8, padding: "14px 16px 12px 20px", borderBottom: `1px solid ${pal.borderSubtle}` }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
          <span style={{ ...mono, color: pal.textTertiary }}>{inspector.mode === "citation" ? C.citationInspectorTitle : C.inspectorTitle}</span>
          <span style={{ ...tokens.type.md, fontWeight: tokens.weight.semibold, color: pal.text }}>
            {inspector.mode === "citation" ? C.citationEvidenceTitle(number) : C.runInspectorTitle}
          </span>
        </div>
        <span ref={closeRef} style={{ display: "inline-flex" }}>
          <IconButton size={28} label={C.closeInspector} onClick={chat.closeInspector} icon={<X size={14} strokeWidth={1.5} />} />
        </span>
      </header>
      <div style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "18px 20px 24px" }}>
        {inspector.mode === "citation" && inspector.checking && (
          <span role="status" style={{ display: "inline-flex", alignItems: "center", gap: 8, ...tokens.type.sm, color: pal.textSecondary, lineHeight: 1.5 }}>
            <Spinner size={11} color={pal.textTertiary} />{C.citationInspectorChecking}
          </span>
        )}
        {inspector.mode === "citation" && !inspector.checking && <CitationBody chat={chat} message={message} citationId={inspector.citationId} onOpenDocument={onOpenDocument} />}
        {inspector.mode === "citation" && new URLSearchParams(location.search).has("return_run_id") && (
          <Button variant="outline" size="sm" icon={<ExternalLink size={13} strokeWidth={1.75} />}
            onClick={() => onOpenRun(message.message_id)} style={{ alignSelf: "flex-start", marginTop: 16 }}>
            Вернуться к запуску Q&A
          </Button>
        )}
        {inspector.mode === "run" && <RunBody message={message} onOpenRun={onOpenRun} />}
      </div>
    </div>
  );
}

// Agno's runs rail: the session's questions, with the one in view highlighted.
export function QuestionsOutline({ messages, activeId, onJump }) {
  const pal = usePal();
  const [all, setAll] = useState(false);
  if (!messages.length) return null;
  const shown = all ? messages : messages.slice(-PREVIEW);
  return (
    <nav aria-label={C.messagesTitle} style={{ display: "flex", flexDirection: "column", animation: `halaska-fade-in ${motion.normal} ${motion.easeOut} both` }}>
      <span style={{ ...mono, color: pal.textTertiary, padding: "0 0 10px 13px" }}>{C.messagesTitle}</span>
      {shown.map(m => {
        const active = m.message_id === activeId;
        const live = m.state !== "terminal";
        const hidden = m.disclosure_eligibility === "ineligible";
        return (
          <button key={m.message_id} type="button" onClick={() => onJump(m.message_id)} aria-current={active ? "true" : undefined} title={hidden ? C.messageContentHidden : m.question}
            style={{
              ...interactiveBase, display: "flex", alignItems: "center", gap: 6, textAlign: "left", padding: "5px 0 5px 12px", background: "transparent",
              borderLeft: `1px solid ${active ? pal.text : pal.border}`, ...tokens.type.sm, color: active ? pal.text : pal.textTertiary,
              transition: `color ${motion.normal} ${motion.easeInOut}, border-color ${motion.normal} ${motion.easeInOut}`,
            }}>
            {live && <StatusDot status="busy" pulse size={6} />}
            <span style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", fontStyle: hidden ? "italic" : "normal" }}>{hidden ? "Скрыто" : m.question}</span>
          </button>
        );
      })}
      {messages.length > PREVIEW && (
        <button type="button" onClick={() => setAll(a => !a)} style={{
          ...interactiveBase, ...caps, ...tokens.type.xs, fontFamily: tokens.font.mono, textAlign: "left", background: "transparent", color: pal.textSecondary, padding: "10px 0 0 13px",
        }}>{all ? "Только последние" : `Все вопросы · ${messages.length}`}</button>
      )}
    </nav>
  );
}
