# Issue #509 Neo4j runtime compatibility v1

Status: **compatible qualification configuration verified; production recovery
remains NOT_VERIFIED**.

The application recovery attempt used Cognee's default `neo4j` dataset handler
from a containerized runner against Neo4j Community. A minimal replay through
the same Cognee handler reproduced `neo4j_multi_database_unsupported` without
calling an LLM or embedding provider. The handler requires server-managed
per-dataset databases, which Community edition does not provide.

A second probe changed only the documented handler and execution placement. A
host-side Cognee 1.5.4 process used `neo4j_community`, created a dedicated
synthetic dataset container and volume, completed a Bolt readiness read with
backend access control enabled, and removed both resources through the public
handler lifecycle. No provider call or application projection retry occurred.

The compatible local qualification path is therefore a host-side runner with
the pinned Community image, `neo4j_community`, a dedicated encryption binding
and Docker access. A containerized runner must instead use Neo4j Enterprise or
AuraDB with the default `neo4j` handler. Disabling backend access control is not
an acceptable workaround because it removes the required per-dataset isolation.

This evidence proves only the graph runtime configuration. A fresh recovered
application target, separately authorized provider-backed projection, normal
publication, one permitted-source application read and independent evidence
review are still required.
