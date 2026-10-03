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
