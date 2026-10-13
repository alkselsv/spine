# Spine Chat transport

This feature owns the browser-side boundary for diagnostic Chat. `protocol.ts`
defines the versioned, disclosure-safe event contract; `transport.ts` handles
ordering, deduplication, resume, heartbeat timeout, cancellation and canonical
message hydration.

`fixtures.ts` is intentionally a controlled adapter for Issue #166 tests. It
does not implement Issue #26 endpoints and must be replaced by an adapter over
the generated API client and Spine SSE endpoint when those endpoints exist.
The transport never treats streamed deltas as the saved message source.
