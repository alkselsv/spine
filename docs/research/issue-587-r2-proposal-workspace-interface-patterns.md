# Issue #587: UI/UX patterns for the R2 proposal workspace

Status: **research resolution and prototype handoff, not an approved design and
not a frontend implementation**. Sources were checked on 2026-10-25. Product
pages, product documentation, first-party changelogs and first-party screenshots
are treated as evidence. Recommendations below are inferences for Spine, not
claims that a referenced product implements Spine's domain or guarantees.

## Research question and boundary

Which current agent-product and proposal/document-product patterns best support
the R2 manager journey without importing another runtime, making a workflow
canvas the product UI, or copying a third-party interface?

The R2 destination is the workflow in `docs/ROADMAP.md` and
`docs/AGENT_DEVELOPMENT.md`: a manager supplies a request and source material,
reviews requirements, gaps, analogues, pricing basis, a deterministic estimate
and an editable proposal, then explicitly approves one immutable version. Every
material conclusion retains accessible evidence and lineage. The first slice is
smaller:

```text
ClientRequest
  -> normalize_request
  -> extract_requirements
  -> validate_evidence
  -> review_requirements
  -> RequirementsArtifact
```

This note does not require pricing, a proposal editor, Google Sheets or DOCX
export for that first slice. It does not select the final interaction model on
behalf of Product Owner/UX.

## Primary references and verified screenshots/demos

| Product | Primary interface/documentation | First-party screenshots or demo | Patterns actually evidenced |
| --- | --- | --- | --- |
| Stack AI | [Interfaces](https://www.stackai.com/platform/interfaces), [UI walkthrough](https://web.dev.stack-ai.com/academy/deep-dive-into-ui), [Chat Assistant options](https://docs.stack-ai.com/stack-ai/export-options/chat-assistant-and-chatbot), [platform overview](https://www.stack-ai.com/docs/platform-overview) | The [UI walkthrough](https://web.dev.stack-ai.com/academy/deep-dive-into-ui) contains real screenshots of Chat, Form, Batch and API interfaces; the [Interfaces page](https://www.stackai.com/platform/interfaces) contains workflow/interface screenshots, including a document answer and source chunk. [Direct interface screenshot](https://framerusercontent.com/images/PBb9fRyo3TLsT2yDDAMhhNuKd8.png?height=450&width=810). | One workflow can be published as form, chat, batch or API. Forms support required fields, file uploads and conditional logic; chat can show processing steps, attachments, feedback and a loading message; batch results are tabular and downloadable. Analytics exposes execution status, runtime, token use, inputs and outputs. |
| V7 Go | [Cases & Concierge](https://www.v7labs.com/changelog/cases), [Tables](https://www.v7labs.com/tables), [workflow documentation](https://docs.go.v7labs.com/docs/section-1-what-is-a-v7-go-agent), [AI citations](https://docs.go.v7labs.com/docs/ai-citations) | [Cases & Concierge](https://www.v7labs.com/changelog/cases) shows the real case UI with file upload, extracted fields and a collapsible source panel. [Direct case screenshot](https://framerusercontent.com/images/UFYFnTJ1zFXLMbWojqls3g3ziB8.jpg). The [V7 homepage](https://www.v7labs.com/) links an official product video. | Each conversation is a persistent Case; a user uploads a document and delegates work. Extracted structured values link to the exact document section and open beside the result. The table view remains available for structured configuration/results. V7 documents human verification using hyperlinked citations and interactive highlights, selected-cell recompute and feedback. |
| Dify | [Workflow product page](https://dify.ai/), [Human Input](https://www.dify.ai/de/blog/the-human-input-node-bringing-human-judgment-into-automated-workflows), [workflow quick start](https://docs.dify.ai/en/guides/application-orchestrate/creating-an-application), [Human Input API release](https://dify.ai/blog/dify-1.14.1-workflows-become-a-team-asset) | The [Human Input article](https://www.dify.ai/de/blog/the-human-input-node-bringing-human-judgment-into-automated-workflows) shows node configuration, reviewer forms and approval branches. [Direct configuration screenshot](https://assets.dify.ai/cdn-cgi/image/format%3Dauto%2Cwidth%3D736%2Conerror%3Dredirect/official/blog/2026/03/01e1b864cb29612a.png). The [homepage](https://dify.ai/) shows Workflow Studio, Knowledge Pipeline and monitoring screenshots. | User Input supports typed fields and file lists. A Human Input node pauses execution, sends a form, shows upstream data, accepts edits/comments, exposes named decision buttons and resumes the selected branch. It supports timeout/end or timeout routing. External systems can query pending tasks, fetch form data and submit a result through the Human Input service API. |
| Relevance AI | [Workforces](https://relevanceai.com/docs/get-started/core-concepts/workforces), [workforce tasks and states](https://relevanceai.com/docs/sdk/workforces), [Approval Mode](https://relevanceai.com/docs/build/agents/give-your-agent-tasks/approval-mode), [Tasks timeline changelog](https://relevanceai.com/docs/changelog#timeline-view-added-to-tasks-page) | The first-party [changelog](https://relevanceai.com/docs/changelog#timeline-view-added-to-tasks-page) shows the Tasks page with Approvals, Escalated and Errors timelines and a mixed-status task list. [Direct screenshot](https://relevanceai.com/docs/images/_opt/changelog/changelog-taskops-timeline.webp). | A task can be `running`, `completed`, `error` or require `action` because it is pending approval/escalated. Workforce task streams identify sub-agent execution and handovers. Tasks have dedicated approval/escalation/error queues and timelines. Approval can be configured per tool and directed to permitted team members. |
| PandaDoc | [proposal product](https://www.pandadoc.com/proposal-software/), [version history](https://support.pandadoc.com/en/articles/9714571-version-history), [approval workflow](https://support.pandadoc.com/en/articles/9714799-approval-workflow-classic-experience), [contract negotiation](https://support.pandadoc.com/en/articles/9714812-contract-negotiation-new-experience) | [Proposal product screenshots](https://www.pandadoc.com/proposal-software/) show the editor and interactive pricing table. Direct screenshots: [editor](https://images.ctfassets.net/a4zep9yar86b/4LuR6Kle6v2zDIs351DsLx/a57cd34047adb4ad647030082a57e7d3/drag-and-drop-editor-2x-620x430.png), [pricing table](https://images.ctfassets.net/a4zep9yar86b/4vxua8knW6qykvaGyeaabR/843e8396de4336b7f6a92c301de196ba/interactive-pricing-tables-620x430.png). Help pages include UI screenshots for version restore, suggestions and approval. | A document-centric editor combines reusable content, editable document blocks and an interactive quote/pricing table. Version history lets a user inspect and restore versions. Suggestions retain author/acceptor/time and can be accepted or rejected. Approval routes a document to named reviewers, records approve/reject comments and activity, and only the approved document becomes ready to send. |
| Agno Control Plane | [product page](https://www.agno.com/products/control-plane), [Control Plane guide](https://docs.agno.com/agent-os/control-plane), [governance overview](https://docs.agno.com/features/control-plane), [tracing](https://docs.agno.com/agent-os/tracing/overview) | The [product page](https://www.agno.com/products/control-plane) contains real screenshots for sessions, traces, chat, evaluations, approvals, guardrails and audit. Direct screenshots: [sessions](https://www.agno.com/assets/cp-sessions-CdAw7Xn8.png), [approvals](https://www.agno.com/assets/act-approvals-DJToB2Vy.png). | Sessions group runs; streamed workflow steps expose progress. Trace tree/waterfall views show model/tool/workflow operations, timing and errors. Approval UI inspects arguments, pauses execution and records approve/reject/edit resolution. Runtime selection is persistent in the header. |

### Existing Spine UI decisions

These are already repository decisions, so the prototype should adapt rather
than introduce a parallel design system:

- `docs/INTERFACE.md` treats Agno as the target **control and observability
  pattern**, not as a runtime dependency. The browser must talk to Spine API,
  not directly to Agno, Cognee or Temporal.
- `docs/INTERFACE.md` and `docs/design/issue-165-control-plane-shell.md` use
  Halaska patterns as a presentation reference, decomposed into Spine-owned
  components and styles. The upstream [Halaska UI repository](https://github.com/Halaska-Studio/ui)
  and [live catalogue](https://ui.halaska.com/) document agent status, approval,
  handoff, action-receipt, error-repair, stepper, data-table and compare patterns.
  Halaska's own site distinguishes Chat (conversation is the product) from
  Canvas (the shared work object is the product). R2 is closer to the latter,
  but must not adopt Halaska's runtime or single-file kit wholesale.
- [Base UI](https://base-ui.com/react/overview/about) remains the headless
  primitive layer. Its [accessibility guide](https://base-ui.com/react/overview/accessibility)
  covers keyboard interaction, ARIA, labels and focus management; Spine still
  owns visible focus, contrast, responsive layout and domain semantics.
- Current `web/` already has `AppShell`, workspace/environment URL context,
  `StatusPanel`/`ApiErrorPanel`, upload/ingestion UI, document/revision/content
  inspection, Q&A sessions, current-authorized citation inspection, feedback,
  cancellation, Q&A run lists/details, attempts, timeline and safe operational
  correlation. Those contracts should be extended, not duplicated.

## Platform pattern matrix and Spine fit

Legend: **strong** = evidenced pattern maps directly; **partial** = useful but
needs a Spine-owned contract; **no** = not evidenced or inappropriate.

| Need | Stack AI | V7 Go | Dify | Relevance AI | PandaDoc | Agno | Spine inference |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Request + files | **strong** form/chat uploads | **strong** Case upload | **strong** typed User Input + file list | partial agent task; workforce SDK says no workforce attachments | **strong** document/file start | partial chat/workflow input | Start with a bounded intake form, not a blank chat. |
| Structured requirements | partial form/output | **strong** structured side panel/table | partial typed variables/form | partial task result | partial document fields | partial structured workflow input | Requirements must be a typed, editable artifact; a transcript is supplementary. |
| Missing information / questions | partial conditional form | partial follow-up Case | **strong** Human Input fields/branches | **strong** escalation/action state | partial comments/reject | **strong** approval/edit | Show gaps as first-class rows with answer fields and explicit resume action. |
| Evidence inspection | partial source chunk | **strong** link + exact highlight + side panel | partial retrieval/debug | no proposal-specific evidence inspector evidenced | no claim-level source evidence evidenced | partial source/run trace | V7 is the closest end-user reference; reuse Spine's fail-closed citation inspector. |
| Historical analogues | partial batch/table | **strong** Case/table/hyperlinks | partial knowledge results | partial knowledge/task | partial content library/templates | partial knowledge | Use cards/table with relevance reason, revision/freshness and source; never treat an old proposal as truth. |
| Pricing basis | no domain guarantee | partial structured table, not authoritative pricing | no deterministic finance guarantee | no deterministic finance guarantee | **strong** quote/pricing editor UX | no | Borrow edit affordances, not arithmetic or authority. Every row needs immutable pricing evidence and server-computed `Decimal` totals. |
| Proposal editing | partial form output | partial generated artifacts | partial editable Human Input | partial task output | **strong** document editor | no | PandaDoc is the primary editing reference, but R2 should build a bounded proposal editor, not a universal document editor. |
| Version history / diff | current Stack AI changelog documents shared drafts/version history, but it post-dates the older interface guide | stale/generated sample warnings, configuration history in changelog | workflow versions, not proposal-version UX | eval/run records, not document diff | **strong** version history and suggestion audit | Studio versions/traces, not proposal diff | Spine needs immutable artifact versions, compare and optimistic locking; no platform supplies the whole contract. |
| Explicit approval | approval gates/HITL | human verification steps | **strong** pause/edit/decision/timeout | **strong** per-tool approval and action queue | **strong** document approval/order/comment | **strong** paused tool approval | Approval must name the exact artifact version and expected version; it is not a generic “looks good” button. |
| Run status/history | **strong** analytics/logs | persistent Case and run transcript | debug/logs | **strong** tasks, handovers, status | document activity/audit | **strong** sessions/traces | Reuse Spine Runs, timeline, attempts and safe diagnostics; keep manager view shallower than engineering trace. |
| Failure/access states | logs/failure messages; connection health | cited output and current changelog evidence for stale samples/permission errors | error branches and timeout | error/escalation/queue | stuck/rejected/edit-after-send states | status/error/authorization | Spine's own taxonomy is stricter and remains authoritative. |

### What should not be copied

1. **Builder canvas as the manager's workspace.** Stack AI, Dify, Relevance and
   Agno use canvases to configure automation. Spine workflows remain code-first;
   the manager reviews typed business artifacts, not nodes and prompts.
2. **Third-party runtime/domain state.** No referenced product may own Spine's
   `ClientRequest`, `RequirementsArtifact`, estimate, approval, evidence or run
   identity. No Agno/Dify/Relevance runtime dependency is justified.
3. **Chat as the only handoff.** A persistent conversation helps clarify, but it
   cannot replace versioned requirements, editable estimate lines, proposal
   sections or an approval decision.
4. **Exposed chain-of-thought.** Some platforms expose “thinking” or detailed
   traces. Spine shows stage progress, tool/action receipts, structured summaries,
   evidence and safe diagnostics, never hidden chain-of-thought.
5. **Client-send/e-sign/analytics in the first slice.** PandaDoc's delivery,
   signature and recipient analytics are beyond R2's current send boundary.
6. **Editable AI totals or inferred prices.** PandaDoc-like table interaction is
   useful, but authoritative pricing sources and deterministic server-side money
   rules remain mandatory.

## R2 manager journey mapping

| Manager action | Recommended interaction | Required trust/control detail |
| --- | --- | --- |
| Submit request and files | “New proposal” intake: customer/project context, desired outcome, constraints, drag/drop files, per-file status | Scope is visible; accepted/rejected files are separate; submission uses one idempotency key. |
| Review extracted requirements | Editable requirements table/list grouped by outcome, scope, constraints and delivery | Each row shows provenance, extraction status and artifact version. |
| See missing information | Persistent “Needs input” section and stage badge, never buried in chat | Gap has reason, blocking effect and suggested question; unknown is not silently filled. |
| Answer clarification | Focused form/drawer beside the requirement; save draft, then “Resume run” | Human answer becomes a new input revision with actor/time and upstream linkage. |
| Review historical analogues | Ranked cards/table with source title, approved date, freshness, similarity/relevance reason and exclusions | Analogue is evidence, not a template or current price authority. Inaccessible/stale analogues cannot support output. |
| Inspect evidence and price source | Right-side evidence inspector opened from a row/claim; exact locator, source revision, freshness/access status | Current authorization is checked on open; unavailable evidence hides content and blocks affected acceptance. |
| Edit scope and estimate | Spreadsheet-like bounded table: line, quantity/unit, source basis, assumptions, tax/currency/rounding summary | Money is recalculated by deterministic backend rules; edited inputs and computed outputs are visually distinct. |
| Review/correct proposal draft | Document preview/editor with outline and section blocks; comments/rework action | Track human edits separately from agent draft; link material claims/price lines to evidence. |
| Compare versions | Side-by-side or inline semantic diff with version metadata and filters for text/estimate/evidence | Diff identifies author/cause and exact base/head; never compare mutable “latest” implicitly. |
| Send for approval | Explicit action opens summary: version, unresolved warnings, evidence health and approver | Submission freezes an immutable candidate version; unresolved blocking gaps prevent submission. |
| Approve a version | Focused approval dialog/work item with approve, reject/request changes and comment | Decision names artifact/version and expected version; stale conflict forces reload/review. Approval does not send externally. |
| Inspect agent run | Collapsible stage strip in workspace; link to full Runs detail/timeline | Show statuses, attempts, artifact lineage, versions, safe error codes, cancellation and trace ID. |

## Required non-happy-path states

The repository taxonomy is the source of truth. The prototype must show these
states in the same synthetic scenario, not as disconnected component samples.

| State | R2 behavior |
| --- | --- |
| `loading` / running | Preserve page geometry with skeletons; show named current stage and elapsed state. Announce meaningful changes through a polite live region. A spinner alone is insufficient for a long run. |
| `partial` | Keep usable accepted files/requirements visible; name failed/omitted items and the effect on completeness. Never style partial as “complete”. |
| `stale` | When a source, artifact or projection version changes, show the pinned version and newer version, make consequential actions read-only, and require refresh/rebase. A `409` optimistic-lock conflict is a stale outcome, not generic failure. |
| `unavailable` | An evidence reference may remain as safe metadata, but its excerpt/content is hidden. Explain whether it was revoked, tombstoned, superseded or otherwise unavailable only through authorized stable reason codes. Block approval if the missing evidence is required. |
| `permission_denied` | Render a safe denial without protected title, excerpt, existence hint or raw backend text. Keep workspace/environment and trace ID visible when safe. |
| `degraded` | Name the affected capability (for example event stream or retrieval), keep canonical saved artifacts readable when authorized, and explain whether polling/retry is active. Do not imply evidence completeness. |
| `failed` | Identify failed stage, safe retryability and preserved prior artifact. Offer retry/new run according to policy with idempotency and lineage; never silently repeat a mutating action. |
| `cancelled` | Treat as terminal. Show who/what cancelled and when, keep the last valid artifact read-only, and make retry create a new linked run rather than resurrecting the cancelled run. |
| `empty` | Explain which input is missing and offer the one relevant action; do not show an empty editor that looks like an agent result. |

## Information-architecture options

Transition counts below use one shared synthetic scenario and count changes of
full-screen/stage context, not opening an inspector drawer. They are hypotheses
for the bounded prototype, not measured production facts.

### Option A — Wizard / guided workflow

Information architecture:

```text
Proposal list
  -> Request & files
  -> Requirements & clarification
  -> Analogues
  -> Estimate
  -> Proposal draft
  -> Version review
  -> Approval
  -> Completion / run link
```

Primary screens are one page per stage, with a global stage stepper, Back/Next,
save status, unresolved count and stage-level validation. Evidence opens in a
drawer so source inspection does not discard current form state. Human review is
a dedicated requirements step and approval is a terminal step.

For the synthetic end-to-end scenario this is **8 forward screen transitions**
after opening a proposal; correcting one requirement after reaching the draft
adds at least two back/forward transitions. A normal run is therefore about
8–10 transitions, plus inspector overlays.

Advantages:

- strong orientation and progressive disclosure for an infrequent manager;
- validation/gating boundaries are obvious;
- mobile can render the same stages as single-column pages;
- the first requirements slice can ship as the first two stages.

Costs and risks:

- evidence, estimate and draft context become separated; cross-checking creates
  backtracking or duplicated summaries;
- non-linear rework is awkward once the agent revises an earlier artifact;
- “Next” can falsely imply completeness when the workflow is partial/stale;
- long documents and estimates are uncomfortable inside step-sized forms;
- weak reuse of Spine's run/evidence detail unless these become nested drawers.

Desktop: centered 720–960 px work column plus evidence drawer. Mobile: one stage
per screen, sticky action footer, attachment/status sheets. Accessibility: a real
ordered stepper with current/completed text, focus moved to the stage heading,
validation summary linked to fields, no status conveyed by color alone.

### Option B — Document-centric workspace

Information architecture:

```text
Proposal list / Work Queue
  -> Proposal workspace
       header: identity, version, workflow state, owner
       left: request/materials + stage/artifact navigation
       center: active requirements/estimate/document artifact
       right: evidence/analogue/run inspector
       bottom/top action bar: save, request changes, submit, approve
  -> Full run detail (secondary)
```

The proposal is the stable route. Selecting a stage changes the central artifact
without losing sources or workflow state. Version compare and approval are
overlays or focused modes. For the same scenario there is **one route transition**
from queue to workspace, approximately **6–8 in-place stage/inspector selections**,
and an optional second route transition to full run diagnostics.

Advantages:

- keeps artifact, evidence, version and run state in one mental context;
- best fit for long-form editing and estimate cross-checking;
- supports non-linear rework and version comparison;
- reuses Spine shell, URL state, evidence inspector and Runs patterns naturally.

Costs and risks:

- high initial information density and harder empty/loading layout;
- resizable three-pane behavior and keyboard order require care;
- incomplete stages can be overlooked without a strong stage rail and gates;
- on mobile, simultaneous artifact/evidence context necessarily becomes
  sequential, reducing the core workspace advantage.

Desktop: 3 panes with independently scrollable center/inspector only when focus
order remains logical; allow the inspector to collapse. Mobile: one primary
artifact, stage selector at top, evidence/run as full-height sheet, sticky save
and review actions; proposal authoring is supported for corrections, not full
page-layout design. Accessibility: landmarks and headings for panes, skip links,
named resize/collapse controls or fixed layouts, table cell edit instructions,
focus restored to the triggering claim when inspector closes.

### Option C — Recommended prototype hypothesis: staged artifact workspace

Prototype a hybrid: **the workspace is the stable route; a guided stage rail and
focused review tasks are the navigation model**.

```text
Work Queue / Proposals
  -> Proposal workspace
       [Request] [Requirements] [Analogues] [Estimate] [Draft] [Approval]
       stage status: not started / running / needs input / ready / approved
       center: exactly one active typed artifact
       right: contextual evidence, analogue or run inspector
       focused modal/sheet: clarification, version compare, submit, approve
```

Why this is the leading hypothesis:

- V7 validates persistent case + structured artifact + adjacent source
  inspection; PandaDoc validates document/quote/version/approval focus; Dify
  validates bounded human forms; Agno/Relevance validate run/approval context.
- It preserves guided orientation without forcing linear navigation after
  revisions begin.
- It reuses the existing Spine shell and current-authorized inspectors and keeps
  the first requirements slice visibly bounded.

This is a recommendation for the prototype, not the approved R2 design. The
prototype should compare it with a pure wizard using the same scenario and
measure orientation, source verification, rework cost and approval confidence.
For the shared scenario, expect **one route transition** into the workspace,
**5–7 in-place stage selections**, and an optional second route transition to
full run diagnostics. Clarification, diff and approval open as focused modes and
do not discard the workspace context.

## Two variants to prototype

1. **Guided review wizard.** One page each for Request, Requirements,
   Analogues/Estimate, Draft/Version and Approval. Use a persistent stepper and
   evidence drawer. The prototype can combine later steps while retaining the
   six conceptual stages.
2. **Staged artifact workspace.** Persistent workspace header/stage rail, center
   artifact, contextual right inspector, focused clarification/version/approval
   modes. This is the leading hypothesis.

### Shared synthetic scenario

“ООО Север” requests an on-premise customer-support knowledge assistant for 120
employees, supplies a two-page request PDF and security questionnaire, but omits
the support SLA and data-migration volume. The agent extracts eight requirements;
one is ambiguous and two need answers. The manager:

1. opens the work item and checks both uploaded files;
2. verifies a security requirement against page 2 of the questionnaire;
3. answers SLA = 8x5 and migration volume = 75 GB;
4. accepts seven requirements, edits one wording and resumes the run;
5. later inspects two historical analogues, rejects the stale one, checks the
   approved pricing basis, changes one quantity, reviews the server-recalculated
   total, edits a draft section, compares versions and submits version 3;
6. the approver rejects version 3 with a comment, the manager creates version 4,
   and the approver approves exactly version 4;
7. the manager opens run history to confirm the clarification and approval
   lineage. No document is sent to a client.

For the first-slice prototype, stop after step 4.

## Minimal UI for the first R2 agent slice

Only two new primary screens are necessary; existing Runs can remain a linked
secondary screen.

### 1. New request

- client/project label, requested outcome, free-text request and constraints;
- file upload with type/size validation and per-file accepted/rejected/progress;
- visible workspace/environment;
- “Extract requirements” action with submission/idempotency state;
- empty, uploading, partial-upload, denied and failed variants.

### 2. Requirements review

- header: request, run/stage status, artifact version and last update;
- editable requirement rows with type/category, normalized wording and
  accepted/edited/rejected state;
- separate ambiguities and missing-information groups with answer fields;
- evidence marker on every evidence-bearing requirement and a fail-closed
  evidence drawer with revision, locator and freshness/access status;
- save draft, request re-extraction, cancel active run and “Accept requirements”;
- acceptance creates a reviewed `RequirementsArtifact` version; it does not
  proceed to analogues/pricing/draft in this slice;
- link to existing run detail/timeline rather than embedding engineering trace.

Optional list entry: add an R2 item to the existing Work Queue/Runs surface only
if discoverability cannot be tested through a direct route. Do not build a
proposal dashboard just for this slice.

### Minimum API interactions (contracts to specify, not implemented here)

Endpoint names are illustrative; the specification should choose the final
resource names from the domain model.

| Interaction | Minimum contract |
| --- | --- |
| Create request | Command accepting typed `ClientRequest`, workspace/environment and idempotency key; returns request ID, version and accepted state. |
| Attach material | Reuse the existing upload/ingestion contract where possible, then create an explicit request-to-`SourceRevision` link; return per-file acceptance/processing independently. |
| Start extraction | Idempotent command pinned to request version, capability/agent/workflow version and authorized context profile; returns run/artifact correlation. |
| Observe | Canonical GET for request/run plus resumable SSE stage events and polling fallback; terminal state is rehydrated from GET. |
| Read artifact | Versioned `RequirementsArtifact` with requirements, ambiguities, missing information, evidence refs, upstream request/source revisions and review state. |
| Inspect evidence | Extend/reuse the current citation inspection semantics: current authorization, stable locator, exact source revision, `available | unavailable`, no protected leak. |
| Save/decide review | Command with `expected_version`, idempotency key, field-level edits/answers, decision (`accept | request_changes`) and comment; returns immutable reviewed artifact/version or typed conflict. |
| Cancel/retry | Reuse existing cancellation/idempotency patterns; retry creates a linked new run and pins the revised request/artifact. |

The first slice does **not** need analogue, pricing, estimate, proposal document,
version-diff or export APIs. Their visual placeholders should not masquerade as
working controls.

## Mapping to the current Spine web shell

| R2 need | Reuse/adapt now | R2-owned missing component/contract |
| --- | --- | --- |
| Workspace/environment and navigation | `AppShell`, scope query parameters, scoped generated client, auth boundary | Proposal/work-item route and navigation label; preserve scope in every deep link. |
| Request files | `DocumentUpload`, ingestion status/history, document/revision detail | Link an accepted `SourceRevision` to `ClientRequest`; request-level batch summary. |
| Global states/errors | `StatusPanel`, `ApiErrorPanel`, localized stable codes and trace ID | Add explicit `unavailable` and `cancelled` presentation where domain-specific; stale conflict banner/actions. |
| Requirements editor | Base UI form primitives, Spine CSS tokens, existing safe Markdown where needed | `RequirementsReview`, editable requirement row, gap/question group, dirty/save/conflict state. |
| Evidence | `AcceptedAnswer` citation placement, `inspectAcceptedCitation`, `ParsedContentInspector` | Generalized artifact-evidence trigger/inspector not coupled to Q&A answer IDs; required-evidence gate. |
| Run progress | `QARunsPage`, `QARunDetailPage`, timeline, attempts, diagnostics, cancellation | Proposal run projection and a manager-level stage summary; do not expose raw trace by default. |
| Feedback/rework | `FeedbackControls` interaction patterns | Typed review decision, revision comment and request-changes command tied to artifact version. |
| Version/approval (later) | Existing status badges/timeline and Base UI dialog primitives | Artifact version list, semantic diff, optimistic-lock conflict, approval work item and immutable decision receipt. |
| Estimate/document (later) | App shell and inspector layout only | Bounded estimate grid, deterministic calculation receipt, structured proposal editor/preview. |

Halaska patterns that can inform appearance are Stepper, Data Table, Approval
Card, Agent Status, Action Receipt, Error Repair, Sheet and Compare Slider.
Because Halaska components are partial/reference implementations and the
repository already uses Base UI, use them as visual/interaction guidance rather
than importing a second primitive layer.

## Missing API/frontend contracts before implementation

1. Versioned schemas for `ClientRequest`, `Requirement`,
   `RequirementsArtifact`, ambiguity/missing-information items and review edits.
2. Request-material links to immutable `SourceRevision` IDs, with partial upload
   and deletion/tombstone effects.
3. Proposal workflow/run read model: stage, wait reason, current artifact IDs,
   actor/owner, retryability, timestamps and terminal outcome.
4. General artifact-evidence inspection contract with current authorization,
   freshness, exact locator and unavailable behavior.
5. Human review command/receipt: decision, actor, comment, immutable input/output
   artifact versions, expected-version conflict and idempotency.
6. Clarification input contract: typed answers become a new `ClientRequest`
   revision and resume/new-run lineage; they are not appended only to chat.
7. Later-slice artifact version list and semantic diff by requirement, estimate
   line, proposal section and evidence change.
8. Later-slice deterministic estimate calculation/validation receipt and
   separation of editable inputs from computed values.
9. Later-slice approval work item/decision that pins the exact candidate version,
   blocks stale approval and never implies external delivery.
10. Frontend route/URL-state contract for selected stage, artifact version,
    inspector target and compare versions; all must preserve scope.

## Accessibility and responsive acceptance for the prototype

- Full keyboard completion of request submission, requirement edits, evidence
  open/close, clarification and acceptance; no hover-only action.
- Visible focus and meaningful accessible names; form error summary focuses the
  first invalid field and errors are associated with fields.
- Stage/status changes announced without streaming every token. Status, evidence
  health and diff do not rely on color alone.
- Tables provide row/column context and an escape path; if grid editing is not
  reliably accessible, use a list of row forms in the prototype.
- Desktop test at approximately 1280 px; narrow test at approximately 390 px.
  On narrow screens evidence and run details become labelled sheets/full-screen
  views, not squeezed sidebars.
- Approval and destructive/rework dialogs trap focus, have title/description,
  close on Escape where safe, and return focus to the trigger. Base UI supplies
  primitive behavior, while Spine supplies content and visible focus/contrast.
- Respect reduced motion; do not animate progress as proof that work is healthy.

## Product Owner / team lead decision questions

1. Is the stable object the **proposal workspace** or the **current workflow
   step**? This decides hybrid/workspace versus wizard as the primary IA.
2. In the first slice, may a manager edit extracted requirement wording directly,
   or only accept/reject and provide corrections for regeneration?
3. Which fields are manager-editable in each later artifact, and which require a
   new agent/code-step run instead of an inline edit?
4. Which missing-information/evidence conditions block requirements acceptance,
   proposal submission and final approval respectively?
5. Is the same person allowed to author and approve, or must owner/approver roles
   be distinct for some workspaces/deal values?
6. What exactly constitutes the approved object: structured artifact version,
   rendered preview/export version, or both linked by a deterministic render
   receipt?
7. Should mobile support full editing or only intake, clarification, evidence
   review and approval? The recommended default is bounded corrections/review,
   not page-layout authoring.
8. Must clarification be an inbox/work-queue assignment as well as an in-workspace
   form? Who owns it, and what are the deadline/escalation semantics?
9. Which diff granularity is decision-useful: field/line/section, rendered page,
   or both? Which changes always require re-approval?
10. What manager-level run detail is useful by default, and which diagnostic
    fields should remain behind the existing Runs view?
11. Is workspace stage navigation permitted before prerequisites complete, and
    if so are future artifacts hidden, empty or read-only previews?
12. Which synthetic scenario and usability signals authorize the choice:
    time-to-locate a gap, evidence verification success, correction count,
    wrong-version approval attempts and user confidence?

## Prototype decision test and recommendation

Use the shared “ООО Север” scenario for both variants and record:

- time and errors to locate two missing inputs;
- time and success rate for opening the correct source location;
- number of full-context transitions/backtracks;
- whether the participant can identify partial, stale, unavailable, denied,
  failed and cancelled states without protected leakage;
- whether edits are understood as new artifact versions;
- whether the participant can state which exact version is being approved;
- keyboard completion and narrow-screen task completion.

**Recommendation to test, not a final design:** lead with the staged artifact
workspace, using a strong guided rail and focused human-review dialogs/sheets;
compare it against the pure wizard. The evidence is complementary rather than a
single product to copy: V7 for artifact/evidence adjacency, PandaDoc for document
and quote editing/version approval, Dify for bounded human pauses, and
Agno/Relevance/Stack AI for stage/run/approval feedback. Spine's existing shell,
authorization, evidence and run-state contracts remain authoritative.

## Acceptance check for Issue #587

- All six named products were checked against current primary public sources.
- Official pages containing real UI screenshots/demos and direct screenshot URLs
  are linked where available.
- Observed patterns are separated from Spine recommendations and mismatches.
- The complete manager journey and required degraded/access/terminal states are
  mapped.
- Wizard and document-centric workspace are compared, including navigation,
  transitions, sources, review, editing, desktop/mobile, accessibility and shell
  reuse; a hybrid is proposed as a prototype hypothesis.
- The first requirements slice is bounded to two primary screens and minimum API
  interactions without pricing/editor/export scope.
- Two prototype variants, one shared scenario, current-component mapping,
  missing contracts and human decision questions are ready for the dependent
  prototype ticket.

## Ready-to-paste resolution and prototype handoff

Research resolution for **Research R2 proposal-workspace interface patterns**:

- Primary sources and first-party UI evidence were checked for Stack AI, V7 Go,
  Dify, Relevance AI, PandaDoc and Agno Control Plane. The source catalogue,
  direct screenshots, observed-pattern matrix and architectural exclusions are
  recorded in this note.
- The references converge on four complementary patterns: V7's persistent case
  with adjacent structured result/source inspection; PandaDoc's document,
  quote, version and approval focus; Dify's bounded pause/edit/decision/timeout
  human step; and Agno/Relevance/Stack AI's observable run, task and approval
  feedback. No referenced runtime or workflow builder should become Spine's
  domain or execution boundary.
- Recommendation for the prototype, not an approved design: compare a pure
  guided wizard against a staged artifact workspace. The leading hypothesis is
  the workspace with a guided stage rail, one active typed artifact, contextual
  evidence/run inspector and focused clarification/diff/approval modes.
- Use the same synthetic “ООО Север” scenario for both variants. Compare
  orientation, source verification, rework/backtracking, version awareness,
  wrong-version approval prevention, keyboard completion and narrow-screen
  completion. Wizard hypothesis: about 8–10 stage transitions. Workspace/hybrid
  hypothesis: one route into the workspace, 5–8 in-place selections, optional
  route to full run diagnostics.
- Reuse `AppShell`, scope-in-URL, generated scoped client, upload/ingestion
  states, `StatusPanel`/`ApiErrorPanel`, current-authorized citation/content
  inspection, feedback/cancellation and Runs timeline/attempts. Add R2-owned
  requirements review, gap/question forms, artifact-evidence inspection,
  artifact version/conflict and later estimate/document/approval contracts.
- Missing contracts are listed explicitly: versioned request/requirements
  schemas, request-to-source links, proposal stage read model, generalized
  artifact evidence, clarification lineage, human review command/receipt,
  optimistic locking, and later diff/estimate/approval boundaries.
- Minimum first-slice review boundary: two primary screens only—New request and
  Requirements review—plus a link to existing run detail. The manager can upload
  material, inspect/edit extracted requirements, answer gaps, inspect authorized
  evidence and accept/request changes on a versioned `RequirementsArtifact`.
  No analogues, pricing, proposal editor, Google Sheets, DOCX export or external
  client send is required.
- Product Owner/UX/team lead still must decide the stable IA object, direct-edit
  authority, blocking conditions, approver separation, exact approved object,
  mobile editing scope, clarification ownership, diff granularity and default
  run detail. The recommendation is therefore not presented as approved.

This research satisfies the ticket's evidence criteria and makes the dependent
prototype ready to start after the tracker dependency is released; it does not
start or resolve that HITL prototype.
