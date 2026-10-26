import { useEffect, useRef, useState } from "react";
import { Check, CircleAlert, LoaderCircle } from "lucide-react";
import { Badge, Label, Toast, Tooltip, arrowNav, interactiveBase, motion, tokens, usePal, useThemeContext } from "../halaska-kit";

export const caps = { textTransform: "uppercase", letterSpacing: "0.06em" };

export function FieldLabel({ children, htmlFor }) {
  return <Label htmlFor={htmlFor} style={{ ...tokens.type.xs, ...caps, fontFamily: tokens.font.mono }}>{children}</Label>;
}

export function MetaBadges({ metadata }) {
  const entries = Object.entries(metadata);
  if (!entries.length) return <Badge>—</Badge>;
  const [[key, value], ...rest] = entries;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, minWidth: 0 }}>
      <Badge style={{ fontFamily: tokens.font.mono, whiteSpace: "nowrap" }}>{key} {value}</Badge>
      {rest.length > 0 && (
        <Tooltip text={rest.map(([k, v]) => `${k}: ${v}`).join(" · ")}>
          <span tabIndex={0} style={{ display: "inline-flex", outline: "none" }}>
            <Badge style={{ fontFamily: tokens.font.mono }}>+{rest.length}</Badge>
          </span>
        </Tooltip>
      )}
    </span>
  );
}

const STATUS = {
  completed: { variant: "default", icon: <Check size={11} strokeWidth={2} />, label: "Completed" },
  processing: { variant: "accent", icon: <LoaderCircle size={11} strokeWidth={2} style={{ animation: "halaska-spin 1s linear infinite" }} />, label: "Processing" },
  failed: { variant: "danger", icon: <CircleAlert size={11} strokeWidth={2} />, label: "Failed" },
};

export function ContentStatus({ status }) {
  const s = STATUS[status] || STATUS.completed;
  return <Badge variant={s.variant} style={{ fontFamily: tokens.font.mono, whiteSpace: "nowrap" }}>{s.icon}{s.label}</Badge>;
}

// DropdownMenu from the kit always anchors to the trigger's right edge;
// this variant can anchor left so menus near the left edge stay on screen.
export function Menu({ trigger, items, align = "left", side = "bottom", minWidth = 200 }) {
  const theme = useThemeContext();
  const pal = usePal(theme);
  const [open, setOpen] = useState(false);
  const [hoverIdx, setHoverIdx] = useState(-1);
  const menuRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => menuRef.current?.querySelector("button")?.focus({ preventScroll: true }), 0);
    return () => clearTimeout(t);
  }, [open]);

  return (
    <div style={{ position: "relative", display: "inline-flex", minWidth: 0 }}>
      <div onClick={() => setOpen(o => !o)} style={{ display: "flex", minWidth: 0, width: "100%" }}>
        {trigger(open)}
      </div>
      {open && <div onClick={() => setOpen(false)} style={{ position: "fixed", inset: 0, zIndex: 9999 }} />}
      {open && (
        <div ref={menuRef} role="menu"
          onKeyDown={(e) => arrowNav(e, { horizontal: false, onEscape: () => setOpen(false) })}
          style={{
            position: "absolute", [align]: 0, zIndex: 10000, minWidth,
            ...(side === "top" ? { bottom: "100%", marginBottom: 6 } : { top: "100%", marginTop: 6 }),
            background: theme === "dark" ? "rgba(30,30,30,0.97)" : "rgba(255,255,255,0.97)",
            backdropFilter: "blur(16px)", WebkitBackdropFilter: "blur(16px)",
            border: `1px solid ${pal.borderSubtle}`, borderRadius: tokens.radius.md,
            padding: 4, boxShadow: `0 8px 24px ${pal.shadowLg}`,
            transformOrigin: `${side === "top" ? "bottom" : "top"} ${align}`,
            animation: `halaska-scale-in ${motion.fast} ${motion.easeOut} both`,
          }}>
          {items.map((item, i) => item.separator ? (
            <div key={i} role="separator" style={{ height: 1, background: pal.borderSubtle, margin: "4px 0" }} />
          ) : item.heading ? (
            <div key={i} style={{ ...tokens.type.xs, ...caps, color: pal.textTertiary, padding: "6px 12px 4px", fontFamily: tokens.font.mono }}>{item.heading}</div>
          ) : (
            <button key={i} type="button" role="menuitemradio" aria-checked={!!item.active}
              onClick={() => { item.onClick?.(); setOpen(false); }}
              onMouseEnter={() => setHoverIdx(i)} onMouseLeave={() => setHoverIdx(-1)}
              style={{
                ...interactiveBase, width: "100%", textAlign: "left", ...tokens.type.sm,
                padding: "8px 12px", borderRadius: tokens.radius.sm,
                color: item.danger ? pal.danger : item.active ? pal.text : pal.textSecondary,
                background: hoverIdx === i ? pal.bgMuted : "transparent",
                display: "flex", alignItems: "center", gap: 10,
              }}>
              {item.icon && <span aria-hidden="true" style={{ display: "flex", color: pal.textTertiary }}>{item.icon}</span>}
              <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.label}</span>
              {item.meta && <span style={{ ...tokens.type.xs, color: pal.textTertiary }}>{item.meta}</span>}
              {item.active && <Check size={13} strokeWidth={1.75} color={pal.textSecondary} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function useToasts() {
  const [toasts, setToasts] = useState([]);
  const push = (message, variant = "default") => {
    const id = Math.random().toString(36).slice(2);
    setToasts(list => [...list, { id, message, variant }]);
    setTimeout(() => setToasts(list => list.filter(t => t.id !== id)), 2800);
  };
  const host = (
    <div aria-live="polite" style={{
      position: "fixed", bottom: 24, left: "50%", transform: "translateX(-50%)", zIndex: 10002,
      display: "flex", flexDirection: "column", alignItems: "center", gap: 8, pointerEvents: "none",
    }}>
      {toasts.map(t => (
        <div key={t.id} style={{ animation: `halaska-step-in ${motion.normal} ${motion.emphasized} both` }}>
          <Toast message={t.message} variant={t.variant} />
        </div>
      ))}
    </div>
  );
  return { push, host };
}
