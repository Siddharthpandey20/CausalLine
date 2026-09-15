"""
Asking where a flagged source came from.

WHAT THE SYSTEM ASSUMED WITHOUT SAYING SO
-----------------------------------------
A detector hands us source ids and the rest of the system treats each one as an
**origin**: contamination starts there and flows forward. Nothing ever asked
whether the flagged source was itself produced by something earlier that the
detector did not name.

For a source that arrived from outside -- a web page, a database row -- that is
correct, and there is nothing upstream to find. For a source that *is* an
earlier event's output (`derived_from`, D-012) it is an assumption, and a
reviewer will ask about it: if the Researcher's finding is flagged, the question
"what made the Researcher write that?" has an answer in the trace, and one of
its inputs may be the real entry point.

WHAT THIS DOES, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------
It walks backwards and **surfaces candidates**. It does not flag them, does not
seed the contamination walk with them, and does not change any recovery
decision. That boundary is the point:

  * a backward walk is a *hypothesis generator*. The relation it follows is "was
    an input to", which is exposure, not influence -- so treating its output as
    contamination would re-import the exposure/influence conflation this whole
    project exists to remove, and would make the contaminated region grow back
    towards B2's.
  * the detector is the component that decides what is malicious
    (docs/01-scope.md). Promoting a candidate to a seed here would be this
    system quietly doing detection, which it does not claim to do.

So the output is a list for a human or for a second detector pass, ranked by how
close it is to the flagged source, and every candidate says how it was reached.

`derived_from`, `ancestors()` and `causal_past()` already existed for other
purposes; this is the first thing that uses them for attribution.
"""

from dataclasses import dataclass, field
from typing import Any, Iterable

from src.common.models import sort_source_ids


@dataclass
class Candidate:
    """One source worth investigating as a possible earlier origin."""

    source_id: str
    hops: int
    via: tuple[str, ...] = ()
    relation: str = ""
    influenced: bool = False

    def describe(self) -> str:
        route = " <- ".join(self.via) if self.via else "?"
        strength = (
            "was influenced by it" if self.influenced else "was only exposed to it"
        )
        return (
            f"{self.source_id} ({self.hops} hop(s) upstream, via {route}; "
            f"the producing event {strength})"
        )


@dataclass
class UpstreamReport:
    flagged: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    terminal: list[str] = field(default_factory=list)
    max_hops: int = 0

    def ids(self) -> list[str]:
        return sort_source_ids([c.source_id for c in self.candidates])

    def summary(self) -> str:
        if not self.flagged:
            return "nothing flagged, so nothing to trace back"
        if not self.candidates:
            return (
                f"flagged {sort_source_ids(self.flagged)}: every one arrived from "
                "outside the run, so there is no earlier origin in this trace"
            )
        return (
            f"flagged {sort_source_ids(self.flagged)}: "
            f"{len(self.candidates)} upstream candidate(s) "
            f"{self.ids()} within {self.max_hops} hop(s). Surfaced for "
            "investigation only -- not flagged, not contaminated"
        )


def _producer(trace: Any, source_id: str) -> str | None:
    """The event a source is the output of, if any.

    `derived_from` is the producer; `origin_event` is merely the event that
    brought the source into the log. For a web page those differ and only the
    first is a derivation -- a page is not *derived from* the tool response that
    fetched it, it arrived from outside -- so a page correctly terminates the
    walk instead of implicating whatever else that tool call saw.
    """
    source = trace.source(source_id)
    return source.derived_from


def investigation_candidates(
    trace: Any,
    flagged: Iterable[str],
    max_hops: int = 4,
    prefer_influence: bool = True,
) -> UpstreamReport:
    """Sources that may be the real origin behind what the detector named.

    `prefer_influence` uses the trace's influence edges into the producing event
    when there are any, and falls back to that event's exposures when there are
    none. The fallback is the conservative direction for a *candidate* list --
    it surfaces more to look at, never fewer -- and it matters because a trace
    whose estimator never ran has no edges at all.
    """
    report = UpstreamReport(flagged=list(flagged))
    known = {s.id for s in trace.sources}
    seen: set[str] = set(report.flagged)

    influencers: dict[str, set[str]] = {}
    for edge in trace.influence:
        influencers.setdefault(edge.target_event, set()).add(edge.source_id)

    frontier = [(sid, 0, ()) for sid in report.flagged if sid in known]
    while frontier:
        source_id, hops, route = frontier.pop(0)
        producer = _producer(trace, source_id)
        if not producer or not trace.has_event(producer):
            report.terminal.append(source_id)
            continue
        if hops >= max_hops:
            continue

        event = trace.event(producer)
        established = influencers.get(producer, set())
        inputs = established if (prefer_influence and established) else set(event.exposures)
        for upstream_id in sort_source_ids([s for s in inputs if s in known]):
            if upstream_id in seen:
                continue
            seen.add(upstream_id)
            via = route + (producer,)
            report.candidates.append(
                Candidate(
                    source_id=upstream_id,
                    hops=hops + 1,
                    via=via,
                    relation="derived_from",
                    influenced=upstream_id in established,
                )
            )
            report.max_hops = max(report.max_hops, hops + 1)
            frontier.append((upstream_id, hops + 1, via))
    return report


if __name__ == "__main__":
    import sys

    from src.tracing.logger import read_trace

    args = sys.argv[1:]
    path = args[0] if args else "data/runs/fake.jsonl"
    seeds = args[1:]
    if not seeds:
        raise SystemExit(
            "pass the detector's flagged source ids; this module will not read "
            "Source.malicious"
        )
    trace = read_trace(path)
    trace.validate()
    report = investigation_candidates(trace, seeds)
    print(f"trace {path}")
    print(report.summary())
    for candidate in report.candidates:
        print(f"  {candidate.describe()}")
    if report.terminal:
        print(
            f"  terminal (arrived from outside the run): "
            f"{sort_source_ids(report.terminal)}"
        )
