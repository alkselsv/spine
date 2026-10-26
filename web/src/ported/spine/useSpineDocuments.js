import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import { createScopedApiClient, SpineApiError } from "../../api/client";
import { useAuth } from "../../app/auth";
import { localizeApiError } from "../../app/errors";
import { useBrowserRuntime } from "../../app/runtime-context";
import { createLifecycleIdempotencyKey, loadLifecycleOperation, submitDocumentTombstone } from "../../features/knowledge/lifecycle-api";
import { loadDocument } from "../../features/knowledge/DocumentDetailPage";
import { loadParsedContent } from "../../features/knowledge/ParsedContentInspector";
import { DEFAULT_PROCESSING_PROFILE, loadProcessingCommand, reprocessDocument } from "../../features/knowledge/processing-api";
import { loadReplacementUploadStatus, submitDocumentRevisionUpload } from "../../features/knowledge/replacement-upload-api";
import { loadRevisionHistory } from "../../features/knowledge/revision-api";
import { createIdempotencyKey, createUploadRows, validateUploadFiles } from "../../features/knowledge/upload";
import { loadBatchStatus, submitDocumentBatch } from "../../features/knowledge/upload-api";
import { FILTER_KEYS, PRESETS, isInFlight, lifecycleOf, matchesPreset } from "./spineData";
import { ru } from "./ru";

const PAGE_LIMIT = 25;
const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
const safeErrorMessage = (error) => {
  const localized = localizeApiError(error);
  return `${localized.title}. ${localized.description}`;
};

async function pollUntil(load, terminal, attempts = 40) {
  let value = await load();
  for (let index = 0; index < attempts && !terminal(value); index += 1) {
    await wait(750);
    value = await load();
  }
  return value;
}

function initialScope(runtime, routeSearch) {
  const search = new URLSearchParams(routeSearch);
  const workspace = search.get("workspace");
  const environment = search.get("environment");
  return {
    workspace: runtime.scope.workspaceOptions.some(o => o.id === workspace) ? workspace : runtime.scope.defaultScope.workspaceId,
    environment: runtime.scope.environmentOptions.some(o => o.id === environment) ? environment : runtime.scope.defaultScope.environmentId,
  };
}

export function normalizeApiDocument(doc) {
  const revision = doc.current_revision;
  return { ...doc, revisions: revision ? [revision] : [], coverage: null, parser: null, segments: [] };
}

function mapLocator(locator) {
  if ("page_number" in locator) return { kind: "page", value: String(locator.page_number) };
  if ("sheet_name" in locator) return { kind: "sheet_row", value: `${locator.sheet_name}:${locator.row_number ?? locator.cell_reference ?? ""}` };
  if ("line_number" in locator) return { kind: "custom", value: `line:${locator.line_number}` };
  return { kind: "custom", value: JSON.stringify(locator) };
}

function resolveInterfaceState(query, enabled) {
  if (!enabled) return "degraded";
  if (query.isPending) return "loading";
  if (query.error instanceof SpineApiError) {
    if (query.error.kind === "authorization") return "permissionDenied";
    if (query.error.kind === "validation") return "validationFailed";
    if (query.error.kind === "unavailable") return "degraded";
    return "failed";
  }
  if (!query.data?.items.length) return "empty";
  if (query.isStale) return "stale";
  if (query.data.items.some(d => d.operational_metadata?.status !== "available")) return "partial";
  return "ready";
}

export function useSpineDocuments(toast, { initialSelectedId = null } = {}) {
  const location = useLocation();
  const navigate = useNavigate();
  const auth = useAuth();
  const runtime = useBrowserRuntime();
  const queryClient = useQueryClient();
  const scopeInitial = useMemo(() => initialScope(runtime, location.search), [location.search, runtime]);
  const [workspace, setWorkspace] = useState(scopeInitial.workspace);
  const [environment, setEnvironment] = useState(scopeInitial.environment);
  const [filters, setFilters] = useState(() => {
    const search = new URLSearchParams(location.search);
    return Object.fromEntries(FILTER_KEYS.flatMap(key => search.get(key) ? [[key, search.get(key)]] : []));
  });
  const [preset, setPresetState] = useState("all");
  const [cursor, setCursor] = useState(() => new URLSearchParams(location.search).get("cursor"));
  const [cursorHistory, setCursorHistory] = useState([]);
  const [selectedId, setSelectedId] = useState(initialSelectedId);
  const [connection, setConnection] = useState("connected");
  const enabled = Boolean(auth.getBearerCredential);
  const scope = useMemo(() => ({ workspaceId: workspace, environmentId: environment }), [workspace, environment]);
  const client = useMemo(() => createScopedApiClient({
    baseUrl: window.location.origin,
    getBearerCredential: auth.getBearerCredential ?? (() => ""),
    scope,
  }), [auth.getBearerCredential, scope]);

  const documentsQuery = useQuery({
    queryKey: ["spine", "ported-documents", workspace, environment, cursor, filters],
    queryFn: async ({ signal }) => {
      const query = { limit: PAGE_LIMIT, ...(cursor ? { cursor } : {}) };
      FILTER_KEYS.forEach(key => { if (filters[key]) query[key] = filters[key]; });
      const result = await client.GET("/api/v1/documents", { params: { query }, signal });
      if (!result.data) throw new Error("The document collection response is unavailable.");
      return { ...result.data, items: result.data.items.map(normalizeApiDocument) };
    },
    enabled,
    retry: false,
    staleTime: 30_000,
  });

  const scoped = documentsQuery.data?.items ?? [];
  const filtered = scoped.filter(d => matchesPreset(d, preset));
  const presetCounts = Object.fromEntries(PRESETS.map(p => [p.value, scoped.filter(d => matchesPreset(d, p.value)).length]));
  const directDocumentQuery = useQuery({
    queryKey: ["spine", "ported-document-detail", workspace, environment, selectedId],
    queryFn: ({ signal }) => loadDocument(client, selectedId, signal),
    enabled: enabled && Boolean(selectedId),
    retry: false,
  });
  const selectedBase = scoped.find(d => d.source_object_id === selectedId)
    ?? (directDocumentQuery.data?.document ? normalizeApiDocument(directDocumentQuery.data.document) : null);
  const revisionsQuery = useQuery({
    queryKey: ["spine", "ported-document-revisions", workspace, environment, selectedId],
    queryFn: ({ signal }) => loadRevisionHistory(client, selectedId, undefined, signal),
    enabled: enabled && Boolean(selectedId),
    retry: false,
  });
  const currentRevision = selectedBase?.current_revision;
  const processingReference = currentRevision?.processing_references?.find(reference => reference.processing_receipt_id);
  const contentQuery = useQuery({
    queryKey: ["spine", "ported-document-content", workspace, environment, selectedId, currentRevision?.source_revision_id, processingReference?.processing_receipt_id],
    queryFn: ({ signal }) => loadParsedContent(
      client,
      selectedId,
      currentRevision.source_revision_id,
      processingReference.processing_receipt_id,
      undefined,
      signal,
    ),
    enabled: enabled && Boolean(selectedId && currentRevision?.source_revision_id && processingReference?.processing_receipt_id),
    retry: false,
  });
  const parsed = contentQuery.data?.data;
  const selected = selectedBase ? {
    ...selectedBase,
    revisions: revisionsQuery.data?.items ?? selectedBase.revisions,
    coverage: processingReference?.coverage ?? null,
    parser: parsed?.parser_name ? `${parsed.parser_name}${parsed.parser_version ? ` ${parsed.parser_version}` : ""}` : null,
    segments: parsed?.status === "ready" ? parsed.items.map(item => ({ locator: mapLocator(item.locator), text: item.text })) : [],
  } : null;

  function writeScope(nextWorkspace, nextEnvironment) {
    const search = new URLSearchParams(location.search);
    search.set("workspace", nextWorkspace);
    search.set("environment", nextEnvironment);
    search.delete("cursor");
    search.delete("processing_cursor");
    navigate({ pathname: location.pathname, search: search.toString() }, { replace: true });
  }

  function writeCollectionQuery(mutator) {
    const search = new URLSearchParams(location.search);
    mutator(search);
    navigate({ pathname: location.pathname, search: search.toString() }, { replace: true });
  }

  function changeScope(next) {
    const nextWorkspace = next.workspace ?? workspace;
    const nextEnvironment = next.environment ?? environment;
    setWorkspace(nextWorkspace);
    setEnvironment(nextEnvironment);
    setCursor(null);
    setCursorHistory([]);
    const deepLinkedDocumentId = location.pathname.match(/^\/knowledge\/documents\/([^/]+)$/)?.[1] ?? null;
    setSelectedId(deepLinkedDocumentId);
    setConnection("connected");
    writeScope(nextWorkspace, nextEnvironment);
  }

  function setFilter(key, value) {
    setFilters(current => {
      const next = { ...current };
      if (value) next[key] = value; else delete next[key];
      return next;
    });
    setCursor(null);
    setCursorHistory([]);
    setSelectedId(null);
    writeCollectionQuery(search => {
      if (value) search.set(key, value); else search.delete(key);
      search.delete("cursor");
    });
  }

  function setPreset(value) {
    setPresetState(value);
    setSelectedId(null);
  }

  function selectDocument(id) {
    setSelectedId(id);
    if (!location.pathname.startsWith("/knowledge/documents")) return;
    navigate({
      pathname: id ? `/knowledge/documents/${id}` : "/knowledge/documents",
      search: location.search,
    }, { replace: true });
  }

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ["spine", "ported-documents", workspace, environment] });
  }

  async function upload(files) {
    if (!validateUploadFiles(files).valid) {
      toast(ru.documents.upload.validationFailed ?? ru.status.validationFailed.description, "warning");
      return;
    }
    try {
      const receipt = await submitDocumentBatch(client, createUploadRows(files), createIdempotencyKey(), new AbortController().signal);
      toast(`${ru.documents.upload.acceptedDescription} · ${files.length}`, "success");
      if (!receipt.terminal) {
        await pollUntil(
          () => loadBatchStatus(client, receipt.run_id, new AbortController().signal),
          status => status.terminal,
        );
      }
      await refresh();
    } catch (error) {
      toast(error instanceof SpineApiError ? error.safeMessage : ru.status.failed.description, "warning");
    }
  }

  async function reprocess(id) {
    const revisionId = (selectedBase?.source_object_id === id ? selectedBase : scoped.find(d => d.source_object_id === id))?.current_revision?.source_revision_id;
    if (!revisionId) return;
    try {
      const receipt = await reprocessDocument(client, {
        expected_current_revision_id: revisionId,
        processing_profile_reference: DEFAULT_PROCESSING_PROFILE,
        source_object_id: id,
        source_revision_id: revisionId,
      }, `document-reprocess-${crypto.randomUUID()}`, new AbortController().signal);
      toast(ru.documents.commands.queued, "success");
      if (["accepted", "processing"].includes(receipt.status)) {
        await pollUntil(
          () => loadProcessingCommand(client, receipt.operation_id, new AbortController().signal),
          status => !["accepted", "processing"].includes(status.status),
        );
      }
      await refresh();
    } catch (error) {
      toast(safeErrorMessage(error), "warning");
    }
  }

  async function replace(id, file) {
    const revisionId = (selectedBase?.source_object_id === id ? selectedBase : scoped.find(d => d.source_object_id === id))?.current_revision?.source_revision_id;
    if (!revisionId || !(file instanceof File)) return;
    try {
      const receipt = await submitDocumentRevisionUpload(client, id, revisionId, file, createIdempotencyKey(), new AbortController().signal);
      toast(ru.documents.documentActions.accepted, "success");
      if (!receipt.terminal) {
        await pollUntil(
          () => loadReplacementUploadStatus(client, receipt.run_id, receipt.item_id, new AbortController().signal),
          status => status.terminal,
        );
      }
      await refresh();
    } catch (error) {
      toast(safeErrorMessage(error), "warning");
    }
  }

  async function tombstone(id, reason) {
    const revisionId = (selectedBase?.source_object_id === id ? selectedBase : scoped.find(d => d.source_object_id === id))?.current_revision?.source_revision_id;
    if (!revisionId) return;
    try {
      const receipt = await submitDocumentTombstone(client, id, {
        deletion_reason: reason,
        expected_current_revision_id: revisionId,
      }, createLifecycleIdempotencyKey("tombstone"), new AbortController().signal);
      if (["accepted_processing_pending", "processing_dispatched"].includes(receipt.status)) {
        await pollUntil(
          () => loadLifecycleOperation(client, receipt.operation_id, new AbortController().signal),
          status => !["accepted_processing_pending", "processing_dispatched"].includes(status.status),
        );
      }
      toast(ru.documents.documentActions.tombstoneCreated, "success");
      await refresh();
    } catch (error) {
      toast(safeErrorMessage(error), "warning");
    }
  }

  async function checkConnection() {
    if (!enabled) { setConnection("failed"); return; }
    setConnection("checking");
    try {
      await client.GET("/api/v1/health/ready");
      setConnection("checked");
    } catch {
      setConnection("failed");
    }
  }

  async function copy(text, what) {
    try { await navigator.clipboard.writeText(text); toast(`${what} скопирован`); }
    catch { toast("Буфер обмена недоступен", "warning"); }
  }

  return {
    workspace, environment, changeScope,
    workspaceOptions: runtime.scope.workspaceOptions,
    environmentOptions: runtime.scope.environmentOptions,
    preset, presetCounts, setPreset,
    filters, setFilter, clearFilters: () => {
      setFilters({});
      setCursor(null);
      setCursorHistory([]);
      writeCollectionQuery(search => {
        FILTER_KEYS.forEach(key => search.delete(key));
        search.delete("cursor");
      });
    },
    hasFilters: Object.keys(filters).length > 0,
    scoped, filtered, pageRows: filtered,
    cursor: cursorHistory.length * PAGE_LIMIT, pageLimit: PAGE_LIMIT,
    hasNext: Boolean(documentsQuery.data?.next_cursor),
    nextPage: () => {
      const next = documentsQuery.data?.next_cursor;
      if (!next) return;
      setCursorHistory(history => [...history, cursor]);
      setCursor(next);
      setSelectedId(null);
      writeCollectionQuery(search => search.set("cursor", next));
    },
    prevPage: () => setCursorHistory(history => {
      if (!history.length) return history;
      const previous = history.at(-1) ?? null;
      setCursor(previous);
      setSelectedId(null);
      writeCollectionQuery(search => {
        if (previous) search.set("cursor", previous); else search.delete("cursor");
      });
      return history.slice(0, -1);
    }),
    inFlight: scoped.filter(isInFlight),
    selected, selectedId: selected?.source_object_id ?? null, setSelectedId: selectDocument,
    scenario: "ready", setScenario: () => {},
    interfaceState: selectedId && directDocumentQuery.error
      ? resolveInterfaceState(directDocumentQuery, enabled)
      : resolveInterfaceState(documentsQuery, enabled),
    retry: () => documentsQuery.refetch(),
    connection, checkConnection,
    upload, reprocess, replace, tombstone, copy,
    reveal: selectDocument,
  };
}
