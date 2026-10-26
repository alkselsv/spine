import { useEffect, useRef, useState } from "react";
import { RefreshCw, SlidersHorizontal, X } from "lucide-react";
import { Button, ChevronIcon, Chip, Orb, Select, StatusDot, Tooltip, interactiveBase, motion, tokens, usePal, useThemeContext } from "../halaska-kit";
import { Menu } from "../components/ui";
import { ALL, FILTER_KEYS, FILTER_OPTIONS, PRESETS, SCENARIOS } from "./spineData";
import { ru } from "./ru";

const D = ru.documents;
const CONNECTION = {
  connected: { dot: "online", label: ru.connection.connected },
  checking: { dot: "busy", label: ru.connection.checking },
  checked: { dot: "online", label: ru.connection.checked },
  failed: { dot: "error", label: ru.connection.failed },
};

// Kit model-picker pill (ChatParadigmExample) carrying Spine's workspace / environment scope.
function ScopePill({ workspace, environment, workspaceOptions, environmentOptions, connection, onChange }) {
  const pal = usePal();
  const ws = workspaceOptions.find(w => w.id === workspace) ?? { id: workspace, label: workspace };
  const env = environmentOptions.find(e => e.id === environment) ?? { id: environment, label: environment };
  const items = [
    { heading: ru.scope.workspaceLabel },
    ...workspaceOptions.map(w => ({ label: w.label, active: w.id === workspace, onClick: () => onChange({ workspace: w.id }) })),
    { separator: true },
    { heading: ru.scope.environmentLabel },
    ...environmentOptions.map(e => ({ label: e.label, active: e.id === environment, onClick: () => onChange({ environment: e.id }) })),
  ];
  return (
    <Menu minWidth={220} items={items} trigger={(open) => (
      <button type="button" aria-haspopup="menu" aria-expanded={open}
        aria-label={`${ru.scope.workspaceLabel}: ${ws.label}, ${ru.scope.environmentLabel}: ${env.label}`}
        style={{
          ...interactiveBase, display: "inline-flex", alignItems: "center", gap: 8, height: 30, padding: "0 12px",
          borderRadius: tokens.radius.pill, background: open ? pal.bgMuted : pal.bgSubtle,
          border: `1px solid ${open ? pal.border : pal.borderSubtle}`, whiteSpace: "nowrap",
        }}>
        <StatusDot status={CONNECTION[connection].dot} size={7} label={CONNECTION[connection].label} />
        <span style={{ ...tokens.type.sm, color: pal.text }}>{ws.label}</span>
        <span aria-hidden="true" style={{ ...tokens.type.sm, color: pal.textTertiary }}>/</span>
        <span style={{ ...tokens.type.sm, color: pal.textSecondary, fontFamily: tokens.font.mono }}>{env.label}</span>
        <ChevronIcon size={9} direction={open ? "up" : "down"} style={{ color: pal.textTertiary }} />
      </button>
    )} />
  );
}

export function SpineTopBar({ kb, compact, title }) {
  const pal = usePal();
  const conn = CONNECTION[kb.connection];
  const inFlight = kb.inFlight.length;
  const orbLabel = `${ru.documents.upload.processing} · ${inFlight}`;
  return (
    <header style={{
      height: 56, flexShrink: 0, display: "flex", alignItems: "center", gap: 12, padding: "0 24px",
      borderBottom: `1px solid ${pal.borderSubtle}`,
    }}>
      <h1 id="page-heading" style={{ position: "absolute", width: 1, height: 1, margin: 0, overflow: "hidden", clip: "rect(0 0 0 0)" }}>
        {title}
      </h1>
      <ScopePill workspace={kb.workspace} environment={kb.environment} workspaceOptions={kb.workspaceOptions}
        environmentOptions={kb.environmentOptions} connection={kb.connection} onChange={kb.changeScope} />
      <div style={{ flex: 1 }} />
      {inFlight > 0 && (compact
        ? <Tooltip text={orbLabel}><span tabIndex={0} style={{ display: "inline-flex", outline: "none" }}><Orb size={18} variant="orbit" label={orbLabel} /></span></Tooltip>
        : <Orb pill size={18} variant="orbit" label={orbLabel} />)}
      <span role="status" aria-live="polite" style={{ display: "inline-flex", alignItems: "center", gap: 6, ...tokens.type.sm, color: pal.textSecondary, whiteSpace: "nowrap" }}>
        {compact
          ? <Tooltip text={conn.label}><span tabIndex={0} style={{ display: "inline-flex", outline: "none" }}><StatusDot status={conn.dot} pulse={kb.connection === "checking"} size={7} label={conn.label} /></span></Tooltip>
          : <><StatusDot status={conn.dot} pulse={kb.connection === "checking"} size={7} label={conn.label} />{conn.label}</>}
      </span>
      <Button variant="outline" size="sm" loading={kb.connection === "checking"} icon={<RefreshCw size={13} strokeWidth={1.5} />}
        aria-label={ru.connection.check} onClick={kb.checkConnection}>
        {compact ? null : ru.connection.check}
      </Button>
    </header>
  );
}

// Prototype-only control: a tab in the bottom gutter, kept out of the product header and marked as tooling.
export function DemoSwitcher({ kb }) {
  const theme = useThemeContext();
  const pal = usePal();
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const current = SCENARIOS.find(s => s.value === kb.scenario) ?? SCENARIOS[0];

  useEffect(() => {
    if (!open) return;
    const onDown = (e) => { if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); window.removeEventListener("keydown", onKey); };
  }, [open]);

  const glass = {
    background: theme === "dark" ? "rgba(30,30,30,0.95)" : "rgba(255,255,255,0.95)",
    backdropFilter: "blur(12px)", WebkitBackdropFilter: "blur(12px)", border: `1px dashed ${pal.border}`,
  };
  return (
    <div ref={rootRef} style={{ position: "fixed", left: "50%", bottom: 0, transform: "translateX(-50%)", zIndex: 9000 }}>
      {open && (
        <div role="menu" aria-label="Демо-сценарий" style={{
          ...glass, position: "absolute", bottom: "calc(100% + 6px)", left: "50%", transform: "translateX(-50%)", width: 220,
          padding: 4, borderRadius: tokens.radius.md, boxShadow: `0 12px 32px ${pal.shadowLg}`, display: "flex", flexDirection: "column",
        }}>
          {SCENARIOS.map(s => (
            <button key={s.value} type="button" role="menuitemradio" aria-checked={s.value === kb.scenario}
              onClick={() => { kb.setScenario(s.value); setOpen(false); }}
              style={{
                ...interactiveBase, textAlign: "left", padding: "7px 10px", borderRadius: tokens.radius.sm, ...tokens.type.sm,
                color: s.value === kb.scenario ? pal.text : pal.textSecondary,
                fontWeight: s.value === kb.scenario ? tokens.weight.medium : tokens.weight.regular,
                background: s.value === kb.scenario ? pal.bgMuted : "transparent",
              }}>{s.label}</button>
          ))}
        </div>
      )}
      <button type="button" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(o => !o)} style={{
        ...interactiveBase, ...glass, borderBottom: "none", display: "inline-flex", alignItems: "center", gap: 6, height: 22, padding: "0 12px",
        borderRadius: `${tokens.radius.sm}px ${tokens.radius.sm}px 0 0`, ...tokens.type.xs, color: pal.textTertiary, whiteSpace: "nowrap",
      }}>
        <span style={{ textTransform: "uppercase", letterSpacing: "0.05em" }}>Демо</span>
        <span style={{ color: pal.textSecondary }}>{current.label}</span>
        <ChevronIcon size={8} direction={open ? "down" : "up"} />
      </button>
    </div>
  );
}

// Presets double as the summary: each tab carries its live count.
export function PresetTabs({ kb }) {
  const pal = usePal();
  return (
    <div role="group" aria-label="Срезы документов" style={{
      display: "inline-flex", flexWrap: "wrap", gap: 2, padding: 3, borderRadius: tokens.radius.md, background: pal.bgSubtle, maxWidth: "100%",
    }}>
      {PRESETS.map(p => {
        const active = kb.preset === p.value;
        const count = kb.presetCounts[p.value];
        const alert = p.value === "attention" && count > 0;
        return (
          <button key={p.value} type="button" aria-pressed={active} onClick={() => kb.setPreset(p.value)}
            style={{
              ...interactiveBase, display: "inline-flex", alignItems: "center", gap: 8, padding: "6px 10px",
              borderRadius: tokens.radius.sm, whiteSpace: "nowrap",
              background: active ? pal.bgElevated : "transparent",
              boxShadow: active ? `0 1px 2px ${pal.shadow}, 0 0 0 1px ${pal.borderSubtle}` : "none",
              ...tokens.type.sm, fontWeight: tokens.weight.medium, color: active ? pal.text : pal.textSecondary,
            }}>
            {p.label}
            <span style={{
              minWidth: 18, padding: "0 5px", borderRadius: tokens.radius.pill, textAlign: "center", boxSizing: "border-box",
              ...tokens.type.xs, fontFamily: tokens.font.mono, fontWeight: tokens.weight.medium,
              background: alert ? pal.dangerBg : "transparent", color: alert ? pal.danger : pal.textTertiary,
            }}>{count}</span>
          </button>
        );
      })}
    </div>
  );
}

export function FiltersPopover({ kb }) {
  const theme = useThemeContext();
  const pal = usePal();
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const active = Object.keys(kb.filters).length;

  useEffect(() => {
    if (!open) return;
    const onDown = (e) => { if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); window.removeEventListener("keydown", onKey); };
  }, [open]);

  return (
    <div ref={rootRef} style={{ position: "relative" }}>
      <Button variant="outline" size="md" aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(o => !o)}
        icon={<SlidersHorizontal size={14} strokeWidth={1.5} />}>
        Фильтры{active ? ` · ${active}` : ""}
      </Button>
      {open && (
        <div role="dialog" aria-label={D.filtersLabel} data-field-stroke={theme === "dark" || undefined} style={{
          "--field-stroke": pal.border, position: "absolute", top: "calc(100% + 6px)", right: 0, zIndex: 30, width: 300, padding: 16, boxSizing: "border-box",
          display: "flex", flexDirection: "column", gap: 12, borderRadius: tokens.radius.md,
          background: theme === "dark" ? "rgba(30,30,30,0.97)" : "rgba(255,255,255,0.97)",
          backdropFilter: "blur(16px)", WebkitBackdropFilter: "blur(16px)",
          border: `1px solid ${pal.borderSubtle}`, boxShadow: `0 12px 32px ${pal.shadowLg}`,
          animation: `halaska-scale-in ${motion.normal} ${motion.emphasized} both`, transformOrigin: "top right",
        }}>
          {FILTER_KEYS.map(key => (
            <Select key={key} size="sm" label={D.filters[key]} value={kb.filters[key] ?? ALL}
              options={[{ value: ALL, label: D.all }, ...FILTER_OPTIONS[key]]}
              onChange={(value) => kb.setFilter(key, value === ALL ? null : value)} />
          ))}
          <div style={{ display: "flex", justifyContent: "space-between", gap: 8, marginTop: 4 }}>
            <Button variant="ghost" size="sm" disabled={!active} onClick={kb.clearFilters}>{D.clearFilters}</Button>
            <Button variant="primary" size="sm" onClick={() => setOpen(false)}>Готово</Button>
          </div>
        </div>
      )}
    </div>
  );
}

export function ActiveFilterChips({ kb }) {
  const entries = FILTER_KEYS.filter(k => kb.filters[k]);
  if (!entries.length) return null;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
      {entries.map(key => (
        <Chip key={key} onToggle={() => kb.setFilter(key, null)} onRemove={() => kb.setFilter(key, null)}>
          {D.filters[key]}: {FILTER_OPTIONS[key].find(o => o.value === kb.filters[key])?.label}
        </Chip>
      ))}
      <Button variant="ghost" size="sm" icon={<X size={13} strokeWidth={1.5} />} onClick={kb.clearFilters}>{D.clearFilters}</Button>
    </div>
  );
}
