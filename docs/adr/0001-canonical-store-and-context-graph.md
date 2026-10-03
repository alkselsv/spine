---
status: accepted
---

# Canonical store and mandatory Context Graph

PostgreSQL is the source of truth for Spine-owned state and source revisions, but
vector retrieval alone is insufficient for the product. Spine therefore requires
a versioned, evidence-linked Context Graph containing ontology, domain entities
and relations; it remains a rebuildable projection behind the Context Broker, so
Cognee or its graph-store implementation can be replaced without changing
canonical facts, workflows or agents.

Cognee is allowed to perform the substantial projection work—parsing and
chunking, embeddings, ontology-driven entity/relation extraction, consolidation,
graph/vector writes and hybrid retrieval—behind a Spine-owned
`ContextProjection` interface. Spine owns immutable source revisions, access
policy, published ontology and projection versions, activation/evaluation and
the external evidence contract; it does not duplicate every projected node,
edge or embedding in PostgreSQL. A probabilistic projection result becomes a
canonical business fact only through separate domain validation or a human
decision.
