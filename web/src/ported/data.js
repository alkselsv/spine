export const READERS = ["Default reader", "PDF reader", "CSV reader", "JSON reader", "Website reader", "Markdown reader"];
export const CHUNKERS = ["Default chunker", "Fixed size chunker", "Recursive chunker", "Semantic chunker", "Agentic chunker", "Document chunker"];
export const FILETYPES = ["JSON", "PDF", "CSV", "MD", "TXT", "URL"];

export const VIEW_OPTIONS = [
  { value: "all", label: "All content" },
  { value: "completed", label: "Completed" },
  { value: "processing", label: "Processing" },
  { value: "failed", label: "Failed" },
];

export const SORT_OPTIONS = [
  { value: "updatedAt:desc", label: "Last uploaded" },
  { value: "updatedAt:asc", label: "First uploaded" },
  { value: "name:asc", label: "Name A–Z" },
  { value: "name:desc", label: "Name Z–A" },
  { value: "status:asc", label: "Status: processing first" },
  { value: "status:desc", label: "Status: completed first" },
];

export const AGENT_OS_LIST = [
  { id: "os-1", name: "Longer OS name to see how it truncates", url: "https://os.acme.dev", status: "online" },
  { id: "os-2", name: "Support AgentOS", url: "https://support.acme.dev", status: "online" },
  { id: "os-3", name: "Local playground", url: "http://localhost:7777", status: "offline" },
];

export const DATABASES = ["db_name", "knowledge_prod", "knowledge_staging"];

export const ORGS = [
  { id: "org-1", name: "Organization", plan: "Free" },
  { id: "org-2", name: "Acme Labs", plan: "Pro" },
];

export const USER = { name: "Tevin Stuurland", email: "Tevin@agno.com" };

const at = (day, month, hours, minutes) => new Date(2025, month, day, hours, minutes).toISOString();

export const INITIAL_CONTENT = [
  {
    id: "c-01", name: "Filename", filetype: "JSON", status: "completed",
    description: "Description", reader: "Default reader", chunker: "Default chunker",
    metadata: { key: "value=id", source: "upload", owner: "tevin" }, updatedAt: at(21, 2, 10, 11),
  },
  {
    id: "c-02", name: "product-catalog", filetype: "JSON", status: "processing",
    description: "Full product catalog export with SKUs, prices and stock levels.", reader: "JSON reader", chunker: "Document chunker",
    metadata: { key: "value=id", team: "commerce", version: "v4" }, updatedAt: at(21, 2, 10, 9),
  },
  {
    id: "c-03", name: "onboarding-guide", filetype: "PDF", status: "completed",
    description: "Employee onboarding handbook used by the HR assistant.", reader: "PDF reader", chunker: "Semantic chunker",
    metadata: { key: "value=id", team: "people", lang: "en" }, updatedAt: at(20, 2, 18, 42),
  },
  {
    id: "c-04", name: "pricing-2025", filetype: "CSV", status: "completed",
    description: "Pricing tiers and regional discounts for 2025.", reader: "CSV reader", chunker: "Fixed size chunker",
    metadata: { key: "value=id", region: "eu", fiscal: "2025" }, updatedAt: at(20, 2, 9, 5),
  },
  {
    id: "c-05", name: "docs.agno.com", filetype: "URL", status: "completed",
    description: "Public documentation crawled from the docs website.", reader: "Website reader", chunker: "Recursive chunker",
    metadata: { key: "value=id", depth: "3", crawl: "weekly" }, updatedAt: at(19, 2, 16, 30),
  },
  {
    id: "c-06", name: "support-faq", filetype: "MD", status: "failed",
    description: "Frequently asked questions from the support portal.", reader: "Markdown reader", chunker: "Agentic chunker",
    metadata: { key: "value=id", team: "support", error: "timeout" }, updatedAt: at(19, 2, 11, 48),
  },
  {
    id: "c-07", name: "q3-board-report", filetype: "PDF", status: "completed",
    description: "Quarterly board report with financial highlights.", reader: "PDF reader", chunker: "Document chunker",
    metadata: { key: "value=id", confidential: "true", quarter: "q3" }, updatedAt: at(18, 2, 14, 20),
  },
  {
    id: "c-08", name: "api-reference", filetype: "JSON", status: "completed",
    description: "OpenAPI specification of the public REST API.", reader: "JSON reader", chunker: "Default chunker",
    metadata: { key: "value=id", spec: "openapi", version: "3.1" }, updatedAt: at(17, 2, 12, 0),
  },
  {
    id: "c-09", name: "customer-interviews", filetype: "TXT", status: "completed",
    description: "Transcripts of twelve customer discovery interviews.", reader: "Default reader", chunker: "Semantic chunker",
    metadata: { key: "value=id", research: "discovery", count: "12" }, updatedAt: at(16, 2, 15, 37),
  },
  {
    id: "c-10", name: "brand-guidelines", filetype: "PDF", status: "completed",
    description: "Brand voice, tone and visual identity guidelines.", reader: "PDF reader", chunker: "Recursive chunker",
    metadata: { key: "value=id", team: "marketing" }, updatedAt: at(14, 2, 10, 15),
  },
  {
    id: "c-11", name: "release-notes", filetype: "MD", status: "completed",
    description: "Changelog for every product release since 2023.", reader: "Markdown reader", chunker: "Fixed size chunker",
    metadata: { key: "value=id" }, updatedAt: at(12, 2, 9, 50),
  },
];

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

export function formatDate(iso) {
  const d = new Date(iso);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${hh}:${mm}`;
}

export function displayName(item) {
  return item.filetype === "URL" ? item.name : `${item.name}.${item.filetype}`;
}

export function parseMetadata(text) {
  return Object.fromEntries(
    text.split(/[\n,]/).map(s => s.trim()).filter(Boolean).map(pair => {
      const i = pair.indexOf(":");
      return i > 0 ? [pair.slice(0, i).trim(), pair.slice(i + 1).trim()] : [pair, ""];
    })
  );
}
