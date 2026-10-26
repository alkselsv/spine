import { useEffect, useRef, useState } from "react";
import { Archive, Copy, EllipsisVertical, ExternalLink, Info, Keyboard, PanelLeft, PanelLeftClose, Plus, RefreshCw } from "lucide-react";
import { Button, Kbd, Spinner, StatusDot, interactiveBase, motion, tokens, usePal } from "../../halaska-kit";
import { Menu } from "../../components/ui";
import { SpineModal } from "../SpineParts";
import { formatDate, formatRelative } from "../spineData";
import { ru } from "../ru";
import { SUGGESTIONS } from "./spineChatData";
import SpineChatMessage from "./SpineChatMessage";
import SpineChatComposer from "./SpineChatComposer";
import { InspectorPanel, QuestionsOutline } from "./SpineChatInspector";
import { AgentMark, Tip } from "./SpineChatParts";
import { mono } from "./chatStyles";

const C = ru.chat;
const COLUMN = 720;
const RAIL = 256;
const OUTLINE = 220;
const INSPECTOR = 360;
const OUTLINE_FROM = 4;

const SHORTCUTS = [
  { label: "Отправить вопрос", keys: ["Enter"] },
  { label: "Новая строка", keys: ["Shift", "Enter"] },
  { label: C.cancelRequest, keys: ["Esc"] },
  { label: C.closeInspector, keys: ["Esc"] },
  { label: C.createSession, keys: ["⌘", "K"] },
  { label: C.collapseSessions, keys: ["⌘", "."] },
];

const SessionId = ({ id }) => {
  const pal = usePal();
  return <span style={{ fontFamily: tokens.font.mono, fontWeight: tokens.weight.semibold, color: pal.text }}>{id.slice(-8)}</span>;
};

function SessionsRail({ chat, onCollapse }) {
  const pal = usePal();
  return (
    <aside aria-labelledby="sessions-heading" style={{
      width: RAIL, flexShrink: 0, display: "flex", flexDirection: "column", minHeight: 0, borderRight: `1px solid ${pal.borderSubtle}`,
      animation: `halaska-fade-in ${motion.normal} ${motion.easeOut} both`,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 10px 8px 20px" }}>
        <h2 id="sessions-heading" style={{ margin: 0, flex: 1, display: "flex", alignItems: "center", gap: 6, ...tokens.type.sm, fontWeight: tokens.weight.semibold, color: pal.text }}>
          Сессии <span style={{ fontWeight: tokens.weight.regular, color: pal.textTertiary, fontFamily: tokens.font.mono, ...tokens.type.xs }}>{chat.sessions.length}</span>
          <Tip text={C.sessionsDescription}>
            <span tabIndex={0} aria-label={C.sessionsDescription} style={{ display: "inline-flex", color: pal.textTertiary, cursor: "help", borderRadius: 4 }}>
              <Info size={13} strokeWidth={1.5} />
            </span>
          </Tip>
        </h2>
        <button type="button" aria-label={C.collapseSessions} title={`${C.collapseSessions} · ⌘.`} onClick={onCollapse} style={{
          ...interactiveBase, width: 28, height: 28, display: "inline-flex", alignItems: "center", justifyContent: "center", borderRadius: tokens.radius.sm, background: "transparent", color: pal.textSecondary,
        }}><PanelLeftClose size={15} strokeWidth={1.5} /></button>
      </div>
      <div role="list" aria-label={C.sessionsTitle} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "0 8px 12px", display: "flex", flexDirection: "column", gap: 2 }}>
        {chat.sessions.map(s => {
          const active = s.session_id === chat.session?.session_id;
          const archived = s.status === "archived";
          return (
            <button key={s.session_id} type="button" role="listitem" aria-current={active ? "page" : undefined} onClick={() => chat.selectSession(s.session_id)}
              style={{
                ...interactiveBase, display: "flex", flexDirection: "column", gap: 3, padding: "8px 12px", borderRadius: tokens.radius.sm, textAlign: "left",
                background: active ? pal.bgMuted : "transparent",
              }}
              onMouseEnter={(e) => { if (!active) e.currentTarget.style.background = pal.bgHover; }}
              onMouseLeave={(e) => { if (!active) e.currentTarget.style.background = "transparent"; }}>
              <span style={{ display: "flex", alignItems: "center", gap: 6, ...tokens.type.sm, color: pal.textSecondary }}>
                <span style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap" }}>Сессия · <SessionId id={s.session_id} /></span>
                {chat.busySessions.has(s.session_id) && <Spinner size={10} color={pal.textTertiary} />}
              </span>
              <span style={{ display: "flex", alignItems: "center", gap: 6, ...tokens.type.xs, color: pal.textTertiary }}>
                <StatusDot status={archived ? "offline" : "online"} size={6} />
                {archived ? C.sessionArchived : C.sessionActive} · {formatRelative(s.updated_at)}
              </span>
            </button>
          );
        })}
        {chat.sessions.length === 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 4, padding: "8px 12px" }}>
            <span style={{ ...tokens.type.sm, color: pal.textSecondary }}>{ru.status.empty.title}</span>
            <span style={{ ...tokens.type.xs, color: pal.textTertiary, lineHeight: 1.45 }}>{C.sessionsDescription}</span>
          </div>
        )}
      </div>
    </aside>
  );
}

function SessionToolbar({ chat, railOpen, onShowRail, onShortcuts, onOpenRuns, compact }) {
  const pal = usePal();
  const s = chat.session;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 16px 10px 12px", minHeight: 48, boxSizing: "border-box" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flex: 1, minWidth: 0 }}>
        {!railOpen && (
          <button type="button" aria-label={C.showSessions} title={`${C.showSessions} · ⌘.`} onClick={onShowRail} style={{
            ...interactiveBase, width: 28, height: 28, display: "inline-flex", alignItems: "center", justifyContent: "center", borderRadius: tokens.radius.sm, background: "transparent", color: pal.textSecondary, flexShrink: 0,
          }}><PanelLeft size={15} strokeWidth={1.5} /></button>
        )}
        {s ? (
          <>
            <h2 id="session-heading" style={{ margin: 0, padding: "0 4px", ...tokens.type.base, fontWeight: tokens.weight.regular, color: pal.textSecondary, whiteSpace: "nowrap" }}>
              Сессия · <SessionId id={s.session_id} />
            </h2>
            <span style={{
              display: "inline-flex", alignItems: "center", gap: 6, height: 22, padding: "0 8px", borderRadius: tokens.radius.pill, flexShrink: 0,
              border: `1px solid ${pal.borderSubtle}`, ...tokens.type.xs, color: pal.textSecondary,
            }}>
              <StatusDot status={s.status === "active" ? "online" : "offline"} size={6} />
              {s.status === "active" ? C.sessionActive : C.sessionArchived}
            </span>
            {!compact && (
              <span title={`${C.sessionUpdated(formatDate(s.updated_at, true))} · ${C.sessionVersion(s.version)}`}
                style={{ ...tokens.type.sm, color: pal.textTertiary, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", minWidth: 0 }}>
                {C.sessionUpdated(formatRelative(s.updated_at))}
              </span>
            )}
            <Menu align="left" minWidth={260}
              items={[
                { label: `Скопировать ${C.canonicalSessionId.toLowerCase()}`, icon: <Copy size={13} strokeWidth={1.5} />, onClick: () => chat.copy(s.session_id, "Идентификатор сессии") },
                { label: C.refreshSession, icon: <RefreshCw size={13} strokeWidth={1.5} />, onClick: chat.refresh },
                { separator: true },
                { label: ru.navigation.qaRuns, icon: <ExternalLink size={13} strokeWidth={1.5} />, onClick: onOpenRuns },
              ]}
              trigger={(open) => (
                <button type="button" aria-label="Действия с сессией" aria-haspopup="menu" aria-expanded={open} style={{
                  ...interactiveBase, width: 24, height: 24, display: "inline-flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
                  borderRadius: tokens.radius.xs, color: pal.textSecondary, background: open ? pal.bgMuted : "transparent",
                }}><EllipsisVertical size={14} strokeWidth={1.5} /></button>
              )} />
          </>
        ) : (
          <h2 id="session-heading" style={{ margin: 0, padding: "0 4px", ...tokens.type.base, fontWeight: tokens.weight.regular, color: pal.textSecondary }}>{C.sessionsTitle}</h2>
        )}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
        <button type="button" aria-label="Горячие клавиши" title="Горячие клавиши" onClick={onShortcuts} style={{
          ...interactiveBase, width: 30, height: 30, display: "inline-flex", alignItems: "center", justifyContent: "center",
          borderRadius: tokens.radius.sm, border: `1.5px solid ${pal.borderInput}`, background: "transparent", color: pal.textSecondary,
        }}><Keyboard size={14} strokeWidth={1.5} /></button>
        <Button variant="primary" size="sm" loading={chat.creating} icon={<Plus size={13} strokeWidth={1.75} />} onClick={chat.createSession}>
          {chat.creating ? C.creatingSession : C.createSession}
        </Button>
      </div>
    </div>
  );
}

function Centered({ children }) {
  return <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 14, textAlign: "center", padding: "48px 0" }}>{children}</div>;
}

function NeutralThread() {
  const pal = usePal();
  return (
    <Centered>
      <span style={{ ...mono, color: pal.textTertiary }}>{C.threadEyebrow}</span>
      <span style={{ fontSize: 22, fontWeight: tokens.weight.medium, letterSpacing: "-0.02em", color: pal.text }}>{C.noSessionTitle}</span>
      <span style={{ ...tokens.type.base, color: pal.textSecondary, maxWidth: 440, lineHeight: 1.55 }}>{C.noSessionDescription}</span>
    </Centered>
  );
}

function ArchivedThread({ chat }) {
  const pal = usePal();
  return (
    <Centered>
      <span aria-hidden="true" style={{ width: 40, height: 40, borderRadius: 11, background: pal.bgMuted, color: pal.textSecondary, display: "inline-flex", alignItems: "center", justifyContent: "center" }}>
        <Archive size={18} strokeWidth={1.5} />
      </span>
      <span style={{ fontSize: 22, fontWeight: tokens.weight.medium, letterSpacing: "-0.02em", color: pal.text }}>{C.archivedTitle}</span>
      <span style={{ ...tokens.type.base, color: pal.textSecondary, lineHeight: 1.55 }}>{C.archivedDescription}</span>
      <Button variant="primary" size="md" loading={chat.restoring} onClick={chat.restoreSession} style={{ marginTop: 6 }}>
        {chat.restoring ? C.restoringSession : C.restoreSession}
      </Button>
    </Centered>
  );
}

function EmptyThread({ chat, scope }) {
  const pal = usePal();
  return (
    <Centered>
      <AgentMark size={40} />
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <span style={{ fontSize: 22, fontWeight: tokens.weight.medium, letterSpacing: "-0.02em", color: pal.text }}>{C.empty}</span>
        <span style={{ ...tokens.type.base, color: pal.textTertiary, maxWidth: 460, lineHeight: 1.55 }}>
          Ответы опираются только на опубликованные источники области {scope.workspace} · {scope.environment}.
        </span>
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "center", maxWidth: 560 }}>
        {SUGGESTIONS.map(q => (
          <button key={q} type="button" onClick={() => chat.setDraft(q)} style={{
            ...interactiveBase, ...tokens.type.sm, padding: "6px 12px", borderRadius: tokens.radius.pill,
            border: `1px solid ${pal.borderSubtle}`, background: pal.bgSubtle, color: pal.textSecondary,
          }}>{q}</button>
        ))}
      </div>
    </Centered>
  );
}

export default function SpineChatScreen({ chat, kb, viewport, onOpenDocument, onOpenRun }) {
  const pal = usePal();
  const [railOpen, setRailOpen] = useState(() => window.innerWidth >= 1280);
  const [shortcuts, setShortcuts] = useState(false);
  const [activeMsg, setActiveMsg] = useState(null);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);
  const stick = useRef(true);

  const scope = {
    workspace: kb.workspaceOptions.find(w => w.id === kb.workspace)?.label ?? kb.workspace,
    environment: kb.environmentOptions.find(e => e.id === kb.environment)?.label ?? kb.environment,
  };
  const messages = chat.thread;
  const last = messages[messages.length - 1];
  const wide = viewport >= 1180;
  const inspectorOpen = !!chat.inspector;
  const showRail = railOpen && viewport >= 900;
  const sessionId = chat.session?.session_id;

  useEffect(() => { stick.current = true; }, [sessionId]);
  useEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [messages, last?.revealed, last?.stage]);

  const trackActive = () => {
    const el = scrollRef.current;
    if (!el) return;
    const line = el.getBoundingClientRect().top + el.clientHeight / 3;
    const nodes = [...el.querySelectorAll("[data-msg]")];
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 4;
    const current = atBottom ? nodes[nodes.length - 1] : nodes.filter(n => n.getBoundingClientRect().top <= line).pop() ?? nodes[0];
    setActiveMsg(current?.dataset.msg ?? null);
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(trackActive, [messages.length, sessionId]);

  useEffect(() => {
    if (!chat.focusId) return;
    const el = document.getElementById(`msg-${chat.focusId}`);
    if (el) {
      stick.current = false;
      el.scrollIntoView({ block: "start" });
    }
    chat.clearFocus();
  }, [chat, chat.focusId]);

  useEffect(() => {
    const onKey = (e) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); chat.createSession(); }
      if (mod && e.key === ".") { e.preventDefault(); setRailOpen(r => !r); }
      if (e.key === "Escape" && !shortcuts) {
        if (chat.inspector) chat.closeInspector();
        else if (chat.running) chat.stop();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  useEffect(() => { if (sessionId && chat.session?.status === "active") inputRef.current?.focus({ preventScroll: true }); }, [sessionId, chat.session?.status]);

  const jump = (id) => {
    stick.current = false;
    document.getElementById(`msg-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const active = chat.session?.status === "active";
  const inspector = inspectorOpen && <InspectorPanel chat={chat} onOpenDocument={onOpenDocument} onOpenRun={onOpenRun} />;
  const openLatestRun = () => onOpenRun(messages.findLast(m => m.state === "terminal")?.message_id ?? null);

  return (
    <div style={{ flex: 1, minHeight: 0, display: "flex" }}>
      {showRail && <SessionsRail chat={chat} onCollapse={() => setRailOpen(false)} />}

      <section aria-labelledby="session-heading" style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        <SessionToolbar chat={chat} railOpen={showRail} onShowRail={() => setRailOpen(true)} onShortcuts={() => setShortcuts(true)} onOpenRuns={openLatestRun}
          compact={viewport < (showRail ? 1360 : 1100) || (inspectorOpen && viewport < 1500)} />

        <div style={{ flex: 1, minHeight: 0, display: "flex" }}>
          <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
            <div ref={scrollRef} onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; trackActive(); }}
              aria-label={C.messagesTitle}
              style={{
                flex: 1, minHeight: 0, overflowY: "auto", padding: "0 24px 24px",
                maskImage: "linear-gradient(to bottom, transparent 0, #000 40px)", WebkitMaskImage: "linear-gradient(to bottom, transparent 0, #000 40px)",
              }}>
              <div style={{ maxWidth: COLUMN, margin: "0 auto", minHeight: "100%", display: "flex", flexDirection: "column", gap: 40, paddingTop: 28 }}>
                {chat.interfaceState === "loading" ? <Centered><Spinner size={18} /><span>{ru.status.loading.description}</span></Centered>
                  : chat.interfaceState === "degraded" ? <Centered><span>{ru.status.degraded.title}</span><span>{ru.status.degraded.description}</span></Centered>
                  : chat.interfaceState === "failed" ? <Centered><span>{ru.status.failed.title}</span><span>{ru.status.failed.description}</span></Centered>
                  : !chat.session ? <NeutralThread />
                  : !active ? <ArchivedThread chat={chat} />
                  : messages.length === 0 ? <EmptyThread chat={chat} scope={scope} />
                  : messages.map(m => <SpineChatMessage key={m.message_id} m={m} chat={chat} onOpenRun={onOpenRun} />)}
              </div>
            </div>
            {active && (
              <div style={{ padding: "0 24px 16px" }}>
                <div style={{ maxWidth: COLUMN, margin: "0 auto" }}>
                  <SpineChatComposer chat={chat} inputRef={inputRef} />
                </div>
              </div>
            )}
          </div>

          {wide && (inspectorOpen || (active && messages.length >= OUTLINE_FROM)) && (
            <aside aria-label={C.inspectorTitle} style={{
              width: inspectorOpen ? INSPECTOR : OUTLINE, flexShrink: 0, minHeight: 0, boxSizing: "border-box", display: "flex", flexDirection: "column",
              borderLeft: inspectorOpen ? `1px solid ${pal.borderSubtle}` : "1px solid transparent",
              padding: inspectorOpen ? 0 : "28px 20px 16px 0", transition: `width ${motion.smooth} ${motion.emphasized}`,
            }}>
              {inspectorOpen ? inspector : <QuestionsOutline messages={messages} activeId={activeMsg ?? last?.message_id} onJump={jump} />}
            </aside>
          )}
        </div>
      </section>

      {!wide && inspectorOpen && (
        <aside aria-label={C.inspectorTitle} style={{
          position: "fixed", top: 0, right: 0, bottom: 0, width: `min(${INSPECTOR}px, 100vw)`, zIndex: 9500, background: pal.bg,
          borderLeft: `1px solid ${pal.borderSubtle}`, boxShadow: `-12px 0 32px ${pal.shadowLg}`, animation: `halaska-step-in ${motion.normal} ${motion.emphasized} both`,
        }}>{inspector}</aside>
      )}

      <SpineModal open={shortcuts} onClose={() => setShortcuts(false)} title="Горячие клавиши" width={400}>
        {SHORTCUTS.map(s => (
          <div key={s.label} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 24, ...tokens.type.sm, color: pal.text }}>
            <span>{s.label}</span>
            <span style={{ display: "inline-flex", gap: 4 }}>{s.keys.map(k => <Kbd key={k}>{k}</Kbd>)}</span>
          </div>
        ))}
      </SpineModal>
    </div>
  );
}
