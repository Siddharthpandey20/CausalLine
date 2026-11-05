"""Multi-agent architectures as data, so one executor can run all of them.

WHY A SPEC AND NOT SEVEN PIPELINES
-----------------------------------
`replay()` re-runs the pipeline and splices logged outputs back in by event id.
It checks the rerun's `(agent_id, kind)` sequence against the original's, and a
shape that differs by one call puts the wrong output into the wrong agent --
the failure `run_pipeline` documents for `workflow`/`workers`. Seven
hand-written executors would be seven chances to get that wrong, and each would
need its own replay audit.

So the architecture is data: an ordered tuple of agents, each declaring what it
reads. One executor (`src/tracing/archpipeline.py`) walks that order and calls
the same primitives `MixedPipeline` already uses -- `_fetch`, `_call`,
`_message`, memory reads -- with the same exposure, ingestion and carrier
records. Replay faithfulness is then a property of the executor, verified once.

The order is **explicit, not derived**. A topological sort would be a second
implementation of the walk, and a tie broken differently between the original
run and the replay is exactly the fault above.

WHAT IS HELD CONSTANT ACROSS ARCHITECTURES, AND WHY
----------------------------------------------------
The task is the same site-code consolidation every mixed-provider run in this
project uses, and the attack channels are the same three. That is deliberate:
the architecture is the independent variable, so the task, the attack and the
scoring must not move with it. A different task per architecture would make the
seven results incomparable, which is the opposite of what the campaign is for.

Provider assignment is **per logical agent and fixed in the spec**, so the same
agent gets the same provider in every trial of that architecture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["AgentSpec", "ArchitectureSpec", "ARCHITECTURES", "architecture"]

# Agent behaviours the executor understands. Each maps to a fixed sequence of
# logged events, which is what keeps replay deterministic.
KINDS = (
    "fetch",      # tool_call + tool_response + web source, then a model call
    "think",      # a model call over whatever is already exposed
    "handoff",    # a model call whose output is addressed to one named peer
    "broadcast",  # a model call published to every member of a shared topic
    "join",       # a model call over several upstreams, fanning in
    "gate",       # a model call that may demand a revision loop upstream
)

PROVIDERS = ("local", "gemini", "nvidia")


@dataclass(frozen=True)
class AgentSpec:
    """One logical agent: a distinct identity with a named responsibility.

    `reads_from` is what makes the graph. It names upstream AGENTS whose
    hand-off message this agent consumes, and it is the only place dependency
    structure lives -- the executor derives exposure from it, so an edge that
    is not here does not exist in the trace either.
    """

    id: str
    role: str                                  # the responsibility, in words
    stage: str                                 # grouping, for reporting only
    kind: str = "think"
    provider: str = "local"
    reads_docs: tuple[int, ...] = ()           # document indices to fetch
    reads_memory: tuple[str, ...] = ()         # memory keys to read
    reads_from: tuple[str, ...] = ()           # upstream agent ids
    emits_to: tuple[str, ...] = ()             # downstream agent ids
    writes_memory: str | None = None           # memory key this agent writes
    loop: int = 1                              # model calls this agent makes

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"{self.id}: unknown kind {self.kind!r}")
        if self.provider not in PROVIDERS:
            raise ValueError(f"{self.id}: unknown provider {self.provider!r}")
        if self.loop < 1:
            raise ValueError(f"{self.id}: loop must be >= 1")


@dataclass(frozen=True)
class ArchitectureSpec:
    """An architecture, and the provenance of its shape."""

    name: str
    source_id: str                             # "S1".."S7" in SOURCES.md
    source_url: str
    evidence: str                              # what the source establishes
    purpose: str                               # the original workflow's job
    agents: tuple[AgentSpec, ...] = ()
    order: tuple[str, ...] = ()                # THE execution order
    shared_topic: tuple[str, ...] = ()         # members of a broadcast group
    notes: tuple[str, ...] = ()                # where the source is silent
    _by_id: dict[str, AgentSpec] = field(default_factory=dict, repr=False,
                                         compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_id", {a.id: a for a in self.agents})

    # --- integrity ---------------------------------------------------------

    def problems(self) -> list[str]:
        """Everything that would make this architecture unrunnable.

        Checked before a campaign spends anything, for the reason
        `Topology.problems` exists: a shape that cannot replay is better
        refused at configuration time than discovered an hour in.
        """
        out: list[str] = []
        ids = [a.id for a in self.agents]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            out.append(f"duplicate agent ids: {dupes}")
        if sorted(self.order) != sorted(ids):
            missing = sorted(set(ids) - set(self.order))
            extra = sorted(set(self.order) - set(ids))
            if missing:
                out.append(f"agents absent from `order`: {missing}")
            if extra:
                out.append(f"`order` names unknown agents: {extra}")
        position = {aid: i for i, aid in enumerate(self.order)}
        for agent in self.agents:
            for upstream in agent.reads_from:
                if upstream not in self._by_id:
                    out.append(f"{agent.id} reads from unknown {upstream!r}")
                elif position.get(upstream, 1 << 30) >= position.get(agent.id, 0):
                    # A consumer scheduled before its producer would read an
                    # output that does not exist yet, and the replay would
                    # splice a different one.
                    out.append(
                        f"{agent.id} reads from {upstream!r}, which runs later"
                    )
            for downstream in agent.emits_to:
                if downstream not in self._by_id:
                    out.append(f"{agent.id} emits to unknown {downstream!r}")
        for member in self.shared_topic:
            if member not in self._by_id:
                out.append(f"shared_topic names unknown agent {member!r}")
        if self.agent_count < 55:
            out.append(f"{self.agent_count} agents; the campaign requires >= 55")
        return out

    # --- counts the campaign has to report separately ----------------------

    @property
    def agent_count(self) -> int:
        """CONFIGURED agents: distinct logical identities in the spec."""
        return len(self.agents)

    @property
    def model_call_count(self) -> int:
        """Model calls one clean execution makes, before any recovery."""
        return sum(a.loop for a in self.agents)

    @property
    def documents_needed(self) -> int:
        highest = 0
        for agent in self.agents:
            for index in agent.reads_docs:
                highest = max(highest, index)
        return highest

    def edges(self) -> list[tuple[str, str]]:
        return [(u, a.id) for a in self.agents for u in a.reads_from]

    def depth(self) -> int:
        """Longest delegation chain, for the structural contrast table."""
        best: dict[str, int] = {}
        for aid in self.order:
            agent = self._by_id[aid]
            best[aid] = 1 + max((best.get(u, 0) for u in agent.reads_from),
                                default=0)
        return max(best.values()) if best else 0

    def fan(self) -> tuple[int, int]:
        """(widest fan-out, widest fan-in)."""
        out: dict[str, int] = {}
        for agent in self.agents:
            for upstream in agent.reads_from:
                out[upstream] = out.get(upstream, 0) + 1
        fan_in = max((len(a.reads_from) for a in self.agents), default=0)
        return (max(out.values(), default=0), fan_in)

    def cross_branch_edges(self) -> int:
        """Edges that leave their producer's stage -- the dependencies that
        make this more than N independent agents reporting to one manager."""
        return sum(1 for a in self.agents for u in a.reads_from
                   if self._by_id[u].stage != a.stage)

    def provider_mix(self) -> dict[str, int]:
        mix: dict[str, int] = {p: 0 for p in PROVIDERS}
        for agent in self.agents:
            mix[agent.provider] += agent.loop
        return mix

    def agent(self, aid: str) -> AgentSpec:
        return self._by_id[aid]

    def to_dict(self) -> dict[str, Any]:
        """Written into the trace header so a replay rebuilds this shape."""
        return {
            "name": self.name,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "evidence": self.evidence,
            "agents": self.agent_count,
            "model_calls": self.model_call_count,
            "order": list(self.order),
            "shared_topic": list(self.shared_topic),
            "depth": self.depth(),
            "fan_out_max": self.fan()[0],
            "fan_in_max": self.fan()[1],
            "cross_branch_edges": self.cross_branch_edges(),
            "provider_mix": self.provider_mix(),
        }


# --------------------------------------------------------------------------
# provider assignment
# --------------------------------------------------------------------------
# Targets from the mission: Ollama ~45%, Gemini ~40%, NVIDIA ~15%. Assignment
# is deterministic from the agent's position so the same logical agent keeps
# the same provider in every trial, which is what makes trials of one
# architecture comparable. Gemini is given the agents that do synthesis and
# review -- the work where a stronger model matters -- rather than a random
# 40%, so "Gemini does meaningful work" is true by construction rather than by
# luck.


# The default share of agents per provider, in twentieths. 9/20 local,
# 8/20 Gemini, 3/20 NVIDIA = 45% / 40% / 15%, the mission's initial target.
# Overridable because §10 requires the campaign to fit the quota actually
# discovered on the target machine rather than a hardcoded tier.
SHARES: dict[str, tuple[int, int, int]] = {
    "target": (9, 8, 3),        # local, gemini, nvidia -- 45/40/15
    "local-heavy": (16, 3, 1),  # when the Gemini quota cannot fund the target
    "local-only": (20, 0, 0),   # offline rehearsal; not a result-bearing mode
}


def assign(index: int, total: int = 0, heavy: bool = False,
           shares: tuple[int, int, int] = SHARES["target"]) -> str:
    """Pick a provider for the agent at `index`, deterministically.

    Deterministic in the index alone, so the same logical agent gets the same
    provider in every trial of its architecture -- which is what makes trials
    comparable and is required by the campaign's fairness rules.

    `heavy` marks synthesis, review and gate roles: the work where a stronger
    model matters. Those prefer the hosted providers, so "Gemini does
    meaningful work" holds by construction rather than by luck. Heavy agents
    are a minority of every architecture, so this bends the mix without
    breaking it.
    """
    local, gemini, nvidia = shares
    if gemini == 0 and nvidia == 0:
        return "local"
    if heavy:
        if gemini == 0:
            return "nvidia" if nvidia else "local"
        return "nvidia" if (nvidia and index % 5 == 4) else "gemini"
    width = local + gemini + nvidia
    slot = index % width
    if slot < local:
        return "local"
    if slot < local + gemini:
        return "gemini"
    return "nvidia"
