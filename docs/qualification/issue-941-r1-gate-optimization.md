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

Hosted-runner PostgreSQL, package-download, and runner-load timing is recorded
below; local verification remains useful only for attributing the removed work.

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
removed repetition rather than removed coverage.

## Hosted CI result

PR #944 run 41894536718, attempt 2, completed successfully on 2026-11-04:
<https://github.com/alkselsv/spine/actions/runs/41894536718/attempts/2>.

- the complete `r1-regression-gate` job took 12 minutes 10 seconds, down from
  the 24 minutes 36 seconds baseline median by 12 minutes 26 seconds (50.5%);
- the optimized repository suite took 5 minutes 20 seconds, down from its
  15 minutes 21 seconds baseline median by 10 minutes 1 second (65.3%);
- retained-revision qualification took 4 minutes 49 seconds;
- media-probe installation took 17 seconds;
- the deterministic offline gate took 2 seconds.

Attempt 1 was cancelled after the unchanged apt media-probe installation made no
progress for 15 minutes. Attempt 2 isolates the implementation timing on a
healthy hosted runner; the cancelled attempt remains evidence that package
installation is a separate source of tail latency outside this ticket's scope.
