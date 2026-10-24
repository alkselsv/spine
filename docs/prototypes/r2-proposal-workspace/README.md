# R2 proposal workspace prototypes — Issue #588

> Disposable research artifacts. These files are not production frontend code,
> do not define an approved design and must not be merged into product routes.

The package contains two structurally independent concepts using the same
synthetic ООО «Север» scenario:

- `?variant=guided` — **A: Guided Review Wizard** with eight sequential screens,
  persistent stepper, evidence drawer, backtracking and explicit human gates;
- `?variant=workspace` — **B: Staged Artifact Workspace** with stage rail,
  active typed artifact and contextual evidence/version inspector.

Both variants include the bounded first slice and illustrative later R2 stages.
The “Лаборатория состояний” control demonstrates loading/running, empty,
partial, stale, evidence unavailable, permission denied, degraded, failed,
cancelled, optimistic-lock conflict and outdated-version approval behavior.

## Open locally

No dependencies, credentials or production API are required.

```bash
python3 -m http.server 8088 --directory docs/prototypes/r2-proposal-workspace
```

Then open:

- <http://localhost:8088/?variant=guided>
- <http://localhost:8088/?variant=workspace>

The files also work when `index.html` is opened directly. Use `←` / `→` outside
form fields to switch concepts. At narrow widths the evidence inspector becomes
a full-height sheet and the stage navigation becomes horizontally scrollable.

## Suggested review path

1. Review the two source files and eight requirements.
2. Open evidence from requirement R5.
3. Enter `8×5` and `75 GB` in the clarification fields.
4. Edit requirement R8 and identify the reviewed RequirementsArtifact version.
5. Visit analogues, exclude the stale example and inspect the pricing basis.
6. Change estimate quantity and observe deterministic recalculation.
7. Edit the proposal, inspect the v3 → v4 semantic diff, reject v3 and approve
   exactly v4.
8. Open linked runs and exercise every state in the state laboratory.
9. Repeat key actions with keyboard only and at a 390 px viewport.

The comparison, contract mapping and human decision register are in
[`comparison.md`](comparison.md).
