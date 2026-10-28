import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import { ThemeProvider, motion, tokens, usePal } from "../halaska-kit";
import { ScopeContext, scopeQueryKey } from "../../app/scope";
import { DocumentDetailPage } from "../../features/knowledge/DocumentDetailPage";
import { IngestionRunPage } from "../../features/knowledge/IngestionRunPage";
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
import { WorkflowStartPage } from "../../features/workflows/WorkflowStartPage";
import { WorkflowRunPage } from "../../features/workflows/WorkflowRunPage";
import { ru } from "./ru";

const SHEET_BELOW = 1180;
const COMPACT_TABLE_BELOW = 1520;
const COMPACT_ALWAYS_BELOW = 940;
const SCREENS = ["knowledge", "chat", "qaRuns", "qaRun", "ingestionRun", "workflowStart", "workflowRun"];
const TITLES = { knowledge: ru.navigation.knowledge, chat: ru.navigation.chat, qaRuns: ru.qaRuns.title, qaRun: ru.qaRuns.detail.title, ingestionRun: ru.navigation.runs, documentProjection: ru.navigation.knowledge, workflowStart: ru.navigation.workflowStart, workflowRun: ru.navigation.runs };
const URL_SCREEN = { chat: "chat", qaRuns: "qa-runs", qaRun: "qa-run" };
const SCREEN_PATH = { knowledge: "/knowledge/documents", chat: "/chat", qaRuns: "/qa/runs", ingestionRun: "/runs", workflowStart: "/workflow/start" };
const RUN_PARAMS = ["run", "attempt_id", "event_id"];
const navOf = (screen) => screen === "qaRun" ? "qaRuns" : screen === "ingestionRun" ? "runs" : screen === "documentProjection" ? "knowledge" : screen;

function readScreen(pathname, search) {
  if (pathname.startsWith("/workflow/runs/")) return "workflowRun";
  if (pathname === "/workflow/start") return "workflowStart";
  if (pathname.startsWith("/runs")) return "ingestionRun";
  if (pathname.startsWith("/knowledge/documents/") && new URLSearchParams(search).get("tab") === "projection") return "documentProjection";
  if (pathname.startsWith("/qa/runs/")) return "qaRun";
  if (pathname === "/qa/runs") return "qaRuns";
  if (pathname.startsWith("/chat")) return "chat";
  if (pathname.startsWith("/knowledge")) return "knowledge";
  const value = new URLSearchParams(search).get("screen");
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
  const location = useLocation();
  const routerNavigate = useNavigate();
  const pal = usePal();
  const { push: toast, host: toastHost } = useToasts();
  const viewport = useViewportWidth();
  const routeDocumentId = location.pathname.match(/^\/knowledge\/documents\/([^/]+)$/)?.[1] ?? null;
  const kb = useSpineDocuments(toast, {
    initialSelectedId: routeDocumentId ?? (window.innerWidth < SHEET_BELOW ? null : undefined),
  });
  const chat = useSpineChat(toast, { workspace: kb.workspace, environment: kb.environment });
  const scopeContext = {
    scope: { workspaceId: kb.workspace, environmentId: kb.environment },
    queryKey: (resource) => scopeQueryKey(resource, { workspaceId: kb.workspace, environmentId: kb.environment }),
  };

  const [screen, setScreen] = useState(() => readScreen(location.pathname, location.search));
  const [nav, setNav] = useState(() => navOf(screen));
  const [collapsedBy, setCollapsedBy] = useState(() => ({
    knowledge: window.innerWidth < 1000, chat: true, qaRuns: window.innerWidth < 1100, qaRun: window.innerWidth < 1280,
    ingestionRun: window.innerWidth < 1100, documentProjection: window.innerWidth < 1000,
  }));
  const collapsed = collapsedBy[screen];
  const [dialog, setDialog] = useState(null);
  const [runId, setRunId] = useState(() => {
    const pathRun = location.pathname.match(/^\/qa\/runs\/([^/]+)$/)?.[1];
    return pathRun ?? new URLSearchParams(location.search).get("run");
  });

  useEffect(() => { document.title = `${TITLES[screen]} · ${ru.shellTitle}`; }, [screen]);
  useEffect(() => {
    const nextScreen = readScreen(location.pathname, location.search);
    setScreen(nextScreen);
    setNav(navOf(nextScreen));
    const pathRun = location.pathname.match(/^\/qa\/runs\/([^/]+)$/)?.[1];
    if (pathRun) setRunId(pathRun);
  }, [location.pathname, location.search]);

  const navigate = (id) => {
    setNav(navOf(id));
    if (!SCREENS.includes(id)) { toast("В прототипе есть экраны знаний, чата и запусков Q&A"); return; }
    setScreen(id);
    const search = new URLSearchParams(location.search);
    search.delete("screen");
    if (id !== "qaRun") RUN_PARAMS.forEach(p => search.delete(p));
    if (navOf(id) !== "qaRuns") LIST_PARAMS.forEach(p => search.delete(p));
    const pathname = id === "qaRun" && runId ? `/qa/runs/${runId}` : (SCREEN_PATH[id] ?? "/");
    routerNavigate({ pathname, search: search.toString() }, { replace: true });
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
    const search = new URLSearchParams(location.search);
    search.delete("screen");
    routerNavigate({ pathname: `/qa/runs/${id}`, search: search.toString() }, { replace: true });
  };

  const backToMessage = (d) => {
    chat.showMessage(d.session_id, d.message_id);
    navigate("chat");
  };

  const openCitationInChat = (d, citationId) => {
    if (d.session_id && d.message_id) chat.showCitation(d.session_id, d.message_id, citationId, d.run_id);
    setNav("chat");
    setScreen("chat");
    const search = new URLSearchParams(location.search);
    search.set("message_id", d.message_id);
    search.set("run_id", d.run_id);
    search.set("citation_id", citationId);
    search.set("inspector", "citation");
    search.set("return_run_id", d.run_id);
    routerNavigate({ pathname: `/chat/${d.session_id}`, search: search.toString() }, { replace: true });
  };

  return (
    <div data-testid="spine-control-plane" data-presentation-runtime="halaska" style={{
      display: "flex", height: "100vh", overflow: "hidden", background: pal.bg, color: pal.text, fontFamily: tokens.font.sans,
      transition: `background ${motion.smooth} ${motion.easeInOut}`,
    }}>
      <SpineSidebar active={nav} collapsed={collapsed} onToggleCollapsed={() => setCollapsedBy(c => ({ ...c, [screen]: !c[screen] }))} onToggleTheme={onToggleTheme} onNavigate={navigate} />

      <main aria-labelledby="page-heading" style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        <SpineTopBar kb={kb} compact={viewport < 1100} title={TITLES[screen]} />
        <ScopeContext.Provider value={scopeContext}>
          {screen === "chat" && <SpineChatScreen chat={chat} kb={kb} viewport={viewport} onOpenDocument={openDocument} onOpenRun={openRun} />}
          {screen === "qaRuns" && (
            <QARunsList kb={kb} viewport={viewport} recentRunId={runId} onOpenRun={openRunById} onOpenChat={() => navigate("chat")} onToast={toast} />
          )}
          {screen === "qaRun" && (
            <QARunScreen chat={chat} kb={kb} runId={runId} viewport={viewport} onOpenRun={openRunById} onBackToMessage={backToMessage} onOpenCitation={openCitationInChat}
              onBack={() => navigate("qaRuns")} onNavigate={navigate} />
          )}
          {screen === "knowledge" && <SpineDocuments kb={kb} viewport={viewport} dialog={dialog} setDialog={setDialog} />}
          {screen === "documentProjection" && <div style={{ flex: 1, minHeight: 0, overflow: "auto", padding: 24 }}><DocumentDetailPage /></div>}
          {screen === "ingestionRun" && <div style={{ flex: 1, minHeight: 0, overflow: "auto", padding: 24 }}><IngestionRunPage /></div>}
          {screen === "workflowStart" && <div style={{ flex: 1, minHeight: 0, overflow: "auto", padding: 24 }}><WorkflowStartPage /></div>}
          {screen === "workflowRun" && <div style={{ flex: 1, minHeight: 0, overflow: "auto", padding: 24 }}><WorkflowRunPage /></div>}
        </ScopeContext.Provider>
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
