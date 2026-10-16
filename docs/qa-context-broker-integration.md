# Q&A production Context Broker handoff

Issue #284 adds the application seam used by `answer_question` after the
offline capability work. `AnswerQuestionContextService` derives the broker
request from the server-resolved execution pin, consumes the broker's typed
`ContextResponse:v1`, loads the exact persisted `ContextBundle`, and runs the
two typed attempt revalidation checkpoints. It also derives the complete
disclosure dependency set from that bundle; dependency revalidation remains a
separate required input to the final disclosure gate.

The offline qualification covers deterministic broker, bundle-store and
revalidation fakes. It does not claim production qualification merely because
the Context Broker tickets are published or closed. The following production
prerequisites remain unverified in this branch:

- a configured PostgreSQL-backed `SavedContextBundlePort` and
  `ContextBundleRevalidator` against the canonical authorization/source
  lifecycle tables;
- the active publication and `as_of` authority being available in the target
  workspace/environment;
- the configured physical Context Projection provider (PostgreSQL + Neo4j +
  PGVector for the current production profile) passing its opt-in integration
  gate;
- the disclosure dependency revalidator returning persisted validation facts
  before an answer is accepted for disclosure.

No Q&A code in this seam imports Cognee or a physical retrieval adapter. Those
operations remain owned by the Context Broker and its `ContextProjection`
boundary.
