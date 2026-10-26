import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, Check, Copy, Lock, TriangleAlert } from "lucide-react";
import { Button, IconButton, Spinner, StatusDot, interactiveBase, motion, tokens, usePal } from "../../halaska-kit";
import { IdText } from "../SpineParts";
import { DOT_STATUS, locatorLabel, sourceKindLabel } from "../spineData";
import { ru } from "../ru";
import { loadOperationalCorrelation } from "../../../features/diagnostics/operational-correlation";
import { Rows } from "../chat/SpineChatParts";
import { mono } from "../chat/chatStyles";
import { completenessRows } from "../chat/spineChatData";
import {
  ATTEMPT_STATE, EVENT_GLOSS, OUTCOME_TONE, REASON_GLOSS, formatMs, formatOffset, formatStamp, outcomeLabel, shortRef, stateLabel,
} from "./qaRunData";

const Q = ru.qaRuns;
const R = Q.detail;
const D = ru.diagnostics;
const K = ru.chat.completeness;

const TABS = {
  run: [["identity", "Идентичность"], ["evidence", "Evidence"], ["diagnostics", "Диагностика"]],
  attempt: [["summary", "Сведения"], ["versions", R.versions], ["usage", R.usage]],
};
const LABEL_COL = "minmax(140px, 38%)";

const Code = ({ children }) => <span style={{ fontFamily: tokens.font.mono, ...tokens.type.xs }}>{children}</span>;

function CopyIcon({ text, label, onCopy }) {
  const [done, setDone] = useState(false);
  return (
    <IconButton size={24} label={done ? "Скопировано" : label} onClick={() => { onCopy(text, label); setDone(true); setTimeout(() => setDone(false), 1200); }}
      icon={done ? <Check size={12} strokeWidth={1.75} /> : <Copy size={12} strokeWidth={1.5} />} />
  );
}

// An id that reads as head…tail and copies on demand, the way ids behave everywhere in Spine.
function Ident({ id, label, onCopy }) {
  if (!id) return <span>{R.unknown}</span>;
  return (
    <span style={{ display: "flex", alignItems: "center", gap: 4, minWidth: 0, margin: "-3px 0" }}>
      <span style={{ flex: 1, minWidth: 0 }}><IdText id={id} /></span>
      <CopyIcon text={id} label={`Скопировать: ${label}`} onCopy={onCopy} />
    </span>
  );
}

const Coded = ({ code, gloss }) => {
  const pal = usePal();
  return (
    <span style={{ display: "inline-flex", flexWrap: "wrap", alignItems: "baseline", columnGap: 8 }}>
      <Code>{code}</Code>
      {gloss && <span style={{ ...tokens.type.xs, color: pal.textTertiary }}>{gloss}</span>}
    </span>
  );
};

function Block({ title, description, children }) {
  const pal = usePal();
  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <h3 style={{ margin: 0, ...mono, fontWeight: tokens.weight.regular, color: pal.textTertiary }}>{title}</h3>
        {description && <p style={{ margin: 0, ...tokens.type.sm, color: pal.textSecondary, lineHeight: 1.5 }}>{description}</p>}
      </div>
      {children}
    </section>
  );
}

function Note({ icon: Icon, tone, title, children }) {
  const pal = usePal();
  const color = tone === "warning" ? pal.warning : pal.textTertiary;
  return (
    <div role="note" style={{
      display: "flex", gap: 10, padding: "10px 12px", borderRadius: tokens.radius.sm,
      background: tone === "warning" ? pal.warningBg : pal.bgSubtle, border: `1px solid ${pal.borderSubtle}`,
    }}>
      <Icon size={14} strokeWidth={1.75} color={color} style={{ flexShrink: 0, marginTop: 2 }} />
      <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0, ...tokens.type.sm, color: pal.textSecondary, lineHeight: 1.5 }}>
        {title && <span style={{ fontWeight: tokens.weight.semibold, color: pal.text }}>{title}</span>}
        {children}
      </div>
    </div>
  );
}

// ─── Run ───

function IdentityTab({ d, onCopy }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {d.warnings.length > 0 && (
        <Note icon={TriangleAlert} tone="warning" title={R.warnings}>
          {d.warnings.map(w => <Coded key={w} code={w} gloss={REASON_GLOSS[w]} />)}
        </Note>
      )}
      <Block title={R.runIdentity}>
        <Rows labelWidth={LABEL_COL} align="left" rows={[
          [Q.run, <Ident key="r" id={d.run_id} label={Q.run} onCopy={onCopy} />],
          [R.state, stateLabel(d.state)],
          [R.outcome, outcomeLabel(d.outcome)],
          [R.subject, Q.subjectKinds[d.subject_kind]],
          [R.subjectId, <Ident key="s" id={d.subject_id} label={R.subjectId} onCopy={onCopy} />],
          [R.createdAt, formatStamp(d.created_at)],
          [R.completedAt, d.completed_at && formatStamp(d.completed_at)],
          [R.duration, formatMs(d.duration_ms)],
          [R.traceId, <Ident key="t" id={d.trace_id} label={R.traceId} onCopy={onCopy} />],
          [R.correlationId, <Ident key="c" id={d.correlation_id} label={R.correlationId} onCopy={onCopy} />],
          [R.causationId, <Ident key="k" id={d.causation_id} label={R.causationId} onCopy={onCopy} />],
          [R.sessionId, <Ident key="se" id={d.session_id} label={R.sessionId} onCopy={onCopy} />],
          [R.messageId, <Ident key="m" id={d.message_id} label={R.messageId} onCopy={onCopy} />],
          [R.commandDigest, <Code key="d">{d.command_digest.slice(0, 23)}…</Code>],
        ]} />
      </Block>
    </div>
  );
}

function EvidenceTab({ d, onOpenCitation }) {
  const pal = usePal();
  return (
    <Block title={R.evidence}>
      {!d.evidence_eligible ? <Note icon={Lock}>{R.evidenceHidden}</Note>
        : d.accepted_evidence.length === 0 ? <span style={{ ...tokens.type.sm, color: pal.textTertiary }}>{R.unknown}</span>
        : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 8 }}>
            {d.accepted_evidence.map((e, i) => (
              <li key={e.evidence_id} style={{ display: "flex", flexDirection: "column", gap: 10, padding: 12, borderRadius: tokens.radius.sm, border: `1px solid ${pal.borderSubtle}` }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ ...mono, color: pal.textTertiary }}>{i + 1}</span>
                  <span style={{ flex: 1, minWidth: 0, ...tokens.type.sm, fontWeight: tokens.weight.medium, color: pal.text }}>
                    {sourceKindLabel(e.source_kind)} · {locatorLabel({ kind: e.locator_kind, value: e.locator_value })}
                  </span>
                  <Button variant="outline" size="sm" icon={<ArrowUpRight size={13} strokeWidth={1.75} />} onClick={() => onOpenCitation(e.citation_id)}>{R.openCitation}</Button>
                </div>
                <Rows labelWidth={LABEL_COL} align="left" rows={[
                  ["Evidence ID", <IdText key="e" id={e.evidence_id} />],
                  [`${ru.chat.citationSourceRevision} · ${e.revision_number}`, <IdText key="r" id={e.source_revision_id} />],
                ]} />
              </li>
            ))}
          </ul>
        )}
    </Block>
  );
}

function DiagnosticsTab({ d, client, scope, onNavigate }) {
  const pal = usePal();
  const query = useQuery({
    queryKey: ["spine", "ported-operational-correlation", scope.workspaceId, scope.environmentId, d.run_id],
    queryFn: ({ signal }) => loadOperationalCorrelation(client, { referenceId: d.run_id, referenceKind: "run", scope }, signal),
    retry: false,
    staleTime: 0,
  });
  const result = query.data;
  const view = result?.view;
  const link = { ...interactiveBase, ...tokens.type.sm, padding: 0, background: "transparent", color: pal.text, textDecoration: "underline", textUnderlineOffset: 3, textDecorationColor: pal.border };
  return (
    <Block title={D.title} description={D.description}>
      {query.isPending ? (
        <span role="status" style={{ display: "inline-flex", alignItems: "center", gap: 8, ...tokens.type.sm, color: pal.textSecondary }}>
          <Spinner size={11} color={pal.textTertiary} />{ru.status.loading.title}
        </span>
      ) : query.error ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <span role="alert" style={{ ...tokens.type.sm, color: pal.danger }}>{ru.status.failed.description}</span>
          <Button variant="outline" size="sm" onClick={() => void query.refetch()} style={{ alignSelf: "flex-start" }}>{ru.documents.retry}</Button>
        </div>
      ) : view ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 14, animation: `halaska-fade-in ${motion.normal} ${motion.easeOut} both` }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span role="status" style={{ display: "inline-flex", alignItems: "center", gap: 6, ...tokens.type.sm, fontWeight: tokens.weight.medium, color: pal.text }}>
              <StatusDot status={view.coverage === "complete" ? "online" : "busy"} size={7} />
              {view.coverage === "complete" ? D.complete : D.partial}
            </span>
            <span aria-label={D.reference} style={{ ...tokens.type.xs, fontFamily: tokens.font.mono, color: pal.textTertiary }}>{shortRef(view.reference_id)}</span>
          </div>
          {view.missing_sources.length > 0 && (
            <span style={{ ...tokens.type.sm, color: pal.textSecondary }}>{D.missingSources}: {view.missing_sources.map(s => D.sources[s]).join(", ")}</span>
          )}
          <ul aria-label={D.records} style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 8 }}>
            {view.records.map(r => (
              <li key={r.source_record_id} style={{ display: "flex", flexDirection: "column", gap: 10, padding: 12, borderRadius: tokens.radius.sm, border: `1px solid ${pal.borderSubtle}` }}>
                <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 }}>
                  <strong style={{ ...tokens.type.sm, fontWeight: tokens.weight.medium, color: pal.text }}>{D.sources[r.source_kind]}</strong>
                  <time dateTime={r.observed_at} style={{ ...tokens.type.xs, color: pal.textTertiary }}>{formatStamp(r.observed_at)}</time>
                </div>
                <Rows labelWidth={LABEL_COL} align="left" rows={[
                  [D.sourceVersion, <Code key="v">{r.source_schema_version}</Code>],
                  [D.sourceRecord, <Code key="s">{shortRef(r.source_record_id)}</Code>],
                  r.metric_measurement && [D.measurement, <Code key="m">{r.metric_measurement}</Code>],
                  r.metric_coverage && [D.measurementCoverage, r.metric_coverage],
                  r.metric_value && [D.measurementValue, <Code key="mv">{r.metric_value}</Code>],
                  r.metric_currency && [D.currency, r.metric_currency],
                  r.run_state && [D.runState, r.run_state],
                  r.run_outcome && [D.runOutcome, r.run_outcome],
                  r.run_id && [D.runReference, <Code key="r">{shortRef(r.run_id)}</Code>],
                  r.message_id && [D.messageReference, <Code key="mm">{shortRef(r.message_id)}</Code>],
                  r.delivery_status && [D.deliveryState, r.delivery_status],
                  r.delivery_attempt_count && [D.deliveryAttempts, String(r.delivery_attempt_count)],
                  r.delivery_last_error_code && [D.safeFailureCode, <Code key="f">{r.delivery_last_error_code}</Code>],
                  r.feedback_outcome && [D.feedback, r.feedback_outcome],
                  r.trace_id && [D.traceReference, <Code key="t">{shortRef(r.trace_id)}</Code>],
                  r.correlation_id && [D.correlationReference, <Code key="c">{shortRef(r.correlation_id)}</Code>],
                ]} />
              </li>
            ))}
          </ul>
          <span style={{ ...tokens.type.xs, color: pal.textTertiary, lineHeight: 1.6 }}>
            {D.auditReference}: <Code>{shortRef(result.authorizationAuditEventId)}</Code> · {D.requestTrace}: <Code>{shortRef(result.requestTraceId)}</Code>
          </span>
          <nav aria-label={D.navigation} style={{ display: "flex", flexWrap: "wrap", gap: "6px 16px", paddingTop: 12, borderTop: `1px solid ${pal.borderSubtle}` }}>
            <button type="button" onClick={() => onNavigate("qaRuns")} style={link}>{D.qaRuns}</button>
            <button type="button" onClick={() => onNavigate("runs")} style={link}>{D.ingestionRuns}</button>
            <button type="button" onClick={() => onNavigate("knowledge")} style={link}>{D.documents}</button>
          </nav>
        </div>
      ) : null}
    </Block>
  );
}

// ─── Attempt ───

function SummaryTab({ a, onCopy }) {
  const pal = usePal();
  const c = a.completeness;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      <Block title={R.selectedAttempt}>
        <Rows labelWidth={LABEL_COL} align="left" rows={[
          [`${R.attempt} ${a.attempt_number}`, <Ident key="a" id={a.attempt_id} label={R.attempt} onCopy={onCopy} />],
          [R.attemptState, <Coded key="s" code={a.state} gloss={ATTEMPT_STATE[a.state]} />],
          [R.outcome, outcomeLabel(a.outcome)],
          [R.startedAt, formatStamp(a.started_at)],
          [R.completedAt, a.completed_at && formatStamp(a.completed_at)],
          [R.latency, formatMs(a.latency_ms)],
          [R.reason, a.reason && <Coded key="r" code={a.reason} gloss={REASON_GLOSS[a.reason]} />],
        ]} />
      </Block>
      {c && (
        <Block title={K.title}>
          <strong style={{ ...tokens.type.sm, fontWeight: tokens.weight.semibold, color: pal.text, marginTop: -4 }}>{K.partiality[c.partiality_status]}</strong>
          <Rows labelWidth={LABEL_COL} align="left" rows={[
            ...completenessRows(c),
            [R.targetScopeVersion, <Code key="t">{c.target_scope_version} · {shortRef(c.target_scope_id)}</Code>],
            [R.observedCoverageVersion, <Code key="o">{c.observed_coverage_version} · {shortRef(c.observed_coverage_id)}</Code>],
          ]} />
        </Block>
      )}
    </div>
  );
}

const versioned = (v) => <Code>{v.version} · {v.digest.slice(7, 19)}</Code>;

function VersionsTab({ a, onCopy }) {
  const pin = a.execution_pin;
  const ctx = a.context;
  return (
    <Block title={R.versions}>
      <Rows labelWidth={LABEL_COL} align="left" rows={[
        [R.agentVersion, versioned(pin.agent_version)],
        [R.promptVersion, versioned(pin.prompt_version)],
        [R.modelVersion, versioned(pin.model_profile)],
        [R.contextProfile, `${pin.context_profile_binding} · v${pin.context_profile_version}`],
        [R.projectionSnapshot, <Ident key="p" id={pin.projection.projection_snapshot_id} label={R.projectionSnapshot} onCopy={onCopy} />],
        [R.retrievalStrategy, ctx && <Code key="rs">{ctx.retrieval_strategy}</Code>],
        [R.asOf, ctx && `${formatStamp(ctx.canonical_as_of_time)} · generation ${ctx.canonical_as_of_generation}`],
        [R.freshness, ctx && <Code key="f">{ctx.freshness}</Code>],
        [R.health, ctx && <Code key="h">{ctx.health}</Code>],
        [R.evidenceCount, ctx && String(ctx.evidence_count)],
      ]} />
    </Block>
  );
}

function UsageTab({ a }) {
  const pal = usePal();
  const u = a.usage;
  const num = (n) => n.toLocaleString("ru-RU");
  const total = u.input_tokens + u.output_tokens;
  return (
    <Block title={R.usage}>
      {total > 0 && (
        <div aria-hidden="true" style={{ display: "flex", height: 6, borderRadius: 3, overflow: "hidden", background: pal.bgSubtle }}>
          <span style={{ width: `${(u.cached_input_tokens / total) * 100}%`, background: pal.textMuted }} />
          <span style={{ width: `${((u.input_tokens - u.cached_input_tokens) / total) * 100}%`, background: pal.textTertiary }} />
          <span style={{ width: `${(u.output_tokens / total) * 100}%`, background: pal.text }} />
        </div>
      )}
      <Rows labelWidth={LABEL_COL} align="left" rows={[
        [R.invocations, String(u.model_invocations)],
        [R.inputTokens, num(u.input_tokens)],
        [R.cachedInputTokens, num(u.cached_input_tokens)],
        [R.outputTokens, num(u.output_tokens)],
        [R.cost, <Code key="c">{u.cost}</Code>],
        [R.currency, u.currency],
      ]} />
    </Block>
  );
}

// ─── Event ───

function EventBody({ node, onCopy, onSelect }) {
  const pal = usePal();
  const { event: e, attempt: a } = node;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      <Block title="Событие">
        <Rows labelWidth={LABEL_COL} align="left" rows={[
          ["Тип", <Coded key="k" code={e.kind} gloss={EVENT_GLOSS[e.kind]} />],
          ["Время", formatStamp(e.occurred_at)],
          ["От начала попытки", formatOffset(node.at)],
          [R.reason, e.reason && <Coded key="r" code={e.reason} gloss={REASON_GLOSS[e.reason]} />],
          ["Event ID", <Ident key="e" id={e.event_id} label="Event ID" onCopy={onCopy} />],
        ]} />
      </Block>
      <Block title={`${R.timeline} · ${R.attempt} ${a.attempt_number}`}>
        <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column" }}>
          {a.timeline.map((t, i) => {
            const current = t.event_id === e.event_id;
            return (
              <li key={t.event_id} style={{ display: "grid", gridTemplateColumns: "14px minmax(0, 1fr)", columnGap: 10 }}>
                <span aria-hidden="true" style={{ position: "relative", display: "flex", justifyContent: "center" }}>
                  {i < a.timeline.length - 1 && <span style={{ position: "absolute", top: 14, bottom: -2, width: 1, background: pal.border }} />}
                  <span style={{ marginTop: 7, width: 7, height: 7, borderRadius: 4, background: current ? pal.text : pal.border }} />
                </span>
                <button type="button" aria-current={current ? "step" : undefined} onClick={() => onSelect(t.event_id)} style={{
                  ...interactiveBase, display: "flex", alignItems: "baseline", gap: 10, padding: "2px 0 12px", background: "transparent", textAlign: "left", minWidth: 0,
                }}>
                  <span style={{ fontFamily: tokens.font.mono, ...tokens.type.sm, color: current ? pal.text : pal.textSecondary, fontWeight: current ? tokens.weight.medium : tokens.weight.regular }}>{t.kind}</span>
                  <time dateTime={t.occurred_at} style={{ ...tokens.type.xs, color: pal.textTertiary }}>{formatStamp(t.occurred_at)}</time>
                  {t.reason && <span style={{ fontFamily: tokens.font.mono, ...tokens.type.xs, color: t.kind === "failed" ? pal.danger : pal.textTertiary }}>{t.reason}</span>}
                </button>
              </li>
            );
          })}
        </ol>
        <Button variant="ghost" size="sm" onClick={() => onSelect(a.attempt_id)} style={{ alignSelf: "flex-start", marginLeft: -8 }}>
          Открыть: {R.attempt} {a.attempt_number}
        </Button>
      </Block>
    </div>
  );
}

// ─── Panel ───

function Pill({ tone, children }) {
  const pal = usePal();
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 6, height: 22, padding: "0 8px", borderRadius: tokens.radius.pill, flexShrink: 0,
      border: `1px solid ${pal.borderSubtle}`, ...tokens.type.xs, color: pal.textSecondary, whiteSpace: "nowrap",
    }}><StatusDot status={DOT_STATUS[tone]} size={6} />{children}</span>
  );
}

const ATTEMPT_TONE = { running: "pending", succeeded: "online", failed: "error", cancelled: "offline" };

export default function QARunPanel({ d, node, client, scope, onSelect, onCopy, onOpenCitation, onNavigate }) {
  const pal = usePal();
  const tabs = TABS[node.type];
  const [tab, setTab] = useState(tabs?.[0][0]);
  const title = node.type === "run" ? ru.qaRuns.run : node.type === "attempt" ? `${R.attempt} ${node.attempt.attempt_number}` : node.event.kind;
  const metric = node.type === "run" ? [R.duration, formatMs(d.duration_ms)]
    : node.type === "attempt" ? [R.latency, formatMs(node.attempt.latency_ms)]
    : ["От начала попытки", formatOffset(node.at)];
  return (
    <section aria-label={title} style={{
      flex: 1, minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column",
      border: `1px solid ${pal.borderSubtle}`, borderRadius: tokens.radius.sm, background: pal.bg, overflow: "hidden",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, minHeight: 44, boxSizing: "border-box", padding: "8px 10px 8px 16px", borderBottom: tabs ? "none" : `1px solid ${pal.borderSubtle}` }}>
        <h2 style={{
          margin: 0, minWidth: 0, ...tokens.type.md, fontWeight: tokens.weight.medium, color: node.error ? pal.danger : pal.text,
          fontFamily: node.type === "event" ? tokens.font.mono : tokens.font.sans, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
        }}>
          {title}
          {node.type === "run" && <span style={{ marginLeft: 8, fontFamily: tokens.font.mono, ...tokens.type.sm, fontWeight: tokens.weight.regular, color: pal.textTertiary }}>{d.run_id.slice(-8)}</span>}
        </h2>
        {node.type === "run" && <Pill tone={OUTCOME_TONE[d.outcome]}>{stateLabel(d.state)} · {outcomeLabel(d.outcome)}</Pill>}
        {node.type === "attempt" && <Pill tone={ATTEMPT_TONE[node.attempt.state]}>{ATTEMPT_STATE[node.attempt.state]}</Pill>}
        <span style={{ flex: 1 }} />
        <span style={{ ...tokens.type.sm, color: pal.textTertiary, whiteSpace: "nowrap" }}>{metric[0]} <span style={{ fontFamily: tokens.font.mono, ...tokens.type.xs, color: pal.text }}>{metric[1]}</span></span>
        <CopyIcon text={node.id} label="Скопировать идентификатор" onCopy={onCopy} />
      </div>

      {tabs && (
        <div role="tablist" aria-label={title} style={{ display: "flex", gap: 4, padding: "0 16px", borderBottom: `1px solid ${pal.borderSubtle}` }}
          onKeyDown={(e) => {
            const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
            if (!step) return;
            const next = (tabs.findIndex(([id]) => id === tab) + step + tabs.length) % tabs.length;
            setTab(tabs[next][0]);
            e.currentTarget.querySelectorAll("[role=tab]")[next]?.focus();
          }}>
          {tabs.map(([id, label]) => {
            const active = id === tab;
            const count = id === "evidence" && d.evidence_eligible ? d.accepted_evidence.length : null;
            return (
              <button key={id} type="button" role="tab" aria-selected={active} tabIndex={active ? 0 : -1} onClick={() => setTab(id)} style={{
                ...interactiveBase, ...tokens.type.sm, height: 34, padding: "0 10px", marginBottom: -1, background: "transparent", borderRadius: 0,
                color: active ? pal.text : pal.textTertiary, borderBottom: `1.5px solid ${active ? pal.text : "transparent"}`,
                display: "inline-flex", alignItems: "center", gap: 6, whiteSpace: "nowrap",
              }}>
                {label}
                {count > 0 && <span style={{ fontFamily: tokens.font.mono, ...tokens.type.xs, color: pal.textTertiary }}>{count}</span>}
              </button>
            );
          })}
        </div>
      )}

      <div role={tabs ? "tabpanel" : undefined} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "18px 16px 24px" }}>
        {node.type === "run" && tab === "identity" && <IdentityTab d={d} onCopy={onCopy} />}
        {node.type === "run" && tab === "evidence" && <EvidenceTab d={d} onOpenCitation={onOpenCitation} />}
        {node.type === "run" && tab === "diagnostics" && <DiagnosticsTab key={d.run_id} d={d} client={client} scope={scope} onNavigate={onNavigate} />}
        {node.type === "attempt" && tab === "summary" && <SummaryTab a={node.attempt} onCopy={onCopy} />}
        {node.type === "attempt" && tab === "versions" && <VersionsTab a={node.attempt} onCopy={onCopy} />}
        {node.type === "attempt" && tab === "usage" && <UsageTab a={node.attempt} />}
        {node.type === "event" && <EventBody node={node} onCopy={onCopy} onSelect={onSelect} />}
      </div>
    </section>
  );
}
