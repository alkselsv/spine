# Branching and integration

Spine uses trunk-based development: `main` is the only long-lived integration
branch, and every implementation ticket is delivered through a short-lived
branch. Issues express work and dependencies; branches carry one ticket's
implementation.

## Start work

Start from an agent-ready GitHub issue whose blocking issues are closed. Update
the local view of `main`, then create a branch from `origin/main` using:

```text
<type>/<issue-number>-<short-slug>
```

Use one of these types:

- `feat` for product behavior;
- `fix` for a defect;
- `docs` for documentation-only work;
- `refactor` for behavior-preserving structural work;
- `test` for test-only work;
- `chore` for repository maintenance.

Examples:

```text
feat/123-citation-inspector
fix/147-workspace-isolation
docs/52-context-broker-adr
```

One branch owns one ticket and one independently verifiable outcome. A spec may
produce several ticket branches; do not create a long-lived feature branch for
the whole spec.

## Dependencies and parallel work

Represent dependencies as blocking edges between GitHub issues. When ticket
`#124` depends on `#123`, merge `#123` first and create the branch for `#124`
from the resulting `main`. Use stacked branches only when waiting for the parent
would stop useful work; after the parent merges, rebase the child onto `main`
before its final review.

Each concurrent agent works in its own Git worktree and ticket branch. Record
the branch, worktree and owned files when assigning the task. An agent preserves
changes owned by other tickets and coordinates before editing overlapping files.

## Implement and review

Use the ticket as the behavioral contract and implement it through the
repository's TDD and verification rules. Keep documentation, migrations and API
or interface changes required by the outcome in the same branch.

Before integration:

1. Rebase the branch onto the current `main` and resolve conflicts by intent.
2. Run the verification gates defined in the root `AGENTS.md`.
3. Run `code-review` against the merge-base with `main`, covering both repository
   standards and the originating issue or spec.
4. Complete `.github/pull_request_template.md` and open a pull request that links
   the issue with `Closes #<number>`.

Pull requests are the integration and review surface, not the intake surface for
requirements. New requirements start as GitHub issues or specs. Agents may
prepare local branches and commits, but pushing a branch, opening a pull request
or changing repository settings requires explicit user authorization.

Integrate an approved pull request with squash merge, then delete its remote and
local ticket branches. Intermediate red-green-refactor commits may remain useful
inside the branch; `main` receives one logical commit for the ticket.

## Prototypes

Create throwaway prototypes from `main` with:

```text
prototype/<short-name>
```

Keep the prototype branch as a primary source instead of merging its code into
production. Record the answer it produced in the relevant spec, ADR or issue,
then implement the production behavior in normal ticket branches.

## Releases and urgent fixes

Tag releases from verified commits on `main`. Do not create release branches
until Spine must maintain more than one released line simultaneously.

Urgent fixes use the same issue, branch, review and verification path as other
changes:

```text
fix/<issue-number>-<short-slug> -> pull request -> main -> patch tag
```

This path keeps the fix in the integration history and prevents production-only
changes from diverging from `main`.
