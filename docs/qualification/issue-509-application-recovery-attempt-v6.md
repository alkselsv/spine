# Issue #509 recovered application attempt v6

Status: **projection publication verified; application recovery qualification remains NOT_VERIFIED**.

The authorized synthetic run restored a fresh isolated PostgreSQL target using
the required pgvector image from the start. TLS runtime readiness, ownership
and ACL posture passed. The bounded provider-backed projection completed
through the production consumer. Canonical receipt, snapshot, activation and
active publication counts were each one.

Before the application canary, an offline preflight proved that the corrected
trusted resolver initialized the active request scope, completeness intent and
authorization context. The approved context profile also passed independent
model and digest validation.

One short-lived service authority was then created for the exact
`answer_question` / `retrieve_context` scope. The single Context Broker request
still failed closed with `profile_unavailable` before source retrieval and
before bundle creation. This disproves the run-v5 hypothesis as a complete root
cause: request-scope initialization was necessary but not sufficient. No retry
was made on target006, and the remaining runtime profile-resolution cause is
not yet verified.

The temporary authority was revoked in the guaranteed cleanup path. The target
was sealed, the direct runtime CONNECT grant was absent, temporary database
authentication material was rotated, the exact transient graph container and
both graph volumes were removed, and PostgreSQL was stopped. No serving,
cutover or customer data was involved.

A future attempt requires a separately authorized fresh target and must first
diagnose the profile repository path without consuming another Broker request.
Independent evidence review remains required.
