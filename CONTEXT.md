# Spine

Spine coordinates specialized AI executors, deterministic operations and people
inside observable business workflows. This glossary fixes the domain language
used across product, architecture and implementation discussions.

## Language

### Agents and execution

**Capability**:
A stable, versioned business contract describing one kind of work and its typed
input and output.
_Avoid_: Skill, task type, agent method

**Agent**:
A catalogued specialized executor that provides one or more related capabilities.
It is an identity, not a chat session, workflow or particular model.
_Avoid_: Bot, assistant, workflow

**Agent Version**:
An immutable release of an Agent that pins its capabilities and operational
configuration for reproducible execution.
_Avoid_: Agent configuration, current agent

**Agent Implementation**:
An executable realization of an Agent Version, whether hosted inside Spine or
reached through an external runtime.
_Avoid_: Agent class, bot code

**Agent Package**:
A versioned distribution of an Agent Implementation, its manifest, handlers,
schemas, skills, tests and evaluation assets.
_Avoid_: Agent, workflow, runtime team

**Skill**:
A reusable package of agent-readable instructions, references, assets and
declared utilities used by an Agent Implementation. A Skill is not independently
invoked by a workflow and does not replace a Capability contract.
_Avoid_: Capability, Agent, unrestricted script bundle

**Agent Binding**:
The versioned selection of an Agent Version to fulfil a Capability in a defined
workspace, environment or workflow scope.
_Avoid_: Agent lookup, routing rule

**Agent Run**:
One recorded invocation of a pinned Agent Version for a workflow step.
_Avoid_: Session, conversation

**Code Step**:
A deterministic workflow step bound directly to a versioned transformation or
business rule rather than resolved through an Agent Binding.
_Avoid_: Agent, code agent

### Sources and projections

**Context Graph**:
A versioned, evidence-linked semantic projection of domain entities, relations
and ontology extracted from canonical source revisions.
_Avoid_: Source of truth, agent memory, vector index

**Source Object**:
A stably identified object observed through one connection or created by an
explicit upload; its identity does not depend on filename, path or checksum.
_Avoid_: File, document revision, checksum duplicate

**Source Revision**:
An immutable observation of a Source Object's content and explicitly
revision-bearing source metadata, regardless of its later admission disposition.
Operational metadata changes do not create one.
_Avoid_: Mutable document, projection version, ingestion attempt

**Tombstone Revision**:
A deletion-kind Source Revision with deletion provenance and no retrievable
content. When canonically current, it fences every earlier content revision.
_Avoid_: Hard delete, mutable deleted flag, empty document

**Canonical Acceptance**:
An append-only decision that a Source Revision has passed identity, idempotency,
security, completeness and policy checks and may become canonically current.
_Avoid_: Projection success, ingestion status, document approval

**Current Source Revision**:
The Source Revision selected by PostgreSQL as the Source Object's canonical
current state. It does not imply that the revision has been projected or served.
_Avoid_: Active revision, indexed revision, latest upload

**Canonical Boundary**:
A durable, commit-consistent cut of accepted-current source state for one
workspace and environment, with a server-recorded boundary time.
_Avoid_: Build start time, publication time, source timestamp

**Projection Config Version**:
An immutable definition of the ontology, extraction, embedding and processing
settings used to build Context Graph projections.
_Avoid_: Projection Snapshot, active index, Cognee configuration

**Projection Snapshot**:
An immutable logical publication of a Context Graph at a server-selected
Canonical Boundary, with exact source-revision membership, isolated
Security Domain partition receipts and validation lineage. Only an active
validated snapshot serves ordinary retrieval.
_Avoid_: Projection Config Version, mutable index, database copy

**Context Bundle**:
The exact authorized retrieval context and evidence persisted as issued to one
Agent Run.
_Avoid_: Retrieval Result, model context window, current projection

**Projection Drift**:
Canonically accepted source changes after a Projection Snapshot's Canonical
Boundary that are not represented in that snapshot.
_Avoid_: Data loss, source timestamp lag, authorization cache

**Manifest Completeness**:
The property that every source in a Projection Snapshot's declared boundary is
accounted for as included, tombstoned or excluded with an auditable reason.
_Avoid_: Coverage completeness, successful publication

**Coverage Completeness**:
The property that every projection-eligible source in scope is included at its
expected revision or accounted for as tombstoned, with no projection exclusion.
_Avoid_: Manifest Completeness, successful publication

**Projection Rollback**:
Publication of a new successor Projection Snapshot that restores previously
validated projection behavior without reverting canonical state or its boundary.
_Avoid_: Pointer reversal, source rollback, database restore

**Activation Generation**:
A PostgreSQL-controlled monotonic identifier for the active Projection Snapshot
selection of one workspace, environment and projection kind.
_Avoid_: Cognee alias, snapshot boundary, cache version

**Publication Freshness**:
A derived `current` or `stale` assessment of whether canonical drift and the
applicable freshness requirements are satisfied for an active publication.
_Avoid_: Projection health, snapshot lifecycle state, source timestamp

**Publication Health**:
A derived `healthy` or `degraded` assessment of exclusions, failed processing,
artifact integrity, receipt consistency and reconciliation for a publication.
_Avoid_: Publication Freshness, answerability, manifest completeness

### Grounded question answering

**Answer Claim**:
The smallest independently verifiable factual assertion made by an answer.
Claim boundaries follow meaning rather than sentences or formatting.
_Avoid_: Sentence, answer paragraph, citation marker

**Citation**:
An immutable, durably identified reference from an Answer Claim to one exact
evidence item and its Source Revision-scoped provenance.
_Avoid_: Markdown link, excerpt, filename, retrieval result

**Claim Evidence Relationship**:
The explicit semantic relationship connecting an Answer Claim to independently
supporting, jointly supporting or derivation-input evidence.
_Avoid_: Nearby citation marker, source list, opposing evidence

**Citation Validation Record**:
The immutable result and policy lineage of validating one Citation for an
answer at its disclosure boundary.
_Avoid_: Current access decision, citation availability, confidence score

**Grounded Answer**:
An answer for which every externally verifiable Answer Claim is actually
supported by valid authorized evidence from the exact persisted Context Bundle.
It is a binary disclosure predicate, not a quality score.
_Avoid_: Groundedness score, confident answer, answer with citations

**Derived Claim**:
An Answer Claim produced by a reproducible deterministic transformation whose
factual inputs are grounded and whose derived nature is disclosed.
_Avoid_: Model inference, uncited calculation, general knowledge

**Target Completeness Scope**:
The server-derived authorized source scope that must be covered to justify an
exhaustive answer; it does not imply visibility into unauthorized material.
_Avoid_: Entire repository, projected sources, retrieval results

**Observed Coverage**:
The verifiably represented portion of a Target Completeness Scope in the pinned
Projection Snapshot, including explicit lifecycle, exclusion and freshness gaps.
_Avoid_: Target Completeness Scope, retrieved evidence, successful publication

**Q&A Attempt**:
One bounded generation and validation attempt using exactly one pinned Projection
Snapshot, Canonical Boundary and Context Bundle.
_Avoid_: Q&A run, retry loop, mixed-snapshot answer

**Disclosure Dependency**:
A protected source whose content reached generation, directly or through content
carried from an earlier attempt, and therefore constrains later answer disclosure.
_Avoid_: Citation only, retrieved candidate, source mentioned in answer

**Partial Answer**:
A clearly limited answer containing only independently useful Answer Claims that
individually satisfy the Grounded Answer predicate and cannot be invalidated by
missing coverage.
_Avoid_: Best-effort answer, silently incomplete answer, exhaustive answer

**Task Input**:
A protected user-supplied parameter or explicitly hypothetical premise used
conditionally without representing it as verified source evidence.
_Avoid_: Citation, Source Observation, verified fact

**Interpretive Context**:
Currently disclosure-eligible conversation material used to resolve references,
intent and Task Inputs but never to ground an Answer Claim.
_Avoid_: Evidence, session memory authority, prior-answer source

**Partial Fallback Authorization**:
An explicit current-request instruction or applicable accepted preference that
permits a non-exhaustive answer when an exhaustive request cannot be completed.
_Avoid_: Missing all-or-nothing instruction, retrieval success, system default

**Grounded Conflict**:
A Grounded Answer that presents contradictory authorized evidence without
selecting a position unless an approved deterministic precedence rule applies.
_Avoid_: Model arbitration, newest-source-wins, resolved fact

**Q&A Outcome**:
The terminal disposition of one question-answering run: answered,
clarification-required, abstained, denied, failed or cancelled.
_Avoid_: Answer kind, interaction state, HTTP status

**Clarification Required**:
A Q&A Outcome that ends the current run with a safe request to disambiguate the
question while allowing the broader user interaction to continue.
_Avoid_: Abstention, failed run, follow-up answer

**Abstention**:
A Q&A Outcome in which required checks completed but no Grounded Answer could be
established; it is distinct from denied access and operational failure.
_Avoid_: Error, permission denial, empty answer

**Disclosure-Safe Reason**:
A non-content-bearing explanation of a Q&A Outcome that does not reveal
protected source existence, identity, content or lifecycle details.
_Avoid_: Internal diagnostic, raw error, source count

### Access and authorization

**Actor Reference**:
An identity used to attribute, initiate or assign work to a human, team, Agent or
service. It does not by itself confer authorization.
_Avoid_: Access Principal, permission, grant

**Access Principal**:
A human subject, team or service identity that may receive explicit authority
under an Access Policy. An Agent Deployment acts through its configured service
principal rather than inheriting authority from its creator or administrator.
_Avoid_: Actor Reference, Agent, role

**Acting Subject**:
The authenticated human whose effective authority is carried by a request or an
on-behalf-of execution.
_Avoid_: Actor Reference, current user, Agent

**Administrator**:
The R1 Control Plane role for operational and policy management. It is not a
content-superuser role and does not imply permission to read protected material.
_Avoid_: Superuser, document reader

**Access Policy**:
The versioned authorization rules of a Source Object; its current version governs
access to the object and all derived revisions and evidence.
_Avoid_: Cognee permissions, copied chunk ACL

**Access Grant**:
An allow-only assignment of `read_content` or purpose-scoped `process_content`
authority to an Access Principal within an Access Policy.
_Avoid_: Role, deny entry, administrator permission

**Protected Content**:
Document content, content-bearing metadata or derived output whose disclosure
requires effective `read_content` authority for every contributing source.
_Avoid_: Operational metadata, public metadata

**Admission Authority**:
A narrowly scoped service authority to receive, isolate and security-validate new
content before a Source Object has an active Access Policy.
_Avoid_: process_content, read_content, ingestion superuser

**Policy Template**:
An immutable, pre-approved basis for creating a Source Object's initial Access
Policy when its source, Security Domain, purpose and environment are eligible.
_Avoid_: User-selected ACL preset, default public policy

**Policy Proposal**:
An immutable requested change to policy, membership or service authority that is
approved and activated against an exact expected baseline version.
_Avoid_: Mutable draft grant, direct ACL edit

**Authorization Bootstrap**:
The one-time, out-of-band establishment and sealing of a workspace/environment's
initial administrators, memberships, Policy Templates and service authorities.
_Avoid_: Break-glass access, administrator recovery, ordinary configuration

**Canonical Human Identity**:
The trusted representation of one real human to which all known authentication
aliases are mapped for authorization and independent-approval decisions.
_Avoid_: OIDC subject, username, account

**Trusted Authorization Context**:
The server-derived acting identity, service principal, purpose and operation
against which an access decision is made and immutably recorded.
_Avoid_: Client parameters, Context Profile, request payload

**Security Domain**:
A workspace- and environment-local partition that narrows storage and retrieval
scope as defense in depth but never grants access.
_Avoid_: Access Policy, permission group, dataset authorization

**Context Profile**:
A versioned set of purpose, retrieval, traversal, freshness and resource
restrictions applied after authorization; it can narrow but never grant access.
_Avoid_: Access Policy, document grant
