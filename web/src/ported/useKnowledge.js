import { useEffect, useMemo, useRef, useState } from "react";
import { AGENT_OS_LIST, CHUNKERS, DATABASES, INITIAL_CONTENT, READERS, displayName, parseMetadata } from "./data";

const PAGE_SIZE = 9;
const STATUS_ORDER = { processing: 0, failed: 1, completed: 2 };

export const EMPTY_FORM = { name: "", description: "", filetype: "PDF", reader: READERS[0], chunker: CHUNKERS[0], metadata: "" };

export function useViewportWidth() {
  const [width, setWidth] = useState(() => window.innerWidth);
  useEffect(() => {
    const onResize = () => setWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return width;
}

export function useKnowledge(toast, { initialSelectedId = "c-01" } = {}) {
  const [os, setOs] = useState(AGENT_OS_LIST[0]);
  const [database, setDatabase] = useState(DATABASES[0]);
  const [items, setItems] = useState(INITIAL_CONTENT);
  const [query, setQuery] = useState("");
  const [view, setView] = useState("all");
  const [sort, setSort] = useState("updatedAt:desc");
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState(initialSelectedId);
  const [refreshing, setRefreshing] = useState(false);
  const timers = useRef([]);

  useEffect(() => () => timers.current.forEach(clearTimeout), []);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    const [key, dir] = sort.split(":");
    const sign = dir === "asc" ? 1 : -1;
    return items
      .filter(it => view === "all" || it.status === view)
      .filter(it => !q || [displayName(it), it.description, ...Object.entries(it.metadata).flat()].some(s => s.toLowerCase().includes(q)))
      .sort((a, b) => {
        const av = key === "status" ? STATUS_ORDER[a.status] : key === "name" ? a.name.toLowerCase() : a.updatedAt;
        const bv = key === "status" ? STATUS_ORDER[b.status] : key === "name" ? b.name.toLowerCase() : b.updatedAt;
        return av > bv ? sign : av < bv ? -sign : (a.updatedAt < b.updatedAt ? 1 : -1);
      });
  }, [items, query, view, sort]);

  const pageCount = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  const safePage = Math.min(page, pageCount);
  const withPageReset = (fn) => (value) => { fn(value); setPage(1); };

  const refresh = () => {
    setRefreshing(true);
    timers.current.push(setTimeout(() => {
      const pending = items.filter(it => it.status === "processing").length;
      setItems(list => list.map(it => it.status === "processing" ? { ...it, status: "completed" } : it));
      setRefreshing(false);
      toast(pending ? `Refreshed · ${pending} item${pending > 1 ? "s" : ""} finished processing` : "Everything is up to date", pending ? "success" : "default");
    }, 900));
  };

  const add = (form) => {
    const name = form.name.trim() || `untitled-${items.length + 1}`;
    const id = `c-${Date.now()}`;
    setItems(list => [{
      id, name, filetype: form.filetype, status: "processing",
      description: form.description.trim(), reader: form.reader, chunker: form.chunker,
      metadata: parseMetadata(form.metadata), updatedAt: new Date().toISOString(),
    }, ...list]);
    setSelectedId(id);
    setView("all");
    setSort("updatedAt:desc");
    setPage(1);
    timers.current.push(setTimeout(() => {
      setItems(list => list.map(it => it.id === id && it.status === "processing" ? { ...it, status: "completed" } : it));
    }, 4000));
    toast(`Uploading ${name} · processing started`);
  };

  const save = (draft) => {
    setItems(list => list.map(it => it.id === draft.id ? { ...draft, updatedAt: new Date().toISOString() } : it));
    toast("Changes saved", "success");
  };

  const selected = items.find(it => it.id === selectedId) || null;

  const remove = () => {
    if (!selected) return;
    setItems(list => list.filter(it => it.id !== selected.id));
    setSelectedId(null);
    toast(`${displayName(selected)} deleted`, "danger");
  };

  const copy = async (text, what = "Text") => {
    try {
      await navigator.clipboard.writeText(text);
      toast(`${what} copied`);
    } catch {
      toast("Clipboard is unavailable", "warning");
    }
  };

  return {
    os, setOs: (o) => { setOs(o); toast(`Connected to ${o.name}`); },
    database, setDatabase: (db) => { setDatabase(db); toast(`Switched to ${db}`); },
    items, processing: items.filter(it => it.status === "processing"),
    query, setQuery: withPageReset(setQuery),
    view, setView: withPageReset(setView),
    sort, setSort: withPageReset(setSort),
    page: safePage, setPage, pageCount, pageRows: visible.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE),
    selected, selectedId, setSelectedId,
    refreshing, refresh, add, save, remove, copy,
  };
}
