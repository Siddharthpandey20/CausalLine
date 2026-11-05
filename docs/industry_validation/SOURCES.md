# Source bibliography for the industry-grounded architecture set

Date: 2026-11-05. Every architecture benchmarked in this campaign is traced to a
source below, and each entry records **what kind of evidence the source is**.
That distinction is load-bearing: this project may not claim a company runs an
architecture in production unless the source establishes it.

| # | Source | Kind of evidence | Architecture it grounds |
|---|---|---|---|
| S1 | [Anthropic — *How we built our multi-agent research system*](https://www.anthropic.com/engineering/multi-agent-research-system) | **Deployed production system** (the Research feature in Claude) | **A** Orchestrator–worker parallel research |
| S2 | [CrewAI — *Hierarchical process*](https://github.com/crewAIInc/crewAI/blob/main/docs/v1.12.2/en/learn/hierarchical-process.mdx) | **Framework pattern** (official docs) | **B** Hierarchical supervisor with manager validation |
| S3 | [Astronomer — *Reference architecture: agentic software development*](https://astronomer.io/docs/learn/reference-architecture-agentic-software-development) | **Operator reference architecture** (vendor engineering docs) | **C** Software delivery pipeline |
| S4 | [OpenAI — *Agent orchestration and handoffs*](https://developers.openai.com/api/docs/guides/agents/orchestration) | **Framework pattern** (official docs) | **D** Delegated-ownership handoff network |
| S5 | [Microsoft AutoGen — *Group Chat*](https://microsoft.github.io/autogen/0.4.9/user-guide/core-user-guide/design-patterns/group-chat.html) | **Framework pattern** (official docs) | **E** Dynamic group collaboration |
| S6 | [AWS — *5 production challenges in multi-agent systems (Deep Insight)*](https://aws.amazon.com/blogs/machine-learning/5-production-challenges-in-multi-agent-systems-and-how-deep-insight-solved-them/) | **Engineering design**, published as an AWS sample and described as "production-grade"; **no named customer deployment** | **F** Enterprise business process with approval gate |
| S7 | [Microsoft — *Designing Multi-Agent Intelligence*](https://developer.microsoft.com/blog/designing-multi-agent-intelligence/) | **Architectural guidance** with three named customer systems (ContraForce, Stemtology, SolidCommerce) | **G** Registry- and classifier-mediated hybrid |
| S8 | [OpenAI — *A practical guide to building AI agents*](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/) | **Guide.** The canonical URL served HTTP 403 and the PDF mirror is a binary stream this environment could not extract, so **no claim in this campaign rests on S8**; S4 supplies the same two patterns from official docs. Recorded for completeness. | — (not relied on) |

## What each source actually establishes

**S1 — the only source here describing a system its authors run in production.**
LeadResearcher (Opus 4) plans and decides how many subagents to spawn;
subagents (Sonnet 4) each take one research task and return findings; a
CitationAgent runs after the research loop to attach citations. The lead spins
up **3–5 subagents in parallel**, each using **3+ tools in parallel**; prompting
guidance is 1 agent for fact-finding, 2–4 for comparisons, **more than 10** for
complex research. The lead **saves its plan to Memory** because context beyond
200k tokens is truncated. Critically for our benchmark: the article states the
lead **cannot steer subagents and subagents cannot coordinate** — this is named
as a bottleneck, not a feature. An appendix variant has subagents write to an
external store and return lightweight references.

**S2** — `process=Process.hierarchical` with a manager that "allocates tasks
among crew members based on their roles and capabilities" and "evaluates
outcomes to ensure they meet the required standards". `manager_llm` or a custom
`manager_agent`; `allow_delegation` is off by default and set explicitly. Flow:
assignment → execution and review → progression.

**S3** — one ticket to a draft PR in six components: planning agent (splits work
into pieces, writing a prompt, acceptance criteria and model choice per piece) →
**developer agents, one per piece, run in parallel** via dynamic task mapping →
**reviewer agents per piece**, checking against that piece's acceptance criteria
→ consolidation agent → deterministic test/lint tasks and PR → human review. The
page does not fully specify reviewer wiring, so our per-piece review edge is
noted as the diagram's implication.

**S4** — two patterns, contrasted. *Manager / agents as tools*: a specialist is
exposed via `as_tool()`, the manager keeps ownership of the final reply.
*Handoff*: the specialist "owns the next response" — the page's phrase is
**"delegated ownership"**, not "decentralized". Example: a Triage agent listing
Billing and Refund agents in `handoffs`. The page does not specify how control
returns.

**S5** — all participants publish and subscribe to one shared topic; a
`GroupChatManager` keeps its own history, selects the next speaker with an LLM,
**excludes the previous speaker** so no participant dominates, and sends
`RequestToSpeak`. Example roles: Writer, Editor, Illustrator, User. The page
says round-robin is also common and that simple rules can beat an LLM selector.
Nesting is stated to be possible but **not exemplified**. There is no single
shared store — the shared state is the message thread.

**S6** — eight agents in three layers: Coordinator → Planner → Plan Reviewer
(human-in-the-loop; the web UI auto-approves after 300 s) → Supervisor →
Coder / Validator / Reporter / Tracker. A Strands *Graph* with conditional
edges; a revision request **loops back to the Planner**, bounded by
`MAX_PLAN_REVISIONS` and `set_max_node_executions(25)`. The `full_plan`
checklist is the central shared artifact. Specialists are invoked
agents-as-tools and keep independent contexts.

**S7** — Orchestrator (routes, preserves context), Classifier (escalates
NLU → SLM → LLM by certainty, returns `IDK` when no intent is found), Agent
Registry (capability metadata, discovery, validation), and domain agents, local
in-process or remote. Shared state is named as Conversation History, Agent State
and Registry Storage. Two deployment models: "Modular Monolith" and
"Microservices". Supervisors are optional and group similar agents.

## Honesty constraints applied

1. **"Production" is claimed only for S1.** S6 is described by its authors as
   production-grade but ships as an AWS sample with no named customer
   deployment; S2, S4, S5 are framework patterns; S3 is operator documentation;
   S7 is guidance with named customer systems but no architecture-level
   deployment detail.
2. **Where a source is silent, the benchmark's choice is recorded as ours.**
   S5's nesting, S3's reviewer wiring and S4's control-return are all
   under-specified upstream; each architecture spec names the gap and what we
   chose.
3. **No architecture is a relabelling of another.** The seven differ in
   topology, not in naming — see `ARCHITECTURES.md` §9 for the structural
   contrast table.
4. **S8 is cited but not relied upon**, because this environment could not
   extract its text. Its two patterns are taken from S4 instead.
