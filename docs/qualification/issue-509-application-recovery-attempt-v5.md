# Issue #509 recovered application attempt v5

Status: **projection publication verified; application recovery qualification remains NOT_VERIFIED**.

The authorized synthetic run restored a fresh isolated target and verified TLS,
runtime readiness, ownership and ACL posture. The approved provider-backed
projection completed through the production consumer. Canonical receipt,
snapshot and activation were persisted, and publication reached activation
generation 1.

The application canary created a short-lived service authority matching the
existing synthetic `answer_question` / `retrieve_context` source grant. Human
reauthorization was not used because the immutable archive already contains an
expired wildcard human binding and the schema correctly rejects replacing or
narrowing that history.

One Context Broker request failed closed with `profile_unavailable` before
source retrieval and before bundle creation. The qualification harness issued a
trusted authorization context but did not initialize the production active
request scope used by the profile repository adapter. Consequently no permitted
source read occurred. The temporary service authority was immediately appended
with a terminal revoked version; the request was not retried on this target.

Cleanup completed after capture. Runtime access was revoked, temporary database
authentication material was rotated to unknown values, the isolated graph was
removed and PostgreSQL was stopped. No serving, cutover or customer data was
involved.

A separately authorized fresh run must initialize the same request-scope state
as production composition before issuing exactly one Context Broker request.
Independent evidence review remains required after that read succeeds.
