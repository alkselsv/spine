# Issue #596 — Chat and Q&A Run workspaces

Status: implemented as a reversible presentation change over the existing Q&A,
authorization and disclosure contracts.

## Boundaries

- `Q&A Run` keeps the glossary meaning: one admitted durable execution of
  `answer_question`, containing one or more Q&A Attempts and ending in exactly
  one Q&A Outcome.
- Session, Message, Run, Attempt and Citation identities remain server-owned.
  The UI records selections in the URL but never treats them as authorization.
- Citation inspection still re-reads the current canonical Message and the
  protected Citation endpoint. The inspector receives no cached excerpt after a
  denial, scope/identity change, mismatch or stale response.
- Operational Diagnostics remains a separate surface below Q&A Run detail. It is
  not mounted inside the Citation Inspector.
- The Q&A Run timeline stays linear. Canonical contracts do not expose nested
  Agent Run relationships, so no trace tree or waterfall is shown.

## Reference audit

Agno Control Plane remains the composition reference: a persistent session
rail, primary working pane, contextual inspector, dense operational list and a
run timeline/details split. Spine does not copy Agno runtime selection, raw
traces, prompts, model/tool payloads or authorization semantics.

Halaska revision
`f11ab6958de3fb186a4d431e8425bbe43a6dc87b` was audited through its examples for
`data-table`, `sheet`, `status-dot`, `tabs`, `separator`, `empty` and the
`patterns/status` composition. The controlled transfer is semantic rather than
a source-file copy: the relevant density, side-sheet, compact-state and thin
separator behavior is rewritten as typed Spine TSX and CSS Modules, uses Spine
tokens, and keeps the MIT provenance in `web/design-system/NOTICE.md`.

## Screen mapping

| Spine screen | Agno element or interaction | Halaska pattern or fallback | Spine adaptation | Accessibility, disclosure and deviation |
| --- | --- | --- | --- | --- |
| Sessions without selection | Session navigation beside an empty conversation | Halaska `empty` and list-density patterns; Spine links | `WorkspaceLayout` renders the rail and neutral detail/inspector. It never selects the first or latest Session. | The empty state explains the disclosure boundary. No protected request is made for an unselected Session. |
| Open Session | Session rail, conversation, contextual inspector | Halaska `sheet`, `separator` and compact status patterns, rewritten in Spine CSS Modules | Wide screens use three coordinated panes. The rail can collapse without changing canonical selection; mobile uses sequential panes. | Selection stays in `sessionId`, `message_id`, `run_id`, `citation_id` and `inspector`. Inspector open moves focus to Close, Escape closes it, and focus returns to the invoking control. |
| Citation Inspector | Contextual evidence details | Halaska side-sheet composition with a Spine/Base UI semantic fallback | The authorized Source Revision, stable locator, excerpt and citation feedback render in the inspector host after fresh server checks. | The accepted answer is hidden while revalidation is pending or fails. Scope, identity and mismatch transitions unmount or clear protected content immediately. A non-modal dialog is used on the narrow drawer surface so the thread remains a separate document region. |
| Safe Run summary in Chat | Contextual run status | Halaska `status-dot`/status pattern, expressed as Spine status chips | Displays only outcome, Message/Run/Attempt identities, Message version and governed completeness/conflict categories. | It is visibly separate from Citation inspection and never displays diagnostics, raw provider payloads or chain-of-thought. |
| Q&A Runs list | Dense trace/run list with filters | Halaska `data-table`, compact inputs and pagination pattern; existing Base UI Select | Existing typed filters, cursor and scope remain URL-backed. Rows are compact and each Run opens only through an explicit route selection. | No first-row auto-selection. The table scrolls inside its own frame, avoiding page-level horizontal overflow. |
| Q&A Run detail | Linear run activity with selected execution details | Halaska `separator`, compact status and list-selection patterns; Spine fallback for timeline | Left pane contains the linear timeline and Q&A Attempt selector. Right pane contains Run identity, selected Attempt, governed decisions and eligible evidence. `attempt_id` is linkable and reloadable. | Unknown/mismatched Attempt IDs fail closed. The UI preserves the Q&A Run / Q&A Attempt / Agent Run distinction and does not invent nested relationships. |
| Operational Diagnostics | Separate trace-oriented operational surface | Existing Spine diagnostic panel | Kept below the Run workspace behind its own disclosure-safe control. | Deliberately not merged with Citation evidence because the two surfaces have different disclosure contracts. |

## Responsive and state behavior

- Above the mobile breakpoint, Chat is three-pane and Run detail is two-pane.
- At the mobile breakpoint, master/detail become sequential. Citation inspection
  becomes a bottom drawer; the selected IDs remain in the URL, Escape closes the
  drawer, and focus returns to the invoking marker or summary control.
- Workspace columns use `min-width: 0`; wide Run tables scroll within their
  bounded frame. Neither surface introduces page-level horizontal overflow.
- Existing shared `StatusPanel` and safe API localization continue to represent
  loading, empty, partial, stale, degraded, denied, validation-failed, failed and
  ready states. Partial Answer, technical partial state, Grounded Conflict,
  Observed Coverage and projection completeness remain independent concepts.

## Material deviations

- Halaska's full kit is not installed because Spine already owns tokens and uses
  Base UI primitives. Copying the kit would introduce a second primitive system.
- Halaska's sortable/checkable demo table is not copied: Q&A Run ordering and
  pagination are server contracts, and row checkboxes have no ticket use case.
- Tabs were audited but not added to Run detail. A single explicit Attempt
  selection is clearer than hiding canonical identity, evidence and decisions
  behind local-only tabs; `attempt_id` is the only needed canonical sub-selection.
- Agno trace trees and waterfalls are omitted until canonical nested Agent Run
  relationships exist.
