# Domain Docs

Spine uses a single-context domain-documentation layout.

## Before exploring, read these

- `CONTEXT.md` at the repository root, when it exists.
- Relevant ADRs under `docs/adr/`.

If these files do not exist, proceed silently. Do not suggest creating them
upfront. The domain-modeling workflows create them lazily when terminology or
architectural decisions are resolved.

## File structure

```text
/
├── CONTEXT.md
├── docs/
│   └── adr/
│       ├── 0001-example-decision.md
│       └── 0002-another-decision.md
└── src/
```

## Use the glossary vocabulary

When naming a domain concept in code, issues, specifications or tests, use the
term defined in `CONTEXT.md`. Do not drift to synonyms the glossary explicitly
rejects.

If a required concept is absent, reconsider whether new terminology is needed
or record the gap for the `domain-modeling` skill.

## Flag ADR conflicts

If proposed work contradicts an existing ADR, identify the conflict explicitly
instead of silently overriding the decision.
