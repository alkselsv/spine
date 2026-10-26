import { useEffect, useState } from "react";
import { ThemeProvider, motion, tokens, usePal } from "../halaska-kit";
import { useToasts } from "../components/ui";
import { useViewportWidth } from "../useKnowledge";
import SpineSidebar from "./SpineSidebar";
import { SpineTopBar } from "./SpineHeader";
import SpineDocumentsTable from "./SpineDocumentsTable";
import { SpineDetailsCard, SpineDetailsSheet } from "./SpineDocumentDetails";
import { ReplaceDialog, TombstoneDialog, UploadDialog } from "./SpineDialogs";
import { InterfaceStateBanner } from "./SpineParts";
import { useSpineDocuments } from "./useSpineDocuments";
import SpineChatScreen from "./chat/SpineChatScreen";
import { useSpineChat } from "./chat/useSpineChat";
import QARunScreen from "./runs/QARunScreen";
import QARunsList from "./runs/QARunsList";
import { LIST_PARAMS } from "./runs/qaRunData";
import { ru } from "./ru";

const SHEET_BELOW = 1180;
const COMPACT_TABLE_BELOW = 1520;
const COMPACT_ALWAYS_BELOW = 940;
const SCREENS = ["knowledge", "chat", "qaRuns", "qaRun"];
const TITLES = { knowledge: ru.navigation.knowledge, chat: ru.navigation.chat, qaRuns: ru.qaRuns.title, qaRun: ru.qaRuns.detail.title };
const URL_SCREEN = { chat: "chat", qaRuns: "qa-runs", qaRun: "qa-run" };
const SCREEN_PATH = { knowledge: "/knowledge/documents", chat: "/chat", qaRuns: "/qa/runs" };
const RUN_PARAMS = ["run", "attempt_id", "event_id"];
const navOf = (screen) => (screen === "qaRun" ? "qaRuns" : screen);

function readScreen() {
  if (window.location.pathname.startsWith("/qa/runs/")) return "qaRun";
  if (window.location.pathname === "/qa/runs") return "qaRuns";
  if (window.location.pathname.startsWith("/chat")) return "chat";
  if (window.location.pathname.startsWith("/knowledge")) return "knowledge";
  const value = new URLSearchParams(window.location.search).get("screen");
  return Object.keys(URL_SCREEN).find(k => URL_SCREEN[k] === value) ?? "knowledge";
}

function SpineDocuments({ kb, viewport, dialog, setDialog }) {
  const useSheet = viewport < SHEET_BELOW;
  const doc = kb.selected;
  const detailsProps = doc && {
    doc,
    onClose: () => kb.setSelectedId(null),
    onCopy: (value) => kb.copy(value, ru.documents.detailSourceId),
    onReprocess: () => kb.reprocess(doc.source_object_id),
    onReplace: () => setDialog("replace"),
    onTombstone: () => setDialog("tombstone"),
  };
  const bannerState = ["partial", "stale", "degraded"].includes(kb.interfaceState) ? kb.interfaceState : null;

  return (
    <>
      <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", gap: 16, padding: 24 }}>
        {bannerState && <InterfaceStateBanner state={bannerState} />}
        <div style={{ flex: 1, minHeight: 0, display: "flex", gap: 16 }}>
          <SpineDocumentsTable kb={kb} onUpload={() => setDialog("upload")}
            compact={viewport < COMPACT_ALWAYS_BELOW || (!!doc && !useSheet && viewport < COMPACT_TABLE_BELOW)} />
          {detailsProps && !useSheet && <SpineDetailsCard key={doc.source_object_id} {...detailsProps} />}
        </div>
      </div>

      {detailsProps && useSheet && <SpineDetailsSheet key={doc.source_object_id} {...detailsProps} open={!dialog} />}
      <UploadDialog open={dialog === "upload"} onClose={() => setDialog(null)} onSubmit={kb.upload} />
      <ReplaceDialog open={dialog === "replace"} onClose={() => setDialog(null)} onSubmit={(file) => kb.replace(doc.source_object_id, file)} />
      <TombstoneDialog key={doc?.source_object_id} open={dialog === "tombstone"} onClose={() => setDialog(null)}
        onSubmit={(reason) => kb.tombstone(doc.source_object_id, reason)} />
    </>
  );
}

function SpineShell({ onToggleTheme }) {
  const pal = usePal();
  const { push: toast, host: toastHost } = useToasts();
  const viewport = useViewportWidth();
  const kb = useSpineDocuments(toast, window.innerWidth < SHEET_BELOW ? { initialSelectedId: null } : undefined);
  const chat = useSpineChat(toast, { workspace: kb.workspace, environment: kb.environment });

  const [screen, setScreen] = useState(readScreen);
  const [nav, setNav] = useState(() => navOf(screen));
  const [collapsedBy, setCollapsedBy] = useState(() => ({
    knowledge: window.innerWidth < 1000, chat: true, qaRuns: window.innerWidth < 1100, qaRun: window.innerWidth < 1280,
  }));
  const collapsed = collapsedBy[screen];
  const [dialog, setDialog] = useState(null);
  const [runId, setRunId] = useState(() => {
    const pathRun = window.location.pathname.match(/^\/qa\/runs\/([^/]+)$/)?.[1];
    return pathRun ?? new URLSearchParams(window.location.search).get("run");
  });

  useEffect(() => { document.title = `${TITLES[screen]} · ${ru.shellTitle}`; }, [screen]);

  const navigate = (id) => {
    setNav(navOf(id));
    if (!SCREENS.includes(id)) { toast("В прототипе есть экраны знаний, чата и запусков Q&A"); return; }
    setScreen(id);
    const url = new URL(window.location.href);
    url.pathname = id === "qaRun" && runId ? `/qa/runs/${runId}` : (SCREEN_PATH[id] ?? "/");
    url.searchParams.delete("screen");
    if (id !== "qaRun") RUN_PARAMS.forEach(p => url.searchParams.delete(p));
    if (navOf(id) !== "qaRuns") LIST_PARAMS.forEach(p => url.searchParams.delete(p));
    window.history.replaceState(null, "", url);
  };

  const openDocument = (id) => {
    kb.reveal(id);
    navigate("knowledge");
  };

  const openRun = (messageId) => {
    const message = Object.values(chat.messagesBySession).flat().find(m => m.message_id === messageId);
    if (message?.run_id) openRunById(message.run_id);
    else navigate("qaRuns");
  };

  const openRunById = (id) => {
    setRunId(id);
    setNav("qaRuns");
    setScreen("qaRun");
    const url = new URL(window.location.href);
    url.pathname = `/qa/runs/${id}`;
    url.searchParams.delete("screen");
    window.history.replaceState(null, "", url);
  };

  const backToMessage = (d) => {
    chat.showMessage(d.session_id, d.message_id);
    navigate("chat");
  };

  const openCitationInChat = (d, citationId) => {
    if (d.session_id && d.message_id) chat.showCitation(d.session_id, d.message_id, citationId);
    navigate("chat");
  };

  return (
    <div style={{
      display: "flex", height: "100vh", overflow: "hidden", background: pal.bg, color: pal.text, fontFamily: tokens.font.sans,
      transition: `background ${motion.smooth} ${motion.easeInOut}`,
    }}>
      <SpineSidebar active={nav} collapsed={collapsed} onToggleCollapsed={() => setCollapsedBy(c => ({ ...c, [screen]: !c[screen] }))} onToggleTheme={onToggleTheme} onNavigate={navigate} />

      <main aria-labelledby="page-heading" style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        <SpineTopBar kb={kb} compact={viewport < 1100} title={TITLES[screen]} />
        {screen === "chat" && <SpineChatScreen chat={chat} kb={kb} viewport={viewport} onOpenDocument={openDocument} onOpenRun={openRun} />}
        {screen === "qaRuns" && (
          <QARunsList kb={kb} viewport={viewport} recentRunId={runId} onOpenRun={openRunById} onOpenChat={() => navigate("chat")} onToast={toast} />
        )}
        {screen === "qaRun" && (
          <QARunScreen chat={chat} kb={kb} runId={runId} viewport={viewport} onOpenRun={openRunById} onBackToMessage={backToMessage} onOpenCitation={openCitationInChat}
            onBack={() => navigate("qaRuns")} onNavigate={navigate} />
        )}
        {screen === "knowledge" && <SpineDocuments kb={kb} viewport={viewport} dialog={dialog} setDialog={setDialog} />}
      </main>

      {toastHost}
    </div>
  );
}

export default function SpineApp() {
  const [theme, setTheme] = useState(() => localStorage.getItem("spine.theme") || "light");
  const toggle = () => setTheme(t => {
    const next = t === "dark" ? "light" : "dark";
    localStorage.setItem("spine.theme", next);
    return next;
  });
  return (
    <ThemeProvider theme={theme}>
      <SpineShell onToggleTheme={toggle} />
    </ThemeProvider>
  );
}
