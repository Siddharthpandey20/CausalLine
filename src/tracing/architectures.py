"""The seven industry-grounded architectures, as specs.

Each is traced to a source in `docs/industry_validation/SOURCES.md` and the
structural claim for each is checked by `ArchitectureSpec.problems()` and by
`tests/test_architectures.py`. The seven differ in TOPOLOGY, not in naming --
`docs/industry_validation/ARCHITECTURES.md` §9 carries the contrast table, and
a test asserts no two share a structural fingerprint.

Where a source is silent, the choice is ours and is recorded in `notes`.
"""

from __future__ import annotations

from src.tracing.archspec import AgentSpec, ArchitectureSpec, assign

__all__ = ["ARCHITECTURES", "architecture", "names"]


def _a(aid: str, role: str, stage: str, index: int, *, kind: str = "think",
       heavy: bool = False, docs: tuple[int, ...] = (),
       memory: tuple[str, ...] = (), reads: tuple[str, ...] = (),
       emits: tuple[str, ...] = (), writes: str | None = None,
       loop: int = 1) -> AgentSpec:
    return AgentSpec(id=aid, role=role, stage=stage, kind=kind,
                     provider=assign(index, 0, heavy=heavy),
                     reads_docs=docs, reads_memory=memory, reads_from=reads,
                     emits_to=emits, writes_memory=writes, loop=loop)


# --------------------------------------------------------------------------
# A. Orchestrator-worker parallel research  (S1, Anthropic -- production)
# --------------------------------------------------------------------------
def _arch_a() -> ArchitectureSpec:
    agents: list[AgentSpec] = []
    order: list[str] = []
    n = 0

    # The lead plans and writes the plan to Memory, because S1 records that
    # context beyond 200k tokens is truncated and the lead saves its plan there.
    agents.append(_a("lead", "analyses the query, plans, decides how many "
                     "subagents to spawn, synthesises the result",
                     "lead", 0, heavy=True, writes="research/plan", loop=2))
    order.append("lead")

    for i in range(1, 4):                      # research planners
        n += 1
        aid = f"plan{i}"
        agents.append(_a(aid, f"decomposes branch {i} of the plan into "
                         f"subagent tasks", "planning", n, heavy=True,
                         memory=("research/plan",), reads=("lead",)))
        order.append(aid)

    # 12 search agents, each fetching its own document. S1: subagents use 3+
    # tools in parallel and run their own searches.
    for i in range(1, 13):
        n += 1
        aid = f"search{i}"
        agents.append(_a(aid, f"issues search {i} and returns raw results",
                         "search", n, kind="fetch", docs=(i,),
                         reads=(f"plan{(i - 1) % 3 + 1}",)))
        order.append(aid)

    for i in range(1, 9):                      # scrapers
        n += 1
        aid = f"scrape{i}"
        agents.append(_a(aid, f"extracts content from the pages search "
                         f"{i} and {i + 8} returned", "scrape", n,
                         reads=(f"search{i}", f"search{(i % 12) + 1}")))
        order.append(aid)

    for i in range(1, 9):                      # source validators
        n += 1
        aid = f"srcval{i}"
        agents.append(_a(aid, f"checks provenance and reliability of "
                         f"scraped batch {i}", "validate", n,
                         reads=(f"scrape{i}",)))
        order.append(aid)

    # 24 specialist researchers. S1 is explicit that subagents CANNOT
    # coordinate -- it names this as a bottleneck -- so there are no
    # subagent-to-subagent edges. Each reads the plan from memory and one
    # validated batch, exactly the star shape the article describes.
    for i in range(1, 25):
        n += 1
        aid = f"sub{i:02d}"
        agents.append(_a(aid, f"researches sub-question {i} against its own "
                         f"validated sources", "subagent", n,
                         memory=("research/plan",),
                         reads=(f"srcval{(i - 1) % 8 + 1}",)))
        order.append(aid)

    for i in range(1, 5):                      # synthesis
        n += 1
        aid = f"synth{i}"
        mine = tuple(f"sub{j:02d}" for j in range(i, 25, 4))
        agents.append(_a(aid, f"synthesises findings from subagents {i}, "
                         f"{i + 4}, ... into a section", "synthesis", n,
                         kind="join", heavy=True, reads=mine))
        order.append(aid)

    n += 1
    agents.append(_a("cite", "runs after the research loop and attaches "
                     "citations to every claim", "citation", n, kind="join",
                     heavy=True, reads=tuple(f"synth{i}" for i in range(1, 5))))
    order.append("cite")

    return ArchitectureSpec(
        name="A-parallel-research",
        source_id="S1",
        source_url="https://www.anthropic.com/engineering/multi-agent-research-system",
        evidence="deployed production system (the Research feature in Claude)",
        purpose="answer an open research question by parallel search, "
                "extraction, validation and synthesis",
        agents=tuple(agents), order=tuple(order),
        notes=(
            "S1 states the lead cannot steer subagents and subagents cannot "
            "coordinate. That is modelled literally: there are NO "
            "subagent-to-subagent edges, which is what makes this a star and "
            "not a mesh.",
            "The lead writes its plan to memory and subagents read it, per "
            "S1's Memory note. This is the architecture's only shared state.",
            "S1 gives no fixed subagent count; 24 sits above its "
            "'more than 10 subagents' guidance for complex research.",
        ))


# --------------------------------------------------------------------------
# B. Hierarchical supervisor with manager validation  (S2, CrewAI)
# --------------------------------------------------------------------------
def _arch_b() -> ArchitectureSpec:
    agents: list[AgentSpec] = []
    order: list[str] = []
    n = 0

    agents.append(_a("manager", "allocates tasks by role and capability, then "
                     "evaluates outcomes against the required standard",
                     "manager", 0, heavy=True, kind="gate", loop=2))
    order.append("manager")

    for s in range(1, 5):                      # sub-supervisors
        n += 1
        aid = f"subsup{s}"
        agents.append(_a(aid, f"sub-supervisor for team {s}; re-delegates "
                         f"within its team", "subsupervisor", n, heavy=True,
                         reads=("manager",)))
        order.append(aid)

    for s in range(1, 5):                      # team leads
        n += 1
        aid = f"tlead{s}"
        agents.append(_a(aid, f"team {s} lead; splits the team's share of "
                         f"the work", "teamlead", n, reads=(f"subsup{s}",)))
        order.append(aid)

    for s in range(1, 5):                      # 11 workers per team = 44
        for w in range(1, 12):
            n += 1
            aid = f"w{s}_{w:02d}"
            docs = ((s - 1) * 11 + w,) if w <= 3 else ()
            agents.append(_a(aid, f"team {s} worker {w}", "worker", n,
                             kind="fetch" if docs else "think", docs=docs,
                             reads=(f"tlead{s}",)))
            order.append(aid)

    for s in range(1, 5):                      # per-team validators
        for v in range(1, 4):
            n += 1
            aid = f"val{s}_{v}"
            mine = tuple(f"w{s}_{w:02d}" for w in range(v, 12, 3))
            agents.append(_a(aid, f"validates team {s} worker outputs "
                             f"{v}, {v + 3}, ...", "validate", n, kind="join",
                             reads=mine))
            order.append(aid)

    for i in range(1, 4):                      # reporting
        n += 1
        aid = f"report{i}"
        mine = tuple(f"val{s}_{i}" for s in range(1, 5))
        agents.append(_a(aid, f"consolidates validated team output into "
                         f"report section {i}", "report", n, kind="join",
                         heavy=True, reads=mine))
        order.append(aid)

    return ArchitectureSpec(
        name="B-hierarchical-supervisor",
        source_id="S2",
        source_url="https://github.com/crewAIInc/crewAI/blob/main/docs/v1.12.2/en/learn/hierarchical-process.mdx",
        evidence="framework pattern (official CrewAI documentation)",
        purpose="allocate a body of work across role-specialised crews under "
                "a manager that reviews outcomes before accepting them",
        agents=tuple(agents), order=tuple(order),
        notes=(
            "S2's manager both delegates and 'evaluates outcomes to ensure "
            "they meet the required standards'. The manager is therefore a "
            "`gate` with loop=2: one delegation pass and one validation pass.",
            "S2 does not say how `manager_llm` and a custom `manager_agent` "
            "interact when both are set; only one manager agent is modelled.",
            "Three-level delegation (manager -> sub-supervisor -> team lead -> "
            "worker) follows S7's suggestion of grouping similar agents under "
            "supervisors, which S2 alone does not specify.",
        ))


# --------------------------------------------------------------------------
# C. Software delivery pipeline  (S3, Astronomer)
# --------------------------------------------------------------------------
def _arch_c() -> ArchitectureSpec:
    agents: list[AgentSpec] = []
    order: list[str] = []
    n = 0

    agents.append(_a("planner", "reads the ticket, explores the codebase and "
                     "splits the work into pieces with acceptance criteria",
                     "plan", 0, heavy=True))
    order.append("planner")

    for i in range(1, 3):
        n += 1
        aid = f"arch{i}"
        agents.append(_a(aid, f"designs interfaces for half {i} of the "
                         f"change", "architect", n, heavy=True,
                         reads=("planner",)))
        order.append(aid)

    # 20 independent lanes. S3 maps one developer task per piece of work, in
    # parallel; the reviewer checks ITS piece against ITS acceptance criteria.
    # So dev and reviewer are PAIRED, which is this architecture's signature:
    # twenty two-agent lanes rather than one wide fan-in.
    for i in range(1, 21):
        n += 1
        dev = f"dev{i:02d}"
        agents.append(_a(dev, f"implements piece {i} against its prompt and "
                         f"acceptance criteria", "develop", n,
                         kind="fetch" if i <= 12 else "think",
                         docs=(i,) if i <= 12 else (),
                         reads=(f"arch{(i - 1) % 2 + 1}",)))
        order.append(dev)
        n += 1
        rev = f"rev{i:02d}"
        agents.append(_a(rev, f"reviews piece {i} against its acceptance "
                         f"criteria and runs its tests", "review", n,
                         reads=(dev,)))
        order.append(rev)

    for i in range(1, 7):                      # integration
        n += 1
        aid = f"integ{i}"
        mine = tuple(f"rev{j:02d}" for j in range(i, 21, 6))
        agents.append(_a(aid, f"integrates reviewed pieces {i}, {i + 6}, ...",
                         "integrate", n, kind="join", reads=mine))
        order.append(aid)

    for i in range(1, 9):                      # deterministic checks
        n += 1
        aid = f"test{i}"
        agents.append(_a(aid, f"runs suite {i} over the integrated change",
                         "test", n, reads=(f"integ{(i - 1) % 6 + 1}",)))
        order.append(aid)

    for i in range(1, 4):
        n += 1
        aid = f"sec{i}"
        agents.append(_a(aid, f"security review {i}", "security", n,
                         heavy=True, reads=(f"test{i}", f"test{i + 3}")))
        order.append(aid)

    n += 1
    agents.append(_a("consolidate", "reconciles the pieces into one coherent "
                     "change", "consolidate", n, kind="join", heavy=True,
                     reads=tuple(f"sec{i}" for i in range(1, 4))
                           + tuple(f"integ{i}" for i in range(1, 7))))
    order.append("consolidate")
    n += 1
    agents.append(_a("pr", "opens the draft pull request for human review",
                     "deliver", n, reads=("consolidate",)))
    order.append("pr")

    return ArchitectureSpec(
        name="C-software-delivery",
        source_id="S3",
        source_url="https://astronomer.io/docs/learn/reference-architecture-agentic-software-development",
        evidence="operator reference architecture (vendor engineering docs)",
        purpose="turn one ticket into a draft pull request for human approval",
        agents=tuple(agents), order=tuple(order),
        notes=(
            "S3's developer tasks are created by dynamic task mapping, one per "
            "piece; 20 pieces is our choice of scale.",
            "S3 does not give the exact reviewer wiring. Per-piece pairing is "
            "the diagram's implication and is recorded here as our reading.",
            "The deterministic test and lint tasks are real Airflow tasks in "
            "S3, i.e. not model calls. They are modelled as agents here "
            "because the benchmark scores agent work; this inflates this "
            "architecture's model-call count relative to S3 and is stated "
            "rather than hidden.",
        ))


# --------------------------------------------------------------------------
# D. Delegated-ownership handoff network  (S4, OpenAI)
# --------------------------------------------------------------------------
def _arch_d() -> ArchitectureSpec:
    agents: list[AgentSpec] = []
    order: list[str] = []
    n = 0

    for i in range(1, 4):                      # triage agents
        n += 1
        aid = f"triage{i}"
        agents.append(_a(aid, f"triages intake stream {i} and hands off to a "
                         f"specialist", "triage", n, kind="handoff",
                         docs=(i,), emits=(f"c{i}_1",)))
        order.append(aid)

    # Eight chains of seven. Each agent hands the conversation to exactly ONE
    # peer, which then owns the reply -- S4's "delegated ownership". There is
    # no permanent central supervisor anywhere in this architecture, which is
    # what distinguishes it from A, B, F and G.
    ROLES = ("billing", "refund", "entitlement", "escalation",
             "compliance", "remediation", "closure")
    for c in range(1, 9):
        previous = f"triage{(c - 1) % 3 + 1}" if c <= 3 else f"c{c - 3}_7"
        for step, role in enumerate(ROLES, start=1):
            n += 1
            aid = f"c{c}_{step}"
            nxt = f"c{c}_{step + 1}" if step < 7 else ()
            agents.append(_a(
                aid, f"{role} specialist on chain {c}; owns the reply after "
                     f"the handoff", "chain", n, kind="handoff",
                heavy=(role in ("compliance", "closure")),
                docs=(3 + c,) if step == 1 and c <= 9 else (),
                reads=(previous,), emits=(nxt,) if nxt else ()))
            order.append(aid)
            previous = aid

    return ArchitectureSpec(
        name="D-handoff-network",
        source_id="S4",
        source_url="https://developers.openai.com/api/docs/guides/agents/orchestration",
        evidence="framework pattern (official OpenAI documentation)",
        purpose="route a request through whichever specialists must own it, "
                "each taking over the conversation in turn",
        agents=tuple(agents), order=tuple(order),
        notes=(
            "S4's phrase is 'delegated ownership', not 'decentralized'. The "
            "receiving specialist owns the next response; the sender does not "
            "retain it.",
            "S4 does not specify how control RETURNS after a handoff. Modelled "
            "as not returning: a chain runs forward only, and later chains "
            "pick up from an earlier chain's closure agent.",
            "This is the deepest and narrowest architecture in the set: "
            "fan-out is 1 almost everywhere, and the delegation depth is the "
            "largest of the seven.",
        ))


# --------------------------------------------------------------------------
# E. Dynamic group collaboration  (S5, AutoGen Group Chat)
# --------------------------------------------------------------------------
def _arch_e() -> ArchitectureSpec:
    agents: list[AgentSpec] = []
    order: list[str] = []
    n = 0
    topic: list[str] = []

    agents.append(_a("chair", "outer group-chat manager; selects the next "
                     "group to speak", "manager", 0, heavy=True, loop=2))
    order.append("chair")

    # Three nested groups. S5 says nesting is possible but gives no example,
    # so the nesting here is ours. Within a group every member publishes to the
    # group's shared topic and reads the previous speaker -- and the manager
    # excludes the previous speaker from the next selection, which is why the
    # read edge is to the speaker BEFORE last within a round.
    ROLES = ("writer", "editor", "illustrator", "critic", "factchecker",
             "stylist")
    for g in range(1, 4):
        n += 1
        gm = f"gm{g}"
        agents.append(_a(gm, f"group {g} chat manager; selects the next "
                         f"speaker and excludes the previous one",
                         "groupmanager", n, heavy=True, reads=("chair",)))
        order.append(gm)
        previous: tuple[str, ...] = (gm,)
        members: list[str] = []
        for r in range(1, 20):
            n += 1
            aid = f"g{g}_{r:02d}"
            role = ROLES[(r - 1) % len(ROLES)]
            agents.append(_a(
                aid, f"group {g} {role} (turn {r}); publishes to the group "
                     f"topic", "member", n, kind="broadcast",
                heavy=(role in ("editor", "critic")),
                docs=((g - 1) * 4 + ((r - 1) % 4) + 1,) if r <= 4 else (),
                reads=previous))
            order.append(aid)
            members.append(aid)
            topic.append(aid)
            # Read the two prior speakers, excluding the immediately previous
            # one from the manager's selection but still receiving its
            # broadcast: the thread IS the shared state in S5.
            previous = tuple(members[-2:]) if len(members) >= 2 else (aid,)

    return ArchitectureSpec(
        name="E-group-collaboration",
        source_id="S5",
        source_url="https://microsoft.github.io/autogen/0.4.9/user-guide/core-user-guide/design-patterns/group-chat.html",
        evidence="framework pattern (official AutoGen documentation)",
        purpose="let a group of specialists converge on one artefact through a "
                "shared conversation with dynamic speaker selection",
        agents=tuple(agents), order=tuple(order), shared_topic=tuple(topic),
        notes=(
            "S5 has no single shared store: the shared state is the message "
            "thread on the group topic. Modelled as broadcast reads rather "
            "than a memory key.",
            "S5's manager excludes the previous speaker from candidate "
            "selection so one participant cannot dominate. Each member "
            "therefore reads the two prior speakers, not one.",
            "S5 states nesting is possible but gives no example or "
            "implementation. The three nested groups are our choice.",
            "This architecture has the widest fan-out of the seven, because a "
            "broadcast is read by every later member of its group.",
        ))


# --------------------------------------------------------------------------
# F. Enterprise business process with approval gate  (S6, AWS Deep Insight)
# --------------------------------------------------------------------------
def _arch_f() -> ArchitectureSpec:
    agents: list[AgentSpec] = []
    order: list[str] = []
    n = 0

    agents.append(_a("coordinator", "routes the request; answers trivia "
                     "itself and hands analysis to the planner", "intake", 0))
    order.append("coordinator")
    n += 1
    agents.append(_a("planner", "breaks the request into a checklist of steps, "
                     "each assigned to an agent", "intake", n, heavy=True,
                     reads=("coordinator",), writes="process/full_plan",
                     loop=2))
    order.append("planner")
    n += 1
    agents.append(_a("plan_reviewer", "human-in-the-loop checkpoint: approves "
                     "the plan or requests a revision", "gate", n,
                     kind="gate", heavy=True, reads=("planner",), loop=2))
    order.append("plan_reviewer")
    n += 1
    agents.append(_a("supervisor", "executes the approved plan by calling the "
                     "specialists as tools", "orchestrate", n, heavy=True,
                     memory=("process/full_plan",), reads=("plan_reviewer",)))
    order.append("supervisor")

    for i in range(1, 5):                      # database agents
        n += 1
        aid = f"db{i}"
        agents.append(_a(aid, f"retrieves dataset partition {i}", "data", n,
                         kind="fetch", docs=(i,), reads=("supervisor",)))
        order.append(aid)

    # S6's Supervisor calls Tracker after EVERY specialist, which is the
    # pattern that makes this architecture's graph distinctive: a progress
    # agent interleaved through the whole execution rather than a final one.
    for i in range(1, 17):
        n += 1
        coder = f"coder{i:02d}"
        agents.append(_a(coder, f"writes and runs analysis step {i}", "execute",
                         n, memory=("process/full_plan",),
                         reads=(f"db{(i - 1) % 4 + 1}",)))
        order.append(coder)
        n += 1
        val = f"validator{i:02d}"
        agents.append(_a(val, f"recomputes and verifies step {i}, and emits "
                         f"citation metadata", "validate", n, reads=(coder,)))
        order.append(val)
        n += 1
        trk = f"tracker{i:02d}"
        agents.append(_a(trk, f"records progress after step {i}", "track", n,
                         reads=(val,)))
        order.append(trk)

    for i in range(1, 9):                      # reporting
        n += 1
        aid = f"reporter{i}"
        mine = tuple(f"tracker{j:02d}" for j in range(i, 17, 8))
        agents.append(_a(aid, f"produces report section {i}", "report", n,
                         kind="join", heavy=True, reads=mine))
        order.append(aid)

    return ArchitectureSpec(
        name="F-enterprise-process",
        source_id="S6",
        source_url="https://aws.amazon.com/blogs/machine-learning/5-production-challenges-in-multi-agent-systems-and-how-deep-insight-solved-them/",
        evidence="engineering design, published as an AWS sample and described "
                 "by its authors as production-grade; no named customer "
                 "deployment",
        purpose="turn a dataset and a natural-language request into a "
                "validated report, through an approved plan",
        agents=tuple(agents), order=tuple(order),
        notes=(
            "S6's `full_plan` checklist is the central shared artefact; "
            "modelled as a memory key the supervisor and every coder read.",
            "S6's plan reviewer is a human gate that auto-approves after 300 s "
            "and can loop a revision back to the planner. Modelled as a `gate` "
            "with loop=2 on both the planner and the reviewer; the loop bound "
            "stands in for MAX_PLAN_REVISIONS.",
            "The tracker-after-every-specialist pattern is taken literally, "
            "which is why this architecture has 16 three-agent execution "
            "triples rather than a flat worker pool.",
        ))


# --------------------------------------------------------------------------
# G. Registry- and classifier-mediated hybrid  (S7, Microsoft)
# --------------------------------------------------------------------------
def _arch_g() -> ArchitectureSpec:
    agents: list[AgentSpec] = []
    order: list[str] = []
    n = 0

    agents.append(_a("orchestrator", "routes requests, preserves context and "
                     "manages request lifecycles", "orchestrate", 0,
                     heavy=True, loop=2))
    order.append("orchestrator")

    # S7's classifier escalates NLU -> SLM -> LLM by certainty and returns IDK
    # when no intent is found. Three agents, deliberately on three different
    # providers, because the escalation is the point.
    for i, (tier, prov) in enumerate(
            (("NLU", "local"), ("SLM", "local"), ("LLM", "gemini")), start=1):
        n += 1
        aid = f"classify{i}"
        agents.append(AgentSpec(
            id=aid, role=f"{tier} intent classifier; escalates when "
                         f"uncertain, returns IDK when no intent is found",
            stage="classify", kind="think", provider=prov,
            reads_from=("orchestrator",) if i == 1 else (f"classify{i - 1}",)))
        order.append(aid)

    for i in range(1, 3):                      # registry
        n += 1
        aid = f"registry{i}"
        agents.append(_a(aid, f"agent registry shard {i}: capability "
                         f"metadata, discovery and validation", "registry", n,
                         reads=("classify3",), writes=f"registry/shard{i}"))
        order.append(aid)

    for s in range(1, 7):                      # optional supervisors
        n += 1
        aid = f"group{s}"
        agents.append(_a(aid, f"optional supervisor grouping domain agents "
                         f"of family {s}", "supervise", n, heavy=True,
                         memory=(f"registry/shard{(s - 1) % 2 + 1}",),
                         reads=(f"registry{(s - 1) % 2 + 1}",)))
        order.append(aid)

    # 24 local (in-process) domain agents and 24 remote ones. S7 draws exactly
    # this distinction, and it is why this architecture pins providers by
    # locality rather than by role.
    for i in range(1, 25):
        n += 1
        aid = f"localdom{i:02d}"
        agents.append(AgentSpec(
            id=aid, role=f"in-process domain agent {i}", stage="localdomain",
            kind="fetch" if i <= 12 else "think", provider="local",
            reads_docs=(i,) if i <= 12 else (),
            reads_from=(f"group{(i - 1) % 6 + 1}",)))
        order.append(aid)
    for i in range(1, 25):
        n += 1
        aid = f"remotedom{i:02d}"
        agents.append(AgentSpec(
            id=aid, role=f"remote domain agent {i}, reached over the network",
            stage="remotedomain", kind="think",
            provider="gemini" if i % 4 else "nvidia",
            reads_from=(f"localdom{i:02d}",)))
        order.append(aid)

    for i in range(1, 5):                      # conversation-history writers
        n += 1
        aid = f"history{i}"
        mine = tuple(f"remotedom{j:02d}" for j in range(i, 25, 4))
        agents.append(_a(aid, f"writes conversation-history shard {i} and "
                         f"reconciles agent state", "state", n, kind="join",
                         heavy=True, reads=mine))
        order.append(aid)

    return ArchitectureSpec(
        name="G-registry-hybrid",
        source_id="S7",
        source_url="https://developer.microsoft.com/blog/designing-multi-agent-intelligence/",
        evidence="architectural guidance with three named customer systems; no "
                 "architecture-level deployment detail",
        purpose="route enterprise requests to discoverable domain agents, "
                "local or remote, through a classifier and a registry",
        agents=tuple(agents), order=tuple(order),
        notes=(
            "S7's classifier escalates NLU -> SLM -> LLM and returns IDK. The "
            "three tiers are three agents on three providers, so the "
            "escalation is visible in the provider mix.",
            "S7 distinguishes local in-process agents from remote ones "
            "reached over the network. Providers here are pinned by LOCALITY, "
            "not by role, which no other architecture in the set does.",
            "S7 names Conversation History, Agent State and Registry Storage "
            "as shared state; the registry shards and history writers model "
            "the first and third. S7's supervisors are optional and are "
            "included.",
        ))


_BUILDERS = {
    "A": _arch_a, "B": _arch_b, "C": _arch_c, "D": _arch_d,
    "E": _arch_e, "F": _arch_f, "G": _arch_g,
}

ARCHITECTURES: dict[str, ArchitectureSpec] = {}


def architecture(key: str) -> ArchitectureSpec:
    """Build (and cache) one architecture by its letter or name."""
    letter = key.strip().upper()[:1]
    if letter not in _BUILDERS:
        for built in (architecture(k) for k in _BUILDERS):
            if built.name == key:
                return built
        raise ValueError(f"unknown architecture {key!r}; "
                         f"have {sorted(_BUILDERS)}")
    if letter not in ARCHITECTURES:
        ARCHITECTURES[letter] = _BUILDERS[letter]()
    return ARCHITECTURES[letter]


def names() -> list[str]:
    return [architecture(k).name for k in sorted(_BUILDERS)]
