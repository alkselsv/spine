# Spine

Spine coordinates specialized AI executors, deterministic operations and people
inside observable business workflows. This glossary fixes the domain language
used across product, architecture and implementation discussions.

## Language

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

**Context Graph**:
A versioned, evidence-linked semantic projection of domain entities, relations
and ontology extracted from canonical source revisions.
_Avoid_: Source of truth, agent memory, vector index

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
