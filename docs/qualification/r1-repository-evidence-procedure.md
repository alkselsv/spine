# R1 repository evidence procedure

This procedure closes only the repository and PostgreSQL portion of R1. It does
not qualify a production environment, a real model provider, or a browser.

The `r1-regression-gate` job performs three independent executions at the exact
checked-out commit:

1. apply the current migration graph to a fresh PostgreSQL 17 target;
2. upgrade every retained revision to the single current head on another fresh
   PostgreSQL 17 target;
3. run the remaining repository suite and emit JUnit metadata with every skip's
   stable node ID and reported reason. This execution deselects only the two
   migration-target nodes already executed by steps 1 and 2, so the three
   executions collectively preserve complete repository coverage without
   repeating either expensive migration qualification.

The repository execution also reports its 50 slowest tests in the CI log. This
timing summary contains test identities and durations only; it is diagnostic
output and is not part of the attested qualification artifact.

The collector matches the observed skips exactly against
`r1-repository-skip-policy-v1.json`. Unknown, changed, required, unclassified,
or stale policy entries make qualification fail. Target receipts contain only
PostgreSQL major, graph coverage, status, and a one-way target fingerprint.

On a push to `main`, `actions/attest@v4` signs the evidence artifact's build
provenance. Pull-request artifacts are intentionally not trusted qualification
evidence. GitHub documents that artifact attestation verification binds the
artifact to its repository and signer workflow, and supports enforcing the
source commit digest:

- <https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations>
- <https://cli.github.com/manual/gh_attestation_verify>

## Independent reproduction

Download `r1-repository-evidence` from a successful `main` workflow run, record
its SHA-256 digest, and run:

```bash
uv sync --extra dev --locked
uv run spine-r1-repository-verify \
  --artifact r1-repository-evidence.json \
  --artifact-digest <sha256> \
  --head-sha <40-character-main-commit> \
  --skip-policy docs/qualification/r1-repository-skip-policy-v1.json \
  --output r1-repository-qualification-result.json
```

The verifier invokes `gh attestation verify` with the exact repository, signer
workflow, `refs/heads/main`, source commit digest, and GitHub-hosted-runner
constraints. Exit code `0` means PASS, `1` means a verified artifact evaluated
to FAIL, and `2` means the input could not be evaluated. Preserve the downloaded
artifact digest, workflow run URL from the signed certificate, verifier result,
and exact commit SHA in any later immutable Issue #511 qualification record.
