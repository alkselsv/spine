# Issue #560 — Q16-01 deployed runtime readiness

Decision: **BLOCKED** for commit `9784577d276445aa5e4e52b513ca89e653ad7e55` and deployment fingerprint `5f8cc32cc79d6127be50cf6363a5006cbebd063f129565a12bdea71a81b53b23`.

The isolated `spine560` SOURCE profile starts reproducibly. PostgreSQL is at migration head `20261120_57`; all three application roles are non-superuser and cannot create roles or databases; 74 of 82 Spine tables have both enabled and forced RLS. Original storage is readable and writable. Temporal is serving the `default` namespace, and both workflow and activity pollers are registered on `spine-r1-qa`. API, Q&A worker, dispatcher, Web, Neo4j/APOC and the versioned provider binding agree on their declared profile identities. Startup and the probe made no provider call.

The exact synthetic run reached API admission, the canonical PostgreSQL outbox, bounded dispatcher delivery and a running Temporal workflow. It did not enter `AnswerQuestionCapability`: the clean SOURCE target has no active Q&A publication, so production configuration preparation fails closed before attempt allocation. The canonical run remains `admitted`, authenticated capture cannot complete, and supported cancellation returns the bounded unavailable response. Infrastructure cancellation was requested only to stop the two synthetic retry loops.

This exposes a dependency cycle: #560 requires a terminal capability execution, while creating and activating the needed projection is assigned to #562, which is blocked by #560. PASS requires resolving that ordering or supplying an already active, independently approved projection for the exact #560 profile. It also requires Security-owned existing fixtures for insufficient-role, revoked/expired and stale-generation live denials. The selected database exposes PGVector 0.8.7 but has not installed the extension.

The full metadata-only manifest, component digests, safe correlations and gate results are in `issue-560-q16-01-runtime-readiness-v1.json`. It intentionally contains no credentials, network locations, source identifiers, questions, answers, provider payloads or raw exceptions.
