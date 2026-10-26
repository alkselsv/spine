import { useState } from "react";
import { Check, CircleAlert, Copy, TriangleAlert } from "lucide-react";
import { AlertBanner, Button, Caption, Label, StatusDot, motion, tokens, useModalFocus, usePal, useThemeContext } from "../halaska-kit";
import { DOT_STATUS, PIPELINE, documentState, idHead, idTail, pipelineStage } from "./spineData";
import { ru } from "./ru";

const NOTE_ICON = { warning: TriangleAlert, danger: CircleAlert };

if (typeof document !== "undefined" && !document.getElementById("spine-keyframes")) {
  const style = document.createElement("style");
  style.id = "spine-keyframes";
  style.textContent = "@keyframes spine-stage { 0%,100% { opacity: 1 } 50% { opacity: 0.35 } }";
  document.head.appendChild(style);
}
const stagePulse = "spine-stage 1.6s ease-in-out infinite";

// Dots and note icons share one fixed-width slot, so every text line starts at the same x.
const MARK_SLOT = 11;
const MARK_GAP = 6;

const Mark = ({ tone, label, pulse }) => <StatusDot status={DOT_STATUS[tone]} pulse={pulse} size={7} label={label} />;

const slot = { width: MARK_SLOT, display: "inline-flex", alignItems: "center", justifyContent: "center", flexShrink: 0 };

function StateLabel({ label }) {
  const pal = usePal();
  return <span style={{ ...tokens.type.sm, fontWeight: tokens.weight.medium, color: pal.text }}>{label}</span>;
}

export function StateMark({ tone, label, pulse }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: MARK_GAP, whiteSpace: "nowrap" }}>
      <span style={slot}><Mark tone={tone} label={label} pulse={pulse} /></span>
      <StateLabel label={label} />
    </span>
  );
}

// Dot + sentence-case label; an optional note sits under the label text, its icon in the dot's column.
export function DocumentStateBadge({ doc, note }) {
  const pal = usePal();
  const s = documentState(doc);
  const stage = pipelineStage(doc);
  const Icon = note && NOTE_ICON[note.tone];
  const sub = { ...tokens.type.xs, whiteSpace: "nowrap", minWidth: 0 };
  return (
    <span style={{
      display: "inline-grid", gridTemplateColumns: `${MARK_SLOT}px minmax(0, 1fr)`, columnGap: MARK_GAP, rowGap: 2,
      alignItems: "center", minWidth: 0, maxWidth: "100%",
    }}>
      <span style={slot}><Mark tone={s.tone} label={s.label} pulse={s.pulse} /></span>
      <span style={{ whiteSpace: "nowrap", minWidth: 0 }}><StateLabel label={s.label} /></span>
      {stage !== null && <>
        <span />
        <span style={{ ...sub, display: "inline-flex", alignItems: "center", gap: 6, color: pal.textTertiary }}>
          <PipelineBar stage={stage} pulse={s.pulse} />
          {note?.text ?? `этап ${stage + 1} из ${PIPELINE.length}`}
        </span>
      </>}
      {note && stage === null && <>
        <span style={slot}>{Icon && <Icon size={11} strokeWidth={2} color={note.tone === "danger" ? pal.danger : pal.warning} />}</span>
        <span style={{ ...sub, overflow: "hidden", textOverflow: "ellipsis", color: Icon ? pal.textSecondary : pal.textTertiary }}>{note.text}</span>
      </>}
    </span>
  );
}

export function PipelineBar({ stage, pulse }) {
  const pal = usePal();
  return (
    <span aria-hidden="true" style={{ display: "inline-flex", gap: 2, flexShrink: 0 }}>
      {PIPELINE.map((step, i) => (
        <span key={step} style={{
          width: 10, height: 3, borderRadius: 2,
          background: i < stage ? pal.textTertiary : i === stage ? pal.warning : pal.bgMuted,
          animation: i === stage && pulse ? stagePulse : undefined,
        }} />
      ))}
    </span>
  );
}

export function PipelineSteps({ stage, pulse, caption }) {
  const pal = usePal();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <ol aria-label="Этапы обработки" style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gridTemplateColumns: `repeat(${PIPELINE.length}, 1fr)`, gap: 4 }}>
        {PIPELINE.map((step, i) => {
          const done = i < stage;
          const current = i === stage;
          return (
            <li key={step} aria-current={current ? "step" : undefined} style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 0 }}>
              <span style={{
                height: 4, borderRadius: 2, background: done ? pal.textTertiary : current ? pal.warning : pal.bgMuted,
                animation: current && pulse ? stagePulse : undefined,
              }} />
              <span style={{
                display: "inline-flex", alignItems: "center", gap: 4, ...tokens.type.xs, whiteSpace: "nowrap", overflow: "hidden",
                color: current ? pal.text : done ? pal.textSecondary : pal.textTertiary, fontWeight: current ? tokens.weight.medium : tokens.weight.regular,
              }}>
                {done && <Check size={11} strokeWidth={2} style={{ flexShrink: 0 }} />}
                {step}
              </span>
            </li>
          );
        })}
      </ol>
      {caption && <Caption style={{ lineHeight: 1.5 }}>{caption}</Caption>}
    </div>
  );
}

// "What happened and what to do" block that opens the details panel for documents with a problem.
export function IssueSummary({ issue, onAction }) {
  const pal = usePal();
  const Icon = NOTE_ICON[issue.tone];
  const color = issue.tone === "danger" ? pal.danger : pal.warning;
  return (
    <div role="status" style={{
      display: "flex", flexDirection: "column", gap: 12, padding: 14, borderRadius: tokens.radius.md,
      background: issue.tone === "danger" ? pal.dangerBg : pal.warningBg,
      border: `1px solid ${pal.borderSubtle}`,
    }}>
      <div style={{ display: "flex", gap: 10 }}>
        <Icon size={16} strokeWidth={1.75} color={color} style={{ flexShrink: 0, marginTop: 2 }} />
        <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
          <span style={{ ...tokens.type.sm, fontWeight: tokens.weight.semibold, color: pal.text }}>{issue.title}</span>
          {issue.description && <span style={{ ...tokens.type.sm, color: pal.textSecondary, lineHeight: 1.5 }}>{issue.description}</span>}
        </div>
      </div>
      {issue.action && onAction && (
        <div style={{ paddingLeft: 26 }}>
          <Button size="sm" variant="primary" onClick={() => onAction(issue.action)}>{issue.actionLabel}</Button>
        </div>
      )}
    </div>
  );
}

export function IdText({ id, size = "sm" }) {
  const pal = usePal();
  return (
    <span title={id} style={{ display: "flex", minWidth: 0, fontFamily: tokens.font.mono, ...tokens.type[size], whiteSpace: "nowrap" }}>
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", color: pal.textTertiary }}>{idHead(id)}</span>
      <span style={{ flexShrink: 0, color: pal.text, fontWeight: tokens.weight.semibold }}>{idTail(id)}</span>
    </span>
  );
}

// CopyInput layout from the kit, with a localized copy action.
export function CopyField({ label, value, onCopy }) {
  const pal = usePal();
  const [copied, setCopied] = useState(false);
  const copy = () => {
    onCopy(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <Label>{label}</Label>
      <div style={{
        display: "flex", alignItems: "center", gap: 8, height: 38, padding: "0 6px 0 14px", boxSizing: "border-box",
        borderRadius: tokens.radius.md, background: pal.bgSubtle, border: `1px solid ${pal.borderSubtle}`,
      }}>
        <span style={{ flex: 1, minWidth: 0 }}><IdText id={value} /></span>
        <Button type="button" size="sm" variant="ghost" icon={copied ? <Check size={13} strokeWidth={1.75} /> : <Copy size={13} strokeWidth={1.5} />}
          aria-label={`Скопировать: ${label}`} onClick={copy}>{copied ? "Скопировано" : "Копировать"}</Button>
      </div>
    </div>
  );
}

// Kit FormDialog look, but with localized actions and a submit that can be disabled.
export function SpineModal({ open, onClose, title, description, children, submitLabel, onSubmit, submitDisabled, danger, width = 480 }) {
  const theme = useThemeContext();
  const pal = usePal();
  const panelRef = useModalFocus(open, onClose);
  if (!open) return null;
  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", backdropFilter: "blur(4px)", WebkitBackdropFilter: "blur(4px)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 10003,
      animation: `halaska-fade-in ${motion.fast} ${motion.easeOut} both`,
    }}>
      <div ref={panelRef} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} onClick={(e) => e.stopPropagation()} style={{
        background: theme === "dark" ? "rgba(30,30,30,0.95)" : "rgba(255,255,255,0.95)",
        backdropFilter: "blur(20px)", WebkitBackdropFilter: "blur(20px)",
        borderRadius: tokens.radius.lg, padding: 24, width: `min(${width}px, calc(100vw - 32px))`, boxSizing: "border-box",
        border: `1px solid ${pal.borderSubtle}`, boxShadow: `0 16px 48px ${pal.shadowLg}`,
        animation: `halaska-scale-in ${motion.normal} ${motion.emphasized} both`, fontFamily: tokens.font.sans,
      }}>
        <div style={{ ...tokens.type.lg, fontWeight: tokens.weight.semibold, color: pal.text, marginBottom: description ? 4 : 16 }}>{title}</div>
        {description && <div style={{ ...tokens.type.sm, color: pal.textSecondary, marginBottom: 16, lineHeight: 1.6 }}>{description}</div>}
        <form onSubmit={(e) => { e.preventDefault(); if (submitDisabled) return; onSubmit?.(); onClose?.(); }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>{children}</div>
          <div style={{ marginTop: 20, display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button variant="ghost" size="sm" type="button" onClick={onClose}>{submitLabel ? "Отмена" : "Закрыть"}</Button>
            {submitLabel && <Button variant={danger ? "danger" : "primary"} size="sm" type="submit" disabled={submitDisabled}>{submitLabel}</Button>}
          </div>
        </form>
      </div>
    </div>
  );
}

const STATE_VARIANT = {
  partial: "warning", stale: "warning", degraded: "warning",
  permissionDenied: "danger", validationFailed: "danger", failed: "danger",
};

// Spine's StatusPanel, rendered with the kit's AlertBanner.
export function InterfaceStateBanner({ state, onRetry, onBack }) {
  const copy = ru.status[state];
  if (!copy || !STATE_VARIANT[state]) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <AlertBanner variant={STATE_VARIANT[state]} title={copy.title} description={copy.description} />
      {(onRetry || onBack) && (
        <div style={{ display: "flex", gap: 8 }}>
          {onRetry && <Button variant="outline" size="sm" onClick={onRetry}>{ru.documents.retry}</Button>}
          {onBack && <Button variant="ghost" size="sm" onClick={onBack}>{ru.documents.backToOverview}</Button>}
        </div>
      )}
    </div>
  );
}

export function SectionTitle({ children, action }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
      <Label style={{ ...tokens.type.xs, textTransform: "uppercase", letterSpacing: "0.05em" }}>{children}</Label>
      {action}
    </div>
  );
}

export function KeyValues({ rows }) {
  const pal = usePal();
  return (
    <dl style={{ margin: 0, display: "grid", gridTemplateColumns: "minmax(120px, 44%) 1fr", columnGap: 12, rowGap: 8 }}>
      {rows.filter(Boolean).map(([label, value, mono]) => (
        <div key={label} style={{ display: "contents" }}>
          <dt style={{ ...tokens.type.sm, color: pal.textTertiary }}>{label}</dt>
          <dd style={{
            margin: 0, minWidth: 0, ...tokens.type.sm, color: pal.text, overflowWrap: "anywhere",
            fontFamily: mono ? tokens.font.mono : tokens.font.sans,
          }}>{value ?? <Caption>{ru.documents.unknown}</Caption>}</dd>
        </div>
      ))}
    </dl>
  );
}
