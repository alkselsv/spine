import { useEffect, useRef, useState } from "react";
import { ChevronRight, ThumbsDown, ThumbsUp } from "lucide-react";
import { Button, Spinner, interactiveBase, motion, tokens, usePal, useThemeContext } from "../../halaska-kit";
import { ru } from "../ru";
import { mono } from "./chatStyles";

const C = ru.chat;

// Fixed to the viewport and rendered only while open, so it never stretches the scrolling thread.
export function Pop({ trigger, children, width = 260 }) {
  const theme = useThemeContext();
  const pal = usePal();
  const [rect, setRect] = useState(null);
  const ref = useRef(null);
  const close = () => setRect(null);

  useEffect(() => {
    if (!rect) return;
    const onDown = (e) => { if (!ref.current?.contains(e.target)) setRect(null); };
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); setRect(null); } };
    const onScroll = (e) => { if (!ref.current?.contains(e.target)) setRect(null); };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [rect]);

  const above = rect && rect.bottom > window.innerHeight * 0.55;
  return (
    <span ref={ref} style={{ display: "inline-flex" }}>
      <span style={{ display: "inline-flex" }} onClick={(e) => setRect(r => (r ? null : e.currentTarget.getBoundingClientRect()))}>{trigger}</span>
      {rect && (
        <div role="dialog" style={{
          position: "fixed", left: Math.max(16, Math.min(rect.left, window.innerWidth - width - 16)), width, zIndex: 10000,
          ...(above ? { bottom: window.innerHeight - rect.top + 6 } : { top: rect.bottom + 6 }),
          padding: 12, boxSizing: "border-box", borderRadius: tokens.radius.md, border: `1px solid ${pal.borderSubtle}`,
          background: theme === "dark" ? "rgba(30,30,30,0.97)" : "rgba(255,255,255,0.97)", backdropFilter: "blur(16px)", WebkitBackdropFilter: "blur(16px)",
          boxShadow: `0 8px 24px ${pal.shadowLg}`, animation: `halaska-scale-in ${motion.fast} ${motion.easeOut} both`,
          transformOrigin: above ? "bottom left" : "top left",
        }}>{typeof children === "function" ? children(close) : children}</div>
      )}
    </span>
  );
}

// The kit Tooltip stays mounted while hidden, which widens scroll containers near their edge;
// this one exists only while hovered or focused and is clamped to the viewport.
export function Tip({ text, children }) {
  const theme = useThemeContext();
  const [rect, setRect] = useState(null);
  const show = (e) => setRect(e.currentTarget.getBoundingClientRect());
  const hide = () => setRect(null);
  return (
    <span style={{ display: "inline-flex" }} onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide} onClick={hide}>
      {children}
      {rect && (
        <span role="tooltip" style={{
          position: "fixed", zIndex: 10001, top: rect.top - 8, left: Math.max(8, Math.min(rect.left + rect.width / 2, window.innerWidth - 8)),
          transform: `translate(${rect.left + rect.width / 2 > window.innerWidth - 120 ? "-100%" : "-50%"}, -100%)`,
          padding: "5px 9px", borderRadius: tokens.radius.sm, whiteSpace: "nowrap", pointerEvents: "none",
          background: theme === "dark" ? "#f4f4f5" : "#18181b", color: theme === "dark" ? "#18181b" : "#fafafa",
          ...tokens.type.xs, fontFamily: tokens.font.sans, animation: `halaska-fade-in ${motion.fast} ${motion.easeOut} both`,
        }}>{text}</span>
      )}
    </span>
  );
}

export function IconAction({ label, active, disabled, onClick, children }) {
  const pal = usePal();
  const [hover, setHover] = useState(false);
  return (
    <Tip text={label}>
      <button type="button" aria-label={label} aria-pressed={active} disabled={disabled} onClick={onClick}
        onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
        style={{
          ...interactiveBase, width: 26, height: 26, display: "inline-flex", alignItems: "center", justifyContent: "center",
          borderRadius: tokens.radius.xs, background: hover && !disabled ? pal.bgMuted : "transparent",
          color: active ? pal.text : hover && !disabled ? pal.text : pal.textTertiary, cursor: disabled ? "default" : "pointer",
        }}>{children}</button>
    </Tip>
  );
}

export function Pill({ icon: Icon, label, lead, open, onClick }) {
  const pal = usePal();
  const [hover, setHover] = useState(false);
  return (
    <button type="button" aria-expanded={open} onClick={onClick} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{
        ...interactiveBase, ...mono, display: "inline-flex", alignItems: "center", gap: 8, height: 26, padding: "0 8px",
        borderRadius: tokens.radius.sm, border: `1px solid ${open ? pal.border : pal.borderSubtle}`,
        background: open || hover ? pal.bgMuted : pal.bgSubtle, color: pal.textSecondary, whiteSpace: "nowrap",
      }}>
      {lead ?? <Icon size={13} strokeWidth={1.5} />}
      {label}
      <ChevronRight size={12} strokeWidth={1.75} style={{ transform: open ? "rotate(90deg)" : "none", transition: `transform ${motion.normal} ${motion.emphasized}` }} />
    </button>
  );
}

export function AgentMark({ size = 22 }) {
  const pal = usePal();
  return (
    <span aria-hidden="true" style={{
      width: size, height: size, borderRadius: Math.round(size * 0.28), background: pal.text, color: pal.bg, flexShrink: 0,
      display: "inline-flex", alignItems: "center", justifyContent: "center",
      fontFamily: tokens.font.mono, fontWeight: tokens.weight.semibold, fontSize: Math.round(size * 0.55), lineHeight: 1,
    }}>S</span>
  );
}

export function PanelHeading({ children }) {
  const pal = usePal();
  return <div style={{ ...mono, color: pal.textTertiary, marginBottom: 10 }}>{children}</div>;
}

export function Rows({ rows, labelWidth = "auto", align = "right" }) {
  const pal = usePal();
  return (
    <dl style={{ margin: 0, display: "grid", gridTemplateColumns: `${labelWidth} minmax(0, 1fr)`, columnGap: 16, rowGap: 6 }}>
      {rows.filter(Boolean).map(([k, v, opts = {}]) => (
        <div key={k} style={{ display: "contents" }}>
          <dt style={{ ...tokens.type.sm, color: pal.textTertiary }}>{k}</dt>
          <dd style={{
            margin: 0, minWidth: 0, textAlign: opts.left ? "left" : align, ...tokens.type.sm, color: pal.text,
            fontFamily: opts.mono ? tokens.font.mono : tokens.font.sans, overflowWrap: "anywhere",
          }}>{v ?? <span style={{ color: pal.textTertiary }}>{ru.qaRuns.unknown}</span>}</dd>
        </div>
      ))}
    </dl>
  );
}

// Thumbs open a short form; once the server records feedback, the choice is final.
export function FeedbackThumbs({ entry, label, onSubmit }) {
  const pal = usePal();
  const [comment, setComment] = useState("");
  const locked = !!entry;
  const thumb = (outcome) => {
    const Icon = outcome === "helpful" ? ThumbsUp : ThumbsDown;
    const text = outcome === "helpful" ? C.feedbackHelpful : C.feedbackIncorrect;
    const chosen = entry?.outcome === outcome;
    const tip = chosen ? (entry.status === "recorded" ? C.feedbackRecorded : C.feedbackSubmitting) : text;
    if (locked) {
      return (
        <IconAction key={outcome} label={tip} active={chosen} disabled>
          {chosen && entry.status === "submitting" ? <Spinner size={11} color={pal.textTertiary} /> : <Icon size={14} strokeWidth={1.5} fill={chosen ? "currentColor" : "none"} />}
        </IconAction>
      );
    }
    return (
      <Pop key={outcome} width={300} trigger={<IconAction label={text}><Icon size={14} strokeWidth={1.5} /></IconAction>}>
        {(close) => (
          <form onSubmit={(e) => { e.preventDefault(); onSubmit(outcome, comment); close(); }} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <PanelHeading>{label} · {text}</PanelHeading>
            <label style={{ display: "flex", flexDirection: "column", gap: 6, ...tokens.type.sm, color: pal.textSecondary }}>
              {C.feedbackCommentLabel}
              <textarea value={comment} maxLength={1000} rows={3} autoFocus onChange={(e) => setComment(e.target.value)} style={{
                resize: "vertical", minHeight: 64, padding: "8px 10px", borderRadius: tokens.radius.sm, border: `1px solid ${pal.border}`,
                background: pal.bgSubtle, color: pal.text, fontFamily: tokens.font.sans, ...tokens.type.sm, outline: "none",
              }} />
            </label>
            <span style={{ ...tokens.type.xs, color: pal.textTertiary, lineHeight: 1.45 }}>{C.feedbackCommentHint}</span>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 6, marginTop: 4 }}>
              <Button type="button" variant="ghost" size="sm" onClick={close}>Отмена</Button>
              <Button type="submit" variant="primary" size="sm">Отправить отзыв</Button>
            </div>
          </form>
        )}
      </Pop>
    );
  };
  return <div role="group" aria-label={label} style={{ display: "flex", alignItems: "center", gap: 2 }}>{thumb("helpful")}{thumb("incorrect")}</div>;
}
