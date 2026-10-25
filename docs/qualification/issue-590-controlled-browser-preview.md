# Issue #590: controlled browser preview verification

Verified on 2026-10-25 from `fix/590-owned-preview`, based on implementation
commit `24ba370` with the review fixes recorded in this document.

## Owned current build

Command:

```bash
SPINE_CONTROLLED_PREVIEW_PORT=43127 pnpm web:test:e2e
```

The command built the current SPA before Playwright, started its Vite preview at
`http://127.0.0.1:43127`, and passed all 30 controlled Chromium tests. After
Playwright exited, `lsof -nP -iTCP:43127 -sTCP:LISTEN` reported no listener,
confirming that the qualification run stopped its owned server.

To verify build freshness independently of a pre-existing `web/dist`, the
visible controlled page title and its browser expectation were temporarily
changed to an Issue #590 marker. Running the same public E2E command rebuilt the
SPA and the browser observed the marker. The temporary source and expectation
changes were then reverted.

## Default-port collision

A harmless Python HTTP server was started on `http://127.0.0.1:4175`, then the
unmodified public command was run:

```bash
pnpm web:test:e2e
```

The command rebuilt the SPA, issued only Playwright's server-availability probe
to the unrelated listener, and exited before launching any of the 30 browser
tests with:

```text
Error: http://127.0.0.1:4175 is already used, make sure that nothing is running
on the port/url or set reuseExistingServer:true in config.webServer.
```

The unrelated listener was stopped after the check. No controlled browser
request or API/SSE traffic reached it.
