"""A deterministic environment for testing ex-ante recovery gates. No LLM.

WHY A SIMULATOR AND NOT THE SCRIPTED PIPELINE
----------------------------------------------
`ScriptedClient` gives exact ground truth but only for the one workflow shape
`run_pipeline` builds. Every hypothesis worth testing after the structural gate
was falsified is about *topology* -- hubs, fan-in, redundancy, multiple hubs,
influence that only appears several hops down -- and none of those shapes can be
expressed there.

This builds real `Trace` objects, so `contaminate()`, `b2_topology_closure`,
`structural_prior`, `restart_all_cost` and the planner all run unmodified. Only
the *generation* is synthetic.

GROUND TRUTH IS HELD OUTSIDE THE TRACE
---------------------------------------
`Workflow.truth` carries the planted causal relation. Nothing writes it into the
trace and no gate is handed it. A gate learns about influence in exactly one
way: by calling `ProbeOracle.probe(...)`, which charges the event's cost. A
gate's cost is therefore what it spent on probes, and a mechanism that is
accurate but expensive shows up as expensive rather than as accurate.

THREE DISTINCT RELATIONS, AND THE DIFFERENCES ARE THE EXPERIMENT
-----------------------------------------------------------------
    influences(s, e)     s genuinely changed e's output       -- the truth
    detectable(s, e)     a leave-one-out counterfactual can SEE that it did
    carries_span(s, e)   the change left copied text behind

Normally `detectable == influences`. Under **redundancy** a clean source carries
the same material, so removing the planted one alone changes nothing a
comparator can observe: influence is real and every probe is blind to it. That
is `docs/03` #16, and it is the mechanism by which any probe-based gate can
produce an unsafe preservation.

`carries_span` is narrower still: semantic influence changes a decision without
leaving any of the source's own wording behind, so a free text-level check sees
nothing. The adversarial cases exploit exactly that gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from src.tracing.logger import Trace, TraceLogger, read_trace

# One model call's cost, in tokens. Constant, so cost differences between cases
# come from topology rather than from a hidden pricing choice.
EVENT_COST = 100


@dataclass
class GroundTruth:
    influences: set[tuple[str, str]] = field(default_factory=set)
    detectable: set[tuple[str, str]] = field(default_factory=set)
    carries_span: set[tuple[str, str]] = field(default_factory=set)
    planted: list[str] = field(default_factory=list)

    def influenced(self, source: str, event: str) -> bool:
        return (source, event) in self.influences

    def visible(self, source: str, event: str) -> bool:
        return (source, event) in self.detectable


@dataclass
class Workflow:
    path: Path
    trace: Trace
    truth: GroundTruth
    name: str
    topology: str
    pattern: str
    branches: int
    # OBSERVABLE, not ground truth. `removability.check()` is pure string work
    # on the stored prompt -- it asks whether redacting a source actually
    # removes its information, and answers without any model call. A pair that
    # is NOT removable has an unsound leave-one-out verdict (D-066): the
    # counterfactual can come back "clean" because a second source carries the
    # same material, not because nothing was used.
    removable: dict = field(default_factory=dict)

    def is_removable(self, source: str, event: str) -> bool:
        return self.removable.get((source, event), True)

    @property
    def n_restart(self) -> int:
        from src.recovery.policy import restart_all_cost

        return int(restart_all_cost(self.trace))

    def true_region(self) -> set[str]:
        """Events genuinely contaminated, walked over the TRUE relation.

        The contract `metrics.ground_truth_events` uses: give the walk the real
        influence edges and mark every other exposure pair examined, so nothing
        is contaminated by ignorance.
        """
        from src.provenance.contamination import contaminate

        pairs = {(s, e.id) for e in self.trace.events for s in e.exposures}
        region = contaminate(
            self.trace, set(self.truth.planted),
            influence=self.truth.influences,
            checked=pairs - self.truth.influences,
        )
        return set(region.events)

    @property
    def r_replay(self) -> int:
        from src.recovery.policy import event_cost

        return int(sum(event_cost(self.trace, e) for e in self.true_region()))

    @property
    def f_true(self) -> float:
        return min(1.0, self.r_replay / max(1, self.n_restart))

    @property
    def f_structural(self) -> float:
        from src.recovery.sprt_investigate import structural_prior

        return structural_prior(self.trace, self.truth.planted)


# --- the probe oracle ---------------------------------------------------------


@dataclass
class ProbeOracle:
    """The only channel through which a gate may learn about influence."""

    workflow: Workflow
    calls: int = 0
    tokens: int = 0
    log: list[tuple[str, str, bool]] = field(default_factory=list)

    def probe(self, source: str, event: str) -> bool:
        """One counterfactual: does removing `source` change `event`'s output?"""
        from src.recovery.policy import event_cost

        self.calls += 1
        self.tokens += int(event_cost(self.workflow.trace, event))
        answer = self.workflow.truth.visible(source, event)
        self.log.append((source, event, answer))
        return answer

    def probe_group(self, sources: Iterable[str], event: str) -> bool:
        """`group_test`'s primitive: remove a whole group in ONE call. Charged
        as one call, which is the saving group testing exists to deliver."""
        from src.recovery.policy import event_cost

        sources = list(sources)
        self.calls += 1
        self.tokens += int(event_cost(self.workflow.trace, event))
        answer = any(self.workflow.truth.visible(s, event) for s in sources)
        self.log.append(("+".join(sources), event, answer))
        return answer


def full_investigation(workflow: Workflow) -> tuple[int, set[str]]:
    """What the real estimator would spend, and what it would conclude.

    `refine_for_verdict`'s frontier expansion, reproduced over the probe oracle:
    recompute the region, take the unchecked pairs whose source is in it, probe
    one, repeat. A clean verdict upstream removes downstream pairs from the
    region and they are never probed -- which is the behaviour any cheap gate is
    competing against.
    """
    from src.provenance.contamination import contaminate

    oracle = ProbeOracle(workflow)
    verdicts: dict[tuple[str, str], bool] = {}
    all_pairs = {(s, e.id) for e in workflow.trace.events for s in e.exposures}

    while True:
        checked = {p for p in all_pairs if p in verdicts}
        influence = {p for p, hit in verdicts.items() if hit}
        region = contaminate(
            workflow.trace, set(workflow.truth.planted),
            influence=influence, checked=checked,
        )
        pending = [
            (s, e.id)
            for e in workflow.trace.events
            for s in e.exposures
            if s in region.sources and (s, e.id) not in verdicts
        ]
        if not pending:
            return oracle.tokens, set(region.events)
        source, event = pending[0]
        verdicts[(source, event)] = oracle.probe(source, event)


# --- topology builders --------------------------------------------------------

# event id -> exposures it was built with. `TraceLogger` exposes no readable
# trace mid-build and re-reading the file per event would be quadratic.
_EXPOSURES: dict[str, list[str]] = {}


def _log_call(log: TraceLogger, agent: str, parents: list[str] | None,
              exposures: list[str]) -> Any:
    event = log.log_event(agent, "agent_output", parents=parents or None,
                          exposures=list(exposures),
                          output_ref=log.put_content(f"{agent} out",
                                                     kind="output"))
    _EXPOSURES[event.id] = list(exposures)
    log.log_usage("pipeline", model="sim", prompt_tokens=EVENT_COST - 10,
                  output_tokens=10, total_tokens=EVENT_COST,
                  event_id_=event.id, agent_id=agent)
    return event


def _fetch(log: TraceLogger, agent: str, label: str, malicious: bool,
           parents: list[str] | None = None) -> tuple[Any, Any]:
    """An agent retrieves a source ITSELF, so the entry event is inside it.

    This matters more than it looks. `b2_topology_closure` finds a flagged
    source by its `origin_event`; a single shared ingest event would put every
    agent inside the closure of every topology, `f_structural` would be 1.00
    everywhere, and the experiment would be vacuous.
    """
    call = log.log_event(agent, "tool_call", parents=parents or None,
                         tool_id="fetch", exposures=[],
                         inputs_ref=[log.put_content(label, kind="tool_args")])
    response = log.log_event(agent, "tool_response", parents=[call.id],
                             tool_id="fetch", exposures=[],
                             output_ref=log.put_content(label, kind="output"))
    source = log.log_source(kind="web", content=label, malicious=malicious,
                            origin_event=response.id, metadata={"doc": label})
    return response, source


def build(
    name: str,
    topology: str,
    pattern: str,
    branches: int,
    workdir: Path,
    hubs: int = 1,
    chain_len: int = 4,
) -> Workflow:
    """One synthetic workflow with a planted causal relation.

    `topology` -- "hub", "fanout", "chain", "fanin", "multihub"
    `pattern`  -- "none", "one", "all", "redundant", "deep", "semantic_one"
    """
    path = workdir / f"{name}.jsonl"
    influences: set[tuple[str, str]] = set()
    spans: set[tuple[str, str]] = set()
    _EXPOSURES.clear()
    bad_id = ""
    targets: list[Any] = []
    downstream_of: dict[str, list[Any]] = {}
    analyst_events: list[Any] = []

    with TraceLogger(path, meta={"sim": True, "topology": topology,
                                 "pattern": pattern}) as log:
        if topology in ("hub", "multihub"):
            n_hubs = hubs if topology == "multihub" else 1
            per_hub = max(1, branches // n_hubs)
            for h in range(n_hubs):
                agent = f"hub{h + 1}"
                resp, src = _fetch(log, agent, "bad" if h == 0 else "clean",
                                   malicious=(h == 0))
                if h == 0:
                    bad_id = src.id
                ref = log.log_source(kind="web", content="ref",
                                     origin_event=resp.id)
                hub = _log_call(log, agent, [resp.id], [src.id, ref.id])
                targets.append(hub)
                hub_src = log.log_source(kind="agent_message", content="assign",
                                         origin_event=hub.id,
                                         derived_from=hub.id)
                kids = []
                for i in range(per_hub):
                    a = _log_call(log, f"h{h+1}a{i+1}", [hub.id], [hub_src.id])
                    analyst_events.append(a)
                    kids.append(a)
                downstream_of[hub.id] = kids

        elif topology == "fanout":
            for i in range(branches):
                agent = f"a{i + 1}"
                resp, src = _fetch(log, agent, "bad" if i == 0 else "clean",
                                   malicious=(i == 0))
                if i == 0:
                    bad_id = src.id
                a = _log_call(log, agent, [resp.id], [src.id])
                analyst_events.append(a)
            targets = [analyst_events[0]]

        elif topology == "chain":
            resp, src = _fetch(log, "c1", "bad", malicious=True)
            bad_id = src.id
            prev, prev_src = resp, src
            for i in range(chain_len):
                e = _log_call(log, f"c{i + 1}", [prev.id], [prev_src.id])
                analyst_events.append(e)
                prev_src = log.log_source(kind="agent_message", content="step",
                                          origin_event=e.id, derived_from=e.id)
                prev = e
            targets = [analyst_events[0]]
            for i, e in enumerate(analyst_events[:-1]):
                downstream_of[e.id] = [analyst_events[i + 1]]

        elif topology == "fanin":
            producers = []
            for i in range(branches):
                agent = f"p{i + 1}"
                resp, src = _fetch(log, agent, "bad" if i == 0 else "clean",
                                   malicious=(i == 0))
                if i == 0:
                    bad_id = src.id
                pe = _log_call(log, agent, [resp.id], [src.id])
                producers.append(pe)
                analyst_events.append(pe)
            parts = [log.log_source(kind="agent_message", content="part",
                                    origin_event=pe.id, derived_from=pe.id)
                     for pe in producers]
            sink = _log_call(log, "sink", [pe.id for pe in producers],
                             [p.id for p in parts])
            analyst_events.append(sink)
            targets = [producers[0]]
            downstream_of[producers[0].id] = [sink]
        else:
            raise ValueError(f"unknown topology {topology!r}")

        def influence(source: str, event: Any, visible: bool = True) -> None:
            influences.add((source, event.id))
            if visible:
                spans.add((source, event.id))

        head = targets[0]
        if pattern == "none":
            pass
        elif pattern == "all":
            for t in targets:
                influence(bad_id, t)
                for kid in downstream_of.get(t.id, []):
                    for sid in _EXPOSURES.get(kid.id, []):
                        influence(sid, kid)
            for e in analyst_events:
                for sid in _EXPOSURES.get(e.id, []):
                    if sid != bad_id and (sid, e.id) not in influences:
                        influence(sid, e)
        elif pattern in ("one", "semantic_one"):
            visible = pattern == "one"
            influence(bad_id, head, visible)
            kids = downstream_of.get(head.id, [])
            if kids:
                for sid in _EXPOSURES.get(kids[0].id, []):
                    influence(sid, kids[0], visible)
        elif pattern == "redundant":
            influence(bad_id, head, visible=False)
            for kid in downstream_of.get(head.id, []):
                for sid in _EXPOSURES.get(kid.id, []):
                    influence(sid, kid, visible=False)
        elif pattern == "none_blocked":
            # No influence at all, but nothing is removable. A gate that
            # refuses to trust unsound verdicts then has NOTHING it may probe
            # and must fall back on the structural bound -- which is the
            # original falsification, reproduced against the soundness guard.
            pass
        elif pattern == "deep_hidden":
            # The bottleneck IS removable and IS genuinely clean, so a
            # soundness-guarded probe believes it -- correctly. The influence
            # sits further down, behind a non-removable pair no probe can see.
            # H2 was never designed against this.
            if analyst_events:
                last = analyst_events[-1]
                _EXPOSURES.setdefault(last.id, []).append(bad_id)
                log.log_event(
                    last.agent_id, "tool_response", parents=[last.id],
                    tool_id="fetch", exposures=[bad_id],
                    output_ref=log.put_content("late", kind="output"))
                influence(bad_id, last, visible=False)
        elif pattern == "deep":
            # A SECOND entry point, far from the hub. The planted source reaches
            # the hub (no influence there) AND the last branch (real influence).
            # A gate that probes only the hub, finds it clean and concludes the
            # closure collapses is then UNSAFE -- it has cleared a region that
            # is genuinely contaminated by a route the hub does not dominate.
            # This is the falsification case for any single-bottleneck probe.
            if analyst_events:
                last = analyst_events[-1]
                _EXPOSURES.setdefault(last.id, []).append(bad_id)
                log.log_event(
                    last.agent_id, "tool_response", parents=[last.id],
                    tool_id="fetch", exposures=[bad_id],
                    output_ref=log.put_content("late", kind="output"))
                influence(bad_id, last)
        else:
            raise ValueError(f"unknown pattern {pattern!r}")

    trace = read_trace(path)
    # Under redundancy the influence is real and NO probe can see it.
    if pattern == "redundant":
        detectable = set()
    elif pattern == "deep_hidden":
        # Only the hidden deep pair is invisible; everything else behaves.
        detectable = {p for p in influences if p not in spans} and set()
        detectable = {p for p in influences if p in spans}
    else:
        detectable = set(influences)
    truth = GroundTruth(influences=influences, detectable=detectable,
                        carries_span=spans, planted=[bad_id])
    # Under redundancy the planted material is duplicated, so redaction does not
    # remove it and every leave-one-out verdict on those pairs is unsound. This
    # is observable for free; it is not a peek at the truth.
    removable: dict = {}
    if pattern == "redundant":
        for sid, eid in influences:
            removable[(sid, eid)] = False
    elif pattern == "none_blocked":
        for e in trace.events:
            for sid in e.exposures:
                removable[(sid, e.id)] = False
    elif pattern == "deep_hidden":
        for pair in influences:
            if pair not in spans:
                removable[pair] = False
    return Workflow(path=path, trace=trace, truth=truth, name=name,
                    topology=topology, pattern=pattern, branches=branches,
                    removable=removable)
