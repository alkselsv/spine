# R2 provider-neutral governed sources acceptance

Issue #655 is implemented by the additive contracts and bounded provider slices
delivered through #662, #664–#672 and the reusable offline qualification in
#677. This gate records the top-level specification outcome without replacing
those lower-boundary suites or claiming that a live provider is production
qualified.

The accepted offline boundary is:

`Provider Connection → pre-provider admission → typed external observation →
canonical ingestion → immutable SourceRevision → transactional outbox →
authorized retrieval → safe operator inspection`.

## Run

From the repository root, run the aggregate acceptance gate and its two child
qualification gates:

```bash
uv run pytest -q \
  tests/qualification/test_issue655_provider_neutral_sources.py \
  tests/qualification/test_issue677_cross_provider_offline.py \
  tests/qualification/test_issue614_google_offline_qualification.py
```

Then run the repository verification gates from `AGENTS.md`:

```bash
uv sync --extra dev
uv run pytest -q
uv run python -m compileall -q src tests
```

The machine-readable mapping from every #655 acceptance criterion to executable
evidence is
`docs/qualification/issue-655-provider-neutral-sources.json`.

## Interpret the result

`offline_qualified` means the provider-neutral contracts, synthetic adapters,
canonical outcomes, API and UI disclosure rules pass without live credentials or
customer data. It does not mean a provider deployment is production-ready.

Google, Yandex Disk, Bitrix24 and mounted-filesystem live behavior remains
`NOT_VERIFIED` until its exact approved prerequisites are exercised. Google
#615, Bitrix24 #679 and mounted-filesystem #680 remain open live gates; the
closed Yandex #678 gate also recorded `NOT_VERIFIED`, not a provider pass.
Missing authorization, credentials, synthetic provider resources or an approved
mounted target is `NOT_VERIFIED`, never a conditional pass. Connecting a source
does not grant AccessPolicy, source-trust, proposal eligibility or PricingBasis
authority.
