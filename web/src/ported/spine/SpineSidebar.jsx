import { useState } from "react";
import { Activity, House, Layers, Library, MessageSquare, Moon, PanelLeft, Settings, Sun } from "lucide-react";
import { Avatar, Caption, IconButton, Text, interactiveBase, motion, tokens, usePal, useThemeContext } from "../halaska-kit";
import { ru } from "./ru";

const N = ru.navigation;
const ICON = { size: 15, strokeWidth: 1.25 };

const GROUPS = [
  { items: [{ id: "overview", label: N.overview, icon: House }] },
  { title: N.groupWork, items: [{ id: "workflowStart", label: N.workflowStart, icon: MessageSquare }, { id: "chat", label: N.chat, icon: MessageSquare }, { id: "knowledge", label: N.knowledge, icon: Library }] },
  { title: N.groupObserve, items: [{ id: "qaRuns", label: N.qaRuns, icon: Activity }, { id: "runs", label: N.runs, icon: Layers }] },
];

function NavRow({ icon: Icon, label, active, collapsed, onClick }) {
  const pal = usePal();
  const [hover, setHover] = useState(false);
  const strong = active || hover;
  return (
    <button type="button" onClick={onClick} aria-current={active ? "page" : undefined} aria-label={label} title={collapsed ? label : undefined}
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{
        ...interactiveBase, display: "flex", alignItems: "center", gap: 10, width: "100%",
        padding: collapsed ? "8px 0" : "8px 10px", justifyContent: collapsed ? "center" : "flex-start",
        borderRadius: tokens.radius.sm, textAlign: "left",
        background: active ? pal.bgMuted : hover ? pal.bgHover : "transparent",
      }}>
      <Icon {...ICON} color={strong ? pal.text : pal.textSecondary} style={{ flexShrink: 0 }} />
      {!collapsed && (
        <span style={{
          flex: 1, minWidth: 0, ...tokens.type.sm, fontFamily: tokens.font.sans,
          fontWeight: active ? tokens.weight.medium : tokens.weight.regular,
          color: strong ? pal.text : pal.textSecondary,
          transition: `color ${motion.smooth} ${motion.easeInOut}`,
        }}>{label}</span>
      )}
    </button>
  );
}

export default function SpineSidebar({ active, onNavigate, collapsed, onToggleCollapsed, onToggleTheme }) {
  const pal = usePal();
  const theme = useThemeContext();
  const themeLabel = theme === "dark" ? ru.theme.enableLight : ru.theme.enableDark;
  const themeButton = (
    <IconButton size={30} label={themeLabel} onClick={onToggleTheme}
      icon={theme === "dark" ? <Sun size={14} strokeWidth={1.25} /> : <Moon size={14} strokeWidth={1.25} />} />
  );

  return (
    <aside style={{
      width: collapsed ? 64 : 240, flexShrink: 0, display: "flex", flexDirection: "column",
      background: pal.bgSubtle, borderRight: `1px solid ${pal.borderSubtle}`, overflow: "hidden",
      transition: `width ${motion.smooth} ${motion.emphasized}, background ${motion.smooth} ${motion.easeInOut}`,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, padding: collapsed ? "16px 0 8px" : "16px 12px 8px 16px", flexDirection: collapsed ? "column" : "row" }}>
        <span aria-hidden="true" style={{
          width: 26, height: 26, flexShrink: 0, borderRadius: tokens.radius.sm, display: "inline-flex", alignItems: "center", justifyContent: "center",
          background: pal.text, color: pal.bg, ...tokens.type.sm, fontWeight: tokens.weight.semibold, fontFamily: tokens.font.mono,
        }}>S</span>
        {!collapsed && (
          <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
            <Text size="sm" weight="semibold" truncate style={{ letterSpacing: "-0.01em" }}>{ru.shellTitle}</Text>
            <Caption style={{ ...tokens.type.xs, fontFamily: tokens.font.mono, letterSpacing: "0.08em" }}>{ru.brand}</Caption>
          </div>
        )}
        <IconButton size={30} label={collapsed ? "Развернуть меню" : "Свернуть меню"} onClick={onToggleCollapsed}
          icon={<PanelLeft size={15} strokeWidth={1.25} />} />
      </div>

      <nav aria-label={N.label} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: 8, display: "flex", flexDirection: "column" }}>
        {GROUPS.map((group, gi) => (
          <div key={gi} style={{ marginTop: gi ? 12 : 0 }}>
            {group.title && !collapsed && (
              <div style={{ padding: "4px 10px 8px" }}>
                <Caption style={{ ...tokens.type.xs, textTransform: "uppercase", letterSpacing: 0.4 }}>{group.title}</Caption>
              </div>
            )}
            {group.title && collapsed && <div style={{ height: 1, background: pal.borderSubtle, margin: "4px 12px 10px" }} />}
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {group.items.map(item => (
                <NavRow key={item.id} {...item} collapsed={collapsed} active={active === item.id} onClick={() => onNavigate(item.id)} />
              ))}
            </div>
          </div>
        ))}
        <div style={{ flex: 1 }} />
        <NavRow icon={Settings} label={N.settings} collapsed={collapsed} active={active === "settings"} onClick={() => onNavigate("settings")} />
      </nav>

      <div style={{
        display: "flex", alignItems: "center", gap: 10, padding: collapsed ? "12px 0" : "12px 12px 12px 16px",
        flexDirection: collapsed ? "column" : "row", justifyContent: collapsed ? "center" : "flex-start",
        borderTop: `1px solid ${pal.borderSubtle}`,
      }}>
        <Avatar name={ru.roles.administrator} size={28} />
        {!collapsed && (
          <div style={{ flex: 1, minWidth: 0 }}>
            <Text size="sm" weight="medium" truncate>{ru.roles.administrator}</Text>
          </div>
        )}
        <span title={themeLabel} style={{ display: "inline-flex" }}>{themeButton}</span>
      </div>
    </aside>
  );
}
