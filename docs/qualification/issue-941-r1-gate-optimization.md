# Issue #941 R1 regression gate optimization

The optimization preserves the three independent R1 repository evidence
boundaries while removing work repeated inside the same CI job.

## Baseline

Four successful runs observed on 2026-11-04 had a median job duration of
24 minutes 36 seconds. The repository suite had a median duration of 15 minutes
21 seconds, and the dedicated retained-revision target had a median duration of
6 minutes 2 seconds. The deterministic `spine-eval-gate` took about two seconds.

Representative baseline:
<https://github.com/alkselsv/spine/actions/runs/41880339622>.

## Preserved coverage

The clean-target and retained-revision tests remain dedicated executions on
separate disposable PostgreSQL targets and continue to emit their validated
receipts. The repository execution deselects exactly those two node IDs, so each
test covered by the previous job still executes once.

The canonical repository evidence command contains the same deselections as the
workflow. Qualification tests compare the complete argv rather than maintaining
an independent approximation.

Issue #68 retains an executable command-contract check for the full offline
gate, but no longer starts a second repository-wide pytest process from inside
an already-running repository suite.

## Timing evidence

The repository command uses `--durations=50`. Pytest emits only slow test node
identities and durations to the existing CI log; the timing list is diagnostic
and is not included in the attested metadata-only evidence artifact.

The final wall-time comparison requires a successful GitHub Actions run of this
branch. Local verification cannot substitute for hosted-runner PostgreSQL,
package-download, and runner-load timing.

## Local verification

On 2026-11-04, using the same worktree and dependency environment:

- the complete repository command passed with 6,760 tests and 7 skips in
  439.81 seconds;
- the optimized repository command passed with 6,758 tests, 7 skips, and the
  two separately receipted target nodes deselected in 216.25 seconds;
- the optimized command emitted the requested 50-entry duration summary and a
  valid JUnit report;
- `python -m compileall -q src tests` passed.

The local repository phase decreased by 223.56 seconds (50.8%). The two omitted
tests remain mandatory dedicated CI executions, so this comparison measures
removed repetition rather than removed coverage. Hosted CI remains the
authoritative measurement for the ticket's final wall-time acceptance criterion.
