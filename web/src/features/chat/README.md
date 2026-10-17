# Spine Chat transport

This feature owns the browser-side boundary for diagnostic Chat. `protocol.ts`
defines the versioned, disclosure-safe event contract; `transport.ts` handles
ordering, deduplication, resume, heartbeat timeout, cancellation and canonical
message hydration.

`fixtures.ts` is intentionally a controlled adapter for Issue #166 tests. It
does not implement Issue #26 endpoints and must be replaced by an adapter over
the generated API client and Spine SSE endpoint when those endpoints exist.
The transport never treats streamed deltas as the saved message source.

`ask-command.ts` owns the isolated command actions for authorized Ask admission.
It sends the generated admission request with a fresh idempotency key, keeps the
same key and payload for an explicit retry, and links a clarification through the
server contract. `SessionBrowserPage.tsx` composes those actions with canonical
session resources: an accepted command is hydrated by its message ID, and
nonterminal messages are refreshed until the server records a terminal outcome.
Restoring an SSE connection cannot submit another Ask. `ChatPanel.tsx` remains a
controlled diagnostic fixture; C14 owns final ChatPanel wiring after C06 merges.

`AcceptedAnswer.tsx` displays answer text only when the canonical message is
terminal and answered, its current disclosure eligibility is `eligible`, and
the accepted presentation's message/run identities and SHA-256 digest match.
`SafeMarkdown.tsx` renders a restricted Markdown subset using React elements;
raw HTML is rejected and links are limited to absolute HTTP(S) URLs. The answer
places each typed citation marker immediately after its exact accepted
claim span. Markdown rendering preserves source offsets so the marker stays at
the accepted placement even when the answer contains formatted text.
Inspecting a citation re-reads the canonical message and protected citation
endpoint with the current workspace/environment scope. The UI does not enable
historical inspection or reuse cached excerpts; any failed, stale, denied or
mismatched disclosure hides the answer and all claim placements. No answer,
partiality or completeness state is inferred from transport health or citation
counts. C14 (#421) owns the final ChatPanel wiring.

## Composition reference map (Issue #410)

Agno AgentOS Control Plane is the interaction target for this shared shell and
the later Chat/Runs slices. Its public guides describe the user-facing areas and
their relationships; they do not document every pixel-level detail. Spine uses
those guides as presentation references only. Agno remains neither a runtime
dependency nor an authorization authority.

Halaska is limited to the revision and MIT attribution recorded in
[`web/design-system/NOTICE.md`](../../../design-system/NOTICE.md). That notice
identifies its applicable patterns as navigation density, status panels, and
recovery-oriented hierarchy. No full kit or upstream source file is installed.
Base UI remains the primitive source for the existing accessible scope selects.

| Area | Agno reference pattern | Halaska or Base UI fallback | Spine adaptation | Material deviation |
| --- | --- | --- | --- | --- |
| Chat | [Control Plane chat](https://docs.agno.com/agent-os/control-plane) starts a conversation from a registered component and follows its streamed output. | No Halaska Chat component is referenced by the pinned notice. Keep the existing Base UI scope selectors and Spine CSS Module surfaces. | The production Chat route composes a scoped Q&A session list, an Ask composer and canonical message hydration. Saved outcomes use localized status labels; accepted answer Markdown is gated by current disclosure eligibility and accepted-answer identity. | This initial composition does not add cancellation, citation inspection, feedback or the final ChatPanel wiring. The controlled diagnostic ChatPanel remains a fixture. |
| Sessions and messages | Agno's [Control Plane session tracking](https://docs.agno.com/agent-os/control-plane#session-tracking) groups conversation messages and runs under a canonical `session_id`; its [session management guide](https://docs.agno.com/agent-os/usage/client/session-management) covers create, list and manage operations. The [storage overview](https://docs.agno.com/sessions/persisting-sessions/overview#session-storage-schema) describes persisted message/run history. | The pinned Halaska notice does not identify a session browser component. Reuse its navigation density and status/recovery hierarchy; native Spine links and buttons sit beside existing Base UI controls. | List/create/restore and message-history pages call `/api/v1/qa/sessions` through the generated, scope-injected client. The UUID session ID is the route identity; cursor pagination is represented in the URL and query key. Query keys include a non-secret auth identity version; scope and prior identity caches are removed when their context changes. Archived sessions require an explicit, versioned restore command. Accepted answers use the server-authorized `accepted_presentation`; exact claim quotes and typed relationships reference citation IDs that are inspected through the protected citation API. | Session rename/delete, runtime selection, raw trace payloads, and Agno identity/authorization semantics remain outside this slice. Server authorization remains authoritative; denied or unavailable current inspection suppresses the answer and its placement graph. |
| Runs | [Tracing filters](https://docs.agno.com/agent-os/tracing/filter-options) distinguish individual runs from sessions that group traces. | Halaska's pinned status-panel hierarchy is applicable to Spine's existing run states; no run component is copied. | Keep the Runs navigation entry and compose its page in the shared main content area. | Spine's current Runs route represents ingestion runs, not a general Agent Run catalog or Agno trace run. |
| Tracing and progress | The [Control Plane guide](https://docs.agno.com/agent-os/control-plane) describes streamed execution output and trace tree/waterfall views with span status, timing, and relationships. | Use the pinned Halaska status-panel pattern for concise state; Base UI remains limited to existing controls. | Keep live Chat state in its existing SSE boundary and make shell connection state explicit with text and a status marker. | No trace tree, waterfall, span timeline, or generic agent-step progress is introduced by this shared-shell ticket. |
| Evidence | Agno's [trace guide](https://docs.agno.com/features/control-plane) describes inspecting model/tool calls and their inputs and outputs. | No evidence-inspector pattern is claimed from Halaska; use Spine's existing revision-scoped Knowledge detail surfaces. | Chat claim markers reference canonical citation IDs. Each user-requested inspection performs a fresh scoped read and displays only the authorized revision-scoped excerpt and locator returned by Spine. | The shell does not expose prompts, model context, raw tool payloads, or a new evidence endpoint. Authorization and evidence content come from Spine's existing protected message and citation endpoints. |
| Errors | Agno traces expose execution errors alongside span status and timing in the [tracing guide](https://docs.agno.com/agent-os/tracing/overview). | Use the pinned Halaska status and recovery hierarchy only as presentation guidance. | Keep Spine's localized safe API error panel and expose connection-check state in the shared header. | Spine errors remain disclosure-safe; provider messages, stack traces, prompts, and protected excerpts are not shown. |
| Navigation and details | The [Control Plane overview](https://docs.agno.com/features/control-plane) moves between Chat, sessions, traces, knowledge, approvals, and runtime management. | Use Halaska's pinned navigation-density pattern and Base UI's existing select primitive; retain Spine's CSS Modules and tokens. | Provide a compact header, labeled workspace/environment context group, persistent primary navigation, and responsive main content column. | Spine navigation remains limited to its existing product routes and server-managed scope. It does not select an Agno runtime or inherit Agno roles and permissions. |

The shared shell owns composition, current scope, and connection presentation.
Feature pages continue to own their state, evidence, error outcomes, and
resource-specific details. Navigation and UI state never grant access; the
server remains responsible for workspace authorization.
