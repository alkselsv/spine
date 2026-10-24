# Issue #588 — R2 proposal workspace prototype comparison

Status: **prototype handoff, not an approved UI/lifecycle specification**.
All data and prices are synthetic. The prototypes are disposable primary-source
artifacts and are intentionally isolated from the production `web/` routes.

Source basis: Issue #588, R2 roadmap, `AGENT_DEVELOPMENT.md`, `INTERFACE.md`, the
existing Spine web shell, and the pinned #587 research note at commit
`b16a58ff1e0147e5be35f33b8094c17ceb6cc6ce`. The note is not treated as merged
production code.

## Decision question and prototype URLs

Should the manager journey use a workflow step as the stable UX object, or a
proposal workspace containing staged typed artifacts?

- A: `index.html?variant=guided`
- B: `index.html?variant=workspace`

Leading hypothesis to test: **B, Staged Artifact Workspace**, because evidence,
version and rework context remain visible while a stage rail retains orientation.
This is a recommendation for human review, not a Product Owner decision.

## Shared scenario and bounded first slice

Both variants use the same ООО «Север» inputs, eight requirements, ambiguity,
two clarification gaps (`8×5`, `75 GB`), evidence inspection, one manager edit
and RequirementsArtifact confirmation. Both visually separate the first slice:

```text
ClientRequest → normalize_request → extract_requirements → validate_evidence
              → review_requirements (human) → RequirementsArtifact
```

Later demo-only stages show two analogues (one stale), an approved pricing basis,
deterministic quantity recalculation, draft editing, v3 rejection, exact-v4
approval and linked agent runs. They are not commitments to first-slice scope.

## Decision matrix

| Criterion | Guided Wizard | Staged Workspace | Recommended | Reason |
| --- | --- | --- | --- | --- |
| Скорость выполнения сценария | Последовательный и понятный, но 8–10 смен контекста | 5–7 in-place selections на одном route | Workspace | Меньше возвратов при повторной проверке |
| Количество переходов | Выше; исправление требует back/forward | Ниже; stage selection сохраняет route и inspector | Workspace | Нелинейная работа естественнее |
| Проверка доказательств | Drawer сохраняет форму, но скрывает соседние артефакты | Evidence постоянно рядом с активным artifact | Workspace | Лучше сопоставление claim ↔ source |
| Уточнение информации | Отдельный обязательный экран хорошо объясняет gate | Focused dialog сохраняет общий контекст | Tie | Wizard лучше обучает, workspace лучше при повторениях |
| Редактирование | Удобно для одного этапа; cross-stage rework дорог | Typed artifact остаётся центральным объектом | Workspace | Сильнее поддерживает несколько циклов правок |
| Контроль версий | Версия видна в header/финальном шаге | Версия, lineage и inspector видны постоянно | Workspace | Ниже риск потерять base/head |
| Предотвращение ошибочного утверждения | Терминальный approval step и блокирующая validation | Exact-version dialog + rail status + stale lock | Workspace | Больше контекста перед consequential action |
| Accessibility | Простая линейная focus order; сильнее на narrow screen | Требует landmarks/sheet/focus restoration | Wizard | Меньше одновременных регионов |
| Reuse существующих Spine components | Reuse StatusPanel/forms/drawer | Reuse AppShell, URL scope, inspectors, Runs и panels | Workspace | Ближе к текущей control-plane композиции |
| Сложность реализации первого slice | Ниже: два последовательных экрана | Средняя: rail + center + responsive inspector | Wizard | Меньше layout/state infrastructure |
| Расширение до полного R2 | Backtracking и длинные artifacts усложняются | Estimate, draft, diff и runs естественно добавляются | Workspace | Стабильный work object масштабируется лучше |

Recommendation: выбрать B как primary desktop IA, сохранив из A ordered stage
semantics, validation summary и последовательный narrow-screen flow. Подтвердить
решение должен Product Owner/тимлид после task walkthrough; до этого оба варианта
остаются равноправными prototype evidence.

## Existing and missing frontend/API contracts

| Need | Existing Spine seam to reuse | Missing R2 contract |
| --- | --- | --- |
| Shell and scope | `AppShell`; workspace/environment URL context; auth boundary | Proposal/work-item route and scoped deep-link state |
| Materials | Upload, ingestion status, revision detail | Explicit ClientRequest → immutable SourceRevision link |
| State/error UI | StatusPanel, ApiErrorPanel, safe trace IDs | Domain-specific unavailable/cancelled/conflict recovery |
| Requirements | Base UI form primitives and Spine tokens | Requirement row, ambiguity/gap forms, dirty/version state |
| Evidence | Citation inspection and parsed-content authorization semantics | General artifact-evidence target and required-evidence gate |
| Runs | Lists, detail, timeline, attempts, cancellation | Manager stage projection and proposal artifact lineage |
| Human review | Feedback/cancellation interaction patterns | Typed review command and immutable review receipt |
| Versions | Status/timeline primitives | Version list, semantic diff and optimistic locking |
| Estimate | No proposal contract | Editable inputs, approved PricingBasis and deterministic receipt |
| Approval | No generic R2 approval | Exact-version work item, policy gate and immutable decision receipt |

### Variant A backend/API needs

- Canonical step/read model with ordered stage, completion/gate status, allowed
  next/back actions and current artifact version.
- Draft save per step plus explicit transition command; backtracking must not
  mutate completed artifacts implicitly.
- Evidence inspection independent of route transition so the drawer can preserve
  unsaved step state.
- Server validation summary for each Next/Accept gate and canonical GET recovery.
- Exact-version approval command and conflict response on the final step.

### Variant B backend/API needs

- Stable proposal workspace read model: stage summaries, active/pinned artifact
  IDs, current versions, ownership and run status.
- Independent versioned artifact GETs so stage selection does not restart work.
- URL-addressable stage/version/inspector target and resumable run observation.
- Clarification, semantic diff, rework and approval work-item contracts for
  focused dialogs; every command carries `expected_version` and idempotency key.
- Manager-level run projection plus deep link to existing full Runs diagnostics.

### Shared minimum first-slice interactions

1. Create a versioned ClientRequest with workspace/environment and idempotency.
2. Reuse upload ingestion, then link accepted SourceRevisions to the request.
3. Start extraction pinned to request, capability/agent/workflow and context
   profile versions; return run/artifact correlation.
4. Read canonical request/run state and observe resumable SSE with polling
   fallback; terminal state always rehydrates from GET.
5. Read RequirementsArtifact with requirements, ambiguities, gaps, evidence,
   upstream lineage, version and review state.
6. Inspect current-authorized evidence at an exact revision/locator; fail closed
   and suppress protected content when denied or unavailable.
7. Submit edits, answers, decision, comment, `expected_version` and idempotency;
   receive a new immutable reviewed version or typed conflict.
8. Cancel terminally; retry creates a linked run rather than resurrecting one.

## State behavior contract demonstrated by both variants

| State | Available | Blocked / recovery |
| --- | --- | --- |
| Loading/running | Read inputs, observe named stage, cancel if policy permits | Accept/approve; wait for canonical result |
| Empty | New Request action | Artifact editor; supply required input |
| Partial | Inspect retained valid items, retry omitted item | Consequential acceptance until completeness gate passes |
| Stale | Read pinned/new version metadata | Edit/approve; refresh/rebase to current version |
| Evidence unavailable | Safe reason code and access request | Excerpt/title where protected; approval requiring evidence |
| Permission denied | Scope and safe trace ID | Any protected content or existence hint; return to allowed list |
| Degraded | Read saved artifact, polling fallback | Completeness claim; retry/refresh capability |
| Failed | Read preserved prior artifact, safe error, retryability | Silent repeat; create a linked retry run |
| Cancelled | Read last valid artifact and cancellation receipt | Resume same run; create linked new run |
| Optimistic conflict | Compare server v4 with local edit | Save/approve until changes are rebased |
| Outdated approval | Inspect v3→v4 diff | Approve v3; select and review exact current candidate |

## Human decision register

These are proposals only; “default” means a starting point for human review.

| ID | Proposed default | Alternative | Rationale | Question to Product Owner / team lead |
| --- | --- | --- | --- | --- |
| D1 | Proposal workspace is stable; stages select artifacts | Current workflow step is stable | Better cross-artifact context and rework; wizard is simpler | Is the primary object the proposal workspace or current step? |
| D2 | Permit direct requirement wording edits with actor/time/diff | Corrections only, then regenerate | Faster bounded correction without hiding provenance | May a manager directly edit extracted wording in slice 1? |
| D3 | Missing SLA/migration block dependent stages; required inaccessible evidence blocks artifact acceptance | Allow explicit risk acceptance | Fail-closed protects consequential output; some gaps may be non-blocking | Which gap/evidence classes block requirements, submit and approval? |
| D4 | Bounded textual/input edits inline; inference or evidence changes create a new run | Regenerate after every edit | Separates deterministic human correction from agent inference | Which exact fields are inline-editable per artifact? |
| D5 | Policy-configurable separation; default same person allowed in low-risk demo | Always distinct author/approver | Avoids inventing governance while enabling stricter workspaces | When must author and approver be different? |
| D6 | Approve structured artifact; rendered output linked by deterministic render receipt and may need a second gate | Approve rendered document only, or both simultaneously | Structured state is auditable; rendered fidelity can still matter | What exact object is legally/operationally approved? |
| D7 | Mobile supports intake, clarification, evidence review and approval; bounded corrections only | Full authoring | Three-pane editing loses its advantage on narrow screens | Must mobile support full proposal/estimate authoring? |
| D8 | Clarification is assigned to proposal owner with explicit due time and escalation policy | Shared unowned queue or in-workspace only | Durable human waits require ownership and timeout semantics | Who owns a clarification, and what happens on timeout? |
| D9 | Field/line/section/evidence semantic diff; material/evidence/price changes force reapproval | Rendered-page diff only or every change forces reapproval | Decision-oriented diff is clearer than pixels; policy can classify materiality | Which changes are material and which always revoke approval? |
| D10 | In-workspace stage, attempts, safe warnings and artifact lineage; deep link for full diagnostics | Expose full trace in workspace | Managers need recovery context, not engineering internals or chain-of-thought | Which diagnostics are useful by default to a manager? |

## Minimum human review contract for slice 1

Input is an immutable RequirementsArtifact candidate and current-authorized
evidence view. The human task records `workspace_id`, environment, task/owner,
candidate artifact ID/version, expected version, upstream ClientRequest and
SourceRevision IDs, opened evidence references, field edits, clarification
answers, decision (`accept | request_changes`), comment, actor, timestamps and
idempotency key. Acceptance is allowed only after server-side authorization,
version, required-field and evidence-validity gates. Success creates an immutable
review receipt and reviewed RequirementsArtifact version. Conflict, denial,
unavailable required evidence, cancellation and timeout are typed outcomes; none
silently mutate or approve the candidate. The receipt resumes a linked durable
workflow run, while full run diagnostics stay in the existing Runs surface.

### Proposed highest testing seam after human selection

Use one browser-level acceptance seam over the scoped `/api/v1` boundary for the
two slice screens: create request, link synthetic revisions, run extraction,
inspect authorized evidence, submit review with expected version, and assert the
reviewed immutable artifact. Drive deterministic backend fakes beneath that seam;
add focused contract tests only for authorization disclosure, optimistic conflict
and idempotent review replay. This is a proposal for the later `$to-spec` step,
not an approved testing decision.

## Human review checklist before `$to-spec`

- Select A, B or an explicit hybrid; record rejected alternative and rationale.
- Resolve D1–D10, especially exact approved object and blocking evidence rules.
- Confirm first-slice editability and review receipt fields.
- Confirm desktop/narrow-screen scope and author/approver policy.
- Confirm the proposed highest acceptance seam and API resource naming.
- Record usability evidence: transitions, source-verification success,
  wrong-version attempts, keyboard completion and 390 px completion.

Only after those decisions should `$to-spec` publish an implementation-ready
issue and apply `ready-for-agent`. Issue #588 must remain open until the human
decision is recorded.
