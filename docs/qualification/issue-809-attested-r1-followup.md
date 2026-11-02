# Issue #809 attested R1 repository follow-up

Status: **R1 repository and migration boundary PASS** for integrated `main`
commit `bfd4887e62e543ccdabebd06c8fc7777e239bab3`.

This follow-up is metadata-only and supersedes only the R1 repository result
in the historical #511 qualification. It does not edit that record, qualify a
production deployment, exercise a model provider or browser, or change the
overall release decision from **NO-GO**. R2–R16 require separate current
evidence, so R17 remains FAIL.

## Verified execution

GitHub Actions run [41455426404](https://github.com/alkselsv/spine/actions/runs/41455426404),
job `125310772293`, completed successfully on GitHub-hosted infrastructure.
The main-only attestation step signed `issue-809-r1-repository-evidence.json`
through `.github/workflows/test.yml@refs/heads/main` for the exact target
commit.

The immutable input is `issue-809-r1-repository-qualification-input.json`.
The independently reproduced result is
`issue-809-r1-repository-qualification-result.json`.

- Actions artifact ID: `13147569337`
- Actions container digest: `sha256:dc973112315b871d44ef713ff8931a73dec2e9743d53b0cf6cd33bd7d722686f`
- Signed evidence subject digest: `sha256:33e8f07957ac697f28af2cec4a486db05f998a472dfb0ad531786aea9e59eb37`
- Downloaded Sigstore bundle digest: `sha256:5e441b8a1b48d7a87e185641a4577b330cd6f910610df5c9d3ebcc0aa1e3a7ec`
- Reproduced result digest: `sha256:a11bc9071595eb7b1ed921e377bc30a9d7311eb67f98756d12ea0f9ec3b0fbc5`

The signed evidence records two distinct PostgreSQL 17 target fingerprints,
the single Alembic head `20261227_95`, clean catalog validation, upgrade
coverage of all 96 retained revisions, and `5822 passed, 0 failed, 0 errors,
7 skipped`. Every skip is individually identified and classified optional by
`r1-skip-policy:2026-11-02`; no required or unclassified skip remains.

## Independent reproduction

From the repository root:

```bash
gh attestation verify \
  docs/qualification/issue-809-r1-repository-evidence.json \
  --repo alkselsv/spine \
  --signer-workflow alkselsv/spine/.github/workflows/test.yml \
  --source-digest bfd4887e62e543ccdabebd06c8fc7777e239bab3 \
  --source-ref refs/heads/main \
  --deny-self-hosted-runners

uv run spine-r1-repository-verify \
  --artifact docs/qualification/issue-809-r1-repository-evidence.json \
  --artifact-digest 33e8f07957ac697f28af2cec4a486db05f998a472dfb0ad531786aea9e59eb37 \
  --head-sha bfd4887e62e543ccdabebd06c8fc7777e239bab3 \
  --skip-policy docs/qualification/r1-repository-skip-policy-v1.json \
  --output /tmp/issue-809-r1-result.json
```

The verifier must exit `0`, and its parsed JSON must equal the committed
result. Exit `1` is an evaluated FAIL; exit `2` means the evidence could not be
evaluated. No private corpus, provider payload, credentials or protected
content is present in this package.
