---
status: accepted
---

# `answer_question` Context Broker handoff

Issue #284 connects the Q&A capability to the existing authorized Context
Broker without adding a second retrieval or authorization path.

## Decision

- The broker is the only retrieval port visible to `answer_question`. It
  accepts the server-resolved `ContextProfile` and trusted request references;
  callers cannot choose a projection, physical namespace, activation,
  canonical boundary or `as_of` cut.
- One broker request is one delegated persistence operation:
  `answer_question` / `retrieve_context`. Every tenant Unit of Work opened to
  resolve the profile, publication, authorized scope, read model, evidence or
  immutable bundle uses that same server-selected authority. Repository method
  names are implementation details and cannot select or broaden authority;
  persistence outside the broker request retains its own bounded operation.
- A successful response carries only a `ContextBundleRef`. The application
  seam loads the immutable bundle from its tenant-scoped store and validates
  request, run/attempt, idempotency, publication, profile and every execution
  pin before creating `AnswerQuestionCandidateInput`.
- Broker outcomes are converted to `RetrievalOutcome` facts and then to the
  existing AQ outcome resolver. They do not constitute an answer. Degraded,
  stale, unavailable, denied, insufficient and cancelled responses are safe
  terminal paths; protected context is never included in their output.
- The bundle-derived `DisclosureDependencySet` is retained across generation.
  Before disclosure, the same bundle and dependencies are revalidated through
  current-authority ports and passed to the existing AQ-11
  `AnswerDisclosureGate`. Only that gate may produce `AcceptedGroundedAnswer`.

## Consequences

The handoff is testable with an offline broker fake and remains independent of
Cognee, PostgreSQL, FastAPI and model-provider APIs. Replay uses the broker's
existing idempotent bundle persistence and stable request identity. Production
qualification still requires the canonical bundle store, current-authority
revalidator, active publication authority and configured physical projection;
the offline suite does not claim those integrations are live.

The deterministic `broker_persistence_exact_scope_rate` evaluation moves from
the pre-change composition baseline `0.0` to `1.0`: every observed Broker
persistence context carries the exact `answer_question` / `retrieve_context`
authority. This metric does not replace the separately authorized production
canary or its independent evidence review.
