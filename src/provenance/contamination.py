"""
Contamination propagation.

Given the detector's verdict -- "these sources are malicious" -- work out
which events actually became contaminated, and which stayed clean.

THE RULE THIS MODULE EXISTS TO ENFORCE
--------------------------------------
Contamination walks exactly two kinds of edge:

    influence edge   source -> event    this source changed this output
    derived_from     event  -> source   this source *is* that event's output

It never walks `Event.parents`. Those are execution history: they record what
ran after what, not what was derived from what. Walking them transitively
gives you everything downstream, which is precisely baseline B2. A method that
silently reproduces its own baseline has no result left to report, and the
failure is invisible -- the numbers still come out, they are just the
baseline's numbers wearing our name.

`derived_from` is the other half and is the easy one to forget. An agent
output exists twice in a trace: as the event that produced it (e0006) and as
the source a later agent consumed (S6). Those are one piece of work (D-012).
Without this edge contamination stops dead at every agent boundary, and we
would report a contaminated set that is too small -- an unsafe preservation,
which is the dangerous direction of error.

WHAT THIS MODULE MUST NOT READ
------------------------------
`Source.malicious` is ground truth, for src/eval/ scoring only. Nothing here
reads it. Malicious ids arrive as an argument, standing in for the detector
this project assumes exists and does not build (docs/01-scope.md). Reading the
label would hand the method the answer it is meant to derive, and every
accuracy number after that would be worthless.
"""

from dataclasses import dataclass, field
from typing import Iterable

from src.common.models import sort_source_ids
from src.tracing.logger import Trace


@dataclass(frozen=True)
class Policy:
    """The knobs that decide how cautious the walk is.

    Both default to the safe setting. docs/04 asks for an ablation with the
    conservative fallback turned off ("how much does unsafe preservation
    rise?"), which is the only reason these are switches rather than
    hardcoded behaviour. Turning one off makes the method less safe on
    purpose, in order to measure what the safety was buying.
    """

    # An exposure nobody checked is treated as influence. docs/02:
    # "Anything we cannot establish confidently is treated as contaminated."
    assume_unchecked_exposures: bool = True

    # An influence edge recorded without confidence still contaminates. It is
    # not evidence of influence -- influence_graph() excludes it from the
    # figure for exactly that reason -- but it is equally not evidence of
    # *non*-influence, and only the second one would justify keeping the work.
    unconfident_edges_contaminate: bool = True


@dataclass
class ContaminatedRegion:
    """What the detector's verdict actually reaches.

    `events` is the answer. Work is counted in events, never sources (D-012),
    so every metric in docs/04 starts from this set.
    """

    seeds: frozenset[str]
    sources: frozenset[str]
    events: frozenset[str]
    # id -> why it ended up in here. Kept because "these events are
    # contaminated" is not usable in a paper without "and this is why this
    # one" -- and because a wrong answer is unreadable without it.
    reasons: dict[str, str] = field(default_factory=dict)

    def clean_events(self, trace: Trace) -> list[str]:
        """Everything the run did that survives. The work we preserve."""
        return [e.id for e in trace.events if e.id not in self.events]

    def explain(self) -> list[str]:
        lines = [
            f"seeds     {sort_source_ids(list(self.seeds))}",
            f"sources   {sort_source_ids(list(self.sources))}",
            f"events    {sorted(self.events)}",
        ]
        for key in sort_source_ids(list(self.sources)) + sorted(self.events):
            if key in self.reasons:
                lines.append(f"  {key:<7} {self.reasons[key]}")
        return lines


def contaminate(
    trace: Trace,
    malicious: Iterable[str],
    policy: Policy | None = None,
    checked: set[tuple[str, str]] | None = None,
) -> ContaminatedRegion:
    """Propagate contamination from the sources the detector flagged.

    `checked` is the set of (source id, event id) pairs that provenance
    analysis actually examined. It matters only when
    `policy.assume_unchecked_exposures` is on: a pair nobody looked at is
    assumed to be influence, a pair that was looked at is trusted.

    Passing None falls back to "a pair counts as checked if it has an
    influence edge". That fallback is **wrong, in the safe direction, and
    should not survive week 2**: a counterfactual check that finds no
    influence records nothing at all, so a pair that was cleared looks
    identical to a pair nobody examined, and both get assumed contaminated.
    It costs work preserved and can never cause an unsafe preservation. See
    D-024.
    """
    policy = policy or Policy()

    known = {s.id for s in trace.sources}
    unknown = [sid for sid in malicious if sid not in known]
    if unknown:
        # A detector naming a source this trace never saw means the verdict
        # and the trace belong to different runs. Propagating nothing would
        # look exactly like a clean run, which is the worst way to fail.
        raise ValueError(f"trace has no source(s) {sorted(unknown)}")

    influenced_events: dict[str, set[str]] = {}
    for edge in trace.influence:
        if edge.confident or policy.unconfident_edges_contaminate:
            influenced_events.setdefault(edge.source_id, set()).add(edge.target_event)

    if checked is None:
        checked = {(e.source_id, e.target_event) for e in trace.influence}

    exposed_events: dict[str, set[str]] = {}
    for event in trace.events:
        for sid in event.exposures:
            exposed_events.setdefault(sid, set()).add(event.id)

    # event -> the source(s) that wrap its output for a later agent (D-012)
    wraps: dict[str, set[str]] = {}
    for source in trace.sources:
        if source.derived_from:
            wraps.setdefault(source.derived_from, set()).add(source.id)

    seeds = frozenset(malicious)
    bad_sources: set[str] = set()
    bad_events: set[str] = set()
    reasons: dict[str, str] = {sid: "flagged by the detector" for sid in seeds}

    # Alternating walk: contaminated source -> the events it influenced ->
    # the sources those events became -> the events *those* influenced.
    pending = list(seeds)
    while pending:
        sid = pending.pop()
        if sid in bad_sources:
            continue
        bad_sources.add(sid)

        targets: dict[str, str] = {}
        for eid in influenced_events.get(sid, ()):
            targets[eid] = f"influenced by {sid}"
        if policy.assume_unchecked_exposures:
            for eid in exposed_events.get(sid, ()):
                if eid not in targets and (sid, eid) not in checked:
                    targets[eid] = f"exposed to {sid}, influence never checked"

        for eid, why in targets.items():
            if eid in bad_events:
                continue
            bad_events.add(eid)
            reasons[eid] = why
            for derived in wraps.get(eid, ()):
                if derived not in bad_sources:
                    reasons.setdefault(derived, f"output of contaminated {eid}")
                    pending.append(derived)

    return ContaminatedRegion(
        seeds=seeds,
        sources=frozenset(bad_sources),
        events=frozenset(bad_events),
        reasons=reasons,
    )


def downstream_closure(trace: Trace, malicious: Iterable[str]) -> set[str]:
    """Everything downstream of where the bad sources entered, by parent edges.

    This is what current practice discards, and it is the set our answer has
    to be smaller than for the week-2 exit test. It is built here **only** as
    the thing we compare against -- see the warning in src/tracing/graphs.py
    about never deciding contamination this way. The real baselines B0/B1/B2
    live in src/eval/ and are scored there.
    """
    from src.tracing.graphs import EventGraph

    graph = EventGraph.from_trace(trace)
    entry = {
        s.origin_event
        for s in trace.sources
        if s.id in set(malicious) and s.origin_event
    }
    return set(entry) | graph.descendants(*entry)


if __name__ == "__main__":
    import sys

    from src.tracing.logger import read_trace

    args = sys.argv[1:]
    path = args[0] if args else "data/runs/fake.jsonl"
    # Seeds come from the command line, never from Source.malicious -- that
    # field is eval's ground truth and reading it here would leak the answer.
    seeds = args[1:] or ["S5"]

    trace = read_trace(path)
    trace.validate()
    region = contaminate(trace, seeds)
    naive = downstream_closure(trace, seeds)
    total = len(trace.events)

    print(f"trace     {path}  ({total} events)")
    print(f"detector  flagged {seeds}")
    print()
    for line in region.explain():
        print(line)
    print()
    print(f"ours              {len(region.events):>2} of {total} events contaminated")
    print(f"everything down-  {len(naive):>2} of {total} events discarded")
    print(f"  stream of entry    {sorted(naive)}")
    print()

    kept_ours = total - len(region.events)
    kept_naive = total - len(naive)
    print(f"work preserved    ours {kept_ours}/{total} ({kept_ours / total:.0%})"
          f"   downstream closure {kept_naive}/{total} ({kept_naive / total:.0%})")
    print()

    smaller = region.events < naive
    print(f"EXIT TEST (week 2): contaminated set is a strict subset of "
          f"everything downstream -> {'PASS' if smaller else 'FAIL'}")
    if not smaller:
        extra = sorted(region.events - naive)
        if extra:
            print(f"  contaminated but not downstream: {extra}")
            print("  that means contamination crossed an edge the parent graph")
            print("  does not have, which is possible and worth reading closely.")
    raise SystemExit(0 if smaller else 1)
