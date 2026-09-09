"""
Where influence edges come from.

The pipeline logs an event, then hands it to an **attributor**, whose only job
is to decide which of that event's exposures were influences and to write the
resulting edges and `check` records. Before this existed, the live pipeline
wrote no influence edges at all -- every trace it produced showed exposure and
nothing else, so every downstream number was the conservative fallback's
number rather than the method's.

Attribution is a strategy object rather than a function for one reason: the
ablations docs/04 asks for ("self-report only, no counterfactual",
"counterfactual only, no self-report", "conservative fallback off") are exactly
a choice of attributor. Making them swappable here means an ablation is a
different object passed to the same pipeline, not a second code path that has
to be kept in step with the first.

    NullAttributor        writes nothing. The state the pipeline was in
                          before: exposure recorded, influence never
                          established, so the conservative fallback decides
                          everything.
    AssumeInfluenced      writes an `assumed` edge and a `tainted` check for
                          every exposure. The conservative fallback made
                          explicit and recorded, rather than left implicit in
                          the absence of records.
    SelfReportAttributor  one extra model call per event. Positive claims are
                          settled; negative claims are deliberately left
                          unrecorded -- see below.
    HybridAttributor      self-report, then a counterfactual on the claims that
                          could cause an unsafe preservation. In
                          src/provenance/estimator.py, because it needs the
                          comparators.

WHY SelfReportAttributor LEAVES NEGATIVES UNRECORDED
----------------------------------------------------
It would be easy, and wrong, to write `check(clean, self_report)` when an agent
says it did not use a source. That record is what stops the contamination walk,
and stopping it on an unverified claim is an unsafe preservation waiting to
happen -- docs/03 issue #1 ("self-reported reasoning is sometimes confidently
wrong") is precisely about this claim.

So a self-reported negative leaves the pair `unchecked`, which the walk already
treats as contaminated. The consequence is honest and worth stating: on its
own, self-report can only ever *add* contamination, never preserve work. It
earns its cost by telling the counterfactual stage where to look, and it is
that stage that converts a negative into a `clean` record. That is also the
prediction the "self-report only" ablation is there to confirm.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from src.common.models import InfluenceEdge
from src.provenance import selfreport


@dataclass
class AttributionRequest:
    """Everything an attributor may look at.

    Note what is absent: `Source.malicious`, and the detector's verdict. An
    estimator that knew which source was the attack would produce edges shaped
    by the answer rather than by the evidence, and every accuracy number after
    that would be worthless. Attribution runs during the original run, before
    any detector has spoken.
    """

    event_id: str
    agent_id: str
    kind: str
    output: str
    exposures: list[str]
    # source id -> a short human label ("web, https://...") for the catalogue
    labels: dict[str, str] = field(default_factory=dict)
    # The exact prompt that produced the output, and the rendered source list
    # inside it. The counterfactual stage cannot work without both: it
    # re-issues the prompt with one source removed, and it needs the block to
    # know where the sources end (see src/common/prompts.py).
    prompt: str | None = None
    source_block: str | None = None
    system: str | None = None
    # RECORDED redundancy, for atomic-unit grouping (D-051).
    #
    # source id -> the other exposed sources it was derived from. Populated
    # only from links the trace actually recorded: `Source.derived_from` names
    # the event that produced this source, and the sources with an influence
    # edge into that event are the inputs it was built from. A summary and its
    # own inputs sitting in one context are individually unnecessary -- either
    # alone still carries the fact -- so single-source removal clears both and
    # the contamination chain silently breaks.
    #
    # Empty means "no recorded link", which is the honest default: coincidental
    # redundancy between unrelated sources is NOT represented here and is not
    # handled. See docs/06 section 2.2.
    derived_links: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def catalogue(self) -> list[tuple[str, str]]:
        return [(sid, self.labels.get(sid, "source")) for sid in self.exposures]


@runtime_checkable
class Sink(Protocol):
    """What an attributor writes to. `TraceLogger` satisfies it."""

    def log_influence(self, edge: InfluenceEdge) -> Any: ...

    def log_check(
        self,
        source_id_: str,
        target_event: str,
        verdict: str,
        method: str,
        confidence: float = ...,
        signature_before: str | None = ...,
        signature_after: str | None = ...,
        comparator: str | None = ...,
        repeats: int = ...,
        notes: str = ...,
    ) -> Any: ...

    def log_usage(self, purpose: str, model: str, prompt_tokens: int, output_tokens: int, total_tokens: int, **kwargs: Any) -> Any: ...


class Attributor(Protocol):
    name: str

    def attribute(self, request: AttributionRequest, sink: Sink) -> None: ...


# --- the two trivial ones, which are both real experimental conditions -------


class NullAttributor:
    """Establish nothing. Every exposure stays unchecked.

    This is the condition every number in the repository was measured under
    before now, which is why it is kept as a named object rather than deleted:
    "no analysis" is the control the analysed runs are compared against, and
    D-025 records what it looks like -- the influencing and exposed-only
    variants of scenario A scored identically, because telling them apart is
    what the expensive half of the method is for.
    """

    name = "none"

    def attribute(self, request: AttributionRequest, sink: Sink) -> None:
        return None


# DEAD CODE -- no caller, kept for reference.
# The conservative fallback written down as an attributor. Never selected: the
# fallback is what happens when nothing is recorded, so running this produces
# the same verdicts at the cost of one record per exposure. Kept as the named
# control condition the module docstring describes.
class AssumeInfluenced:
    """Every exposure is an influence, recorded as `assumed`.

    Same verdict the conservative fallback reaches, but written down. Useful
    because it separates two things the old trace conflated: a run where the
    fallback fired because nothing was checked, and a run where the fallback
    fired because checking failed.
    """

    name = "assumed"

    def attribute(self, request: AttributionRequest, sink: Sink) -> None:
        for sid in request.exposures:
            sink.log_influence(
                InfluenceEdge(sid, request.event_id, method="assumed", confident=False)
            )
            sink.log_check(
                sid,
                request.event_id,
                "tainted",
                "assumed",
                confidence=0.0,
                notes="conservative fallback: exposure taken as influence",
            )


# --- self-report -------------------------------------------------------------


# DEAD CODE -- no caller, kept for reference.
# A second, independent implementation of the self-report path. The one that
# actually runs is `HybridAttributor` in src/provenance/estimator.py with
# `mode="self_report"`, which shares `selfreport.ask()` with this class but
# adds the counterfactual escalation and the budget. Two implementations of
# one policy is a drift hazard; this is the one nothing calls.
@dataclass
class SelfReportAttributor:
    """One structured self-report call per event.

    `confident_at` is the agent-reported confidence above which a positive
    claim is marked `confident=True` on the edge. It affects only the figure
    that claims to show established influence (`influence_graph()` filters on
    it); an unconfident edge still contaminates, because not-evidence-of-
    influence is not evidence of non-influence.
    """

    client: Any
    name: str = "self_report"
    confident_at: float = 0.7
    model: str = "unknown"

    def attribute(self, request: AttributionRequest, sink: Sink) -> None:
        if not request.exposures:
            return
        report = selfreport.ask(
            self.client,
            request.event_id,
            request.kind,
            request.output,
            request.catalogue(),
        )
        sink.log_usage(
            "self_report",
            model=self.model,
            prompt_tokens=report.prompt_tokens,
            output_tokens=report.output_tokens,
            total_tokens=report.total_tokens,
            event_id_=request.event_id,
            agent_id=request.agent_id,
        )
        for sid in request.exposures:
            claim = report.claim(sid)
            if not claim.used:
                # Left unchecked on purpose. See the module docstring: a
                # self-reported negative is a claim, and acting on it is how an
                # unsafe preservation happens.
                continue
            sink.log_influence(
                InfluenceEdge(
                    sid,
                    request.event_id,
                    method="self_report",
                    confident=claim.confidence >= self.confident_at,
                )
            )
            sink.log_check(
                sid,
                request.event_id,
                "tainted",
                "self_report",
                confidence=claim.confidence,
                notes=(
                    "agent reported using this source"
                    + ("" if claim.reported else " (no explicit claim; defaulted)")
                ),
            )


# --- structural, for events the model did not write --------------------------


def record_carrier(
    sink: Sink,
    event_id: str,
    exposures: list[str],
    inherited: list[str],
    copied_from: str,
) -> None:
    """Attribution for an event that hands an earlier event's output onward.

    A hand-off message, or a tool call whose arguments were lifted out of an
    earlier output, produces no new content: its text is a function of one
    upstream event's output. So its influence set is that event's influence
    set, restricted to what is in context here. Read off, not estimated.

    Worth spelling out because the tempting shortcut is wrong in the dangerous
    direction. A carrier consults none of the sources in its context, so
    "structurally clean, used nothing" looks right -- and would mark a message
    whose text is a verbatim copy of contaminated output as clean. The copy
    relation is real influence; only the *reading* of fresh sources is absent.
    """
    for sid in exposures:
        if sid in set(inherited):
            sink.log_influence(
                InfluenceEdge(sid, event_id, method="structural", confident=True)
            )
            sink.log_check(
                sid,
                event_id,
                "tainted",
                "structural",
                confidence=1.0,
                notes=f"carries output of {copied_from}, which this source influenced",
            )
        else:
            sink.log_check(
                sid,
                event_id,
                "clean",
                "structural",
                confidence=1.0,
                notes=f"carries output of {copied_from}, which this source did not influence",
            )


def record_structural(
    sink: Sink, event_id: str, exposures: list[str], used: list[str], why: str = ""
) -> None:
    """Attribution for an event whose output our own code computed.

    Tool calls, tool responses, memory reads and writes have no model in the
    loop: which inputs reached the output is a property of the code path, and
    the code path is right here. So these verdicts are neither claims nor
    estimates -- they are read off, and they are the only `clean` records in a
    trace that cost nothing and can be relied on completely.

    This matters more than it sounds. Roughly half the events in a run are of
    this kind, and every one of them is exposed to whatever is in its agent's
    context. Without these records the conservative fallback taints all of
    them, which is a large slice of the headline metric spent on events that
    never consulted a source at all.
    """
    influenced = set(used)
    for sid in exposures:
        if sid in influenced:
            sink.log_influence(
                InfluenceEdge(sid, event_id, method="structural", confident=True)
            )
            sink.log_check(
                sid,
                event_id,
                "tainted",
                "structural",
                confidence=1.0,
                notes=why or "this source fed the operation's arguments",
            )
        else:
            sink.log_check(
                sid,
                event_id,
                "clean",
                "structural",
                confidence=1.0,
                notes=why or "operation computed by code; this source was not read",
            )


def derived_links_for(
    exposures: Sequence[str],
    producer_of: Callable[[str], str | None],
    influencers_of: Callable[[str], Sequence[str]],
    link_shared_ancestors: bool = True,
) -> dict[str, tuple[str, ...]]:
    """Which exposed sources are redundant with which, by recorded provenance.

    Deliberately expressed over two callables rather than over a `Trace`, so
    the same rule serves both callers: the pipeline establishes links mid-run
    off its own partial log, and `request_for()` establishes them afterwards
    off a finished trace. One rule, two call sites, no chance of them drifting.

    TWO SHAPES OF RECORDED REDUNDANCY, BOTH REAL
    --------------------------------------------
    **Direct** -- a summary sitting beside its own inputs:

        A links to B  <=>  A.derived_from names an event E
                           AND B has an influence edge into E
                           AND both are exposed here

    **Shared ancestor** -- two summaries of the same upstream source, where
    that source is *not* itself in this context:

        A links to B  <=>  the events that produced A and B were influenced
                           by at least one source in common

    The second shape is the one that costs points, and it took a measurement
    to see. At the Coder in the long workflow the context holds five Researcher
    findings and no web pages: S14, S16, S17 and S18 were each produced by an
    event that S4 -- the poisoned page -- influenced, but S4 is nowhere in the
    Coder's context, so no direct link exists between any pair of them. Each
    finding is individually unnecessary because the other four still carry the
    poisoned fact. Leave-one-out clears all five, the script is attributed to
    the memory source alone, contamination never reaches the Executor, and
    A-influencing/oracle recovers 11.1% against B1's 14.8%.

    Both shapes use only links the trace recorded. Neither covers two unrelated
    sources that happen to state the same fact -- see docs/06 section 2.2.

    `link_shared_ancestors=False` restricts this to the direct shape, which is
    what the sibling case has to be measured against.
    """
    exposed = set(exposures)
    links: dict[str, set[str]] = {sid: set() for sid in exposures}
    ancestors: dict[str, set[str]] = {}

    for sid in exposures:
        producer = producer_of(sid)
        if not producer:
            ancestors[sid] = set()
            continue
        upstream = set(influencers_of(producer))
        ancestors[sid] = upstream
        # Direct: an input of mine is sitting right here beside me.
        for other in upstream:
            if other in exposed and other != sid:
                links[sid].add(other)
                links[other].add(sid)

    if link_shared_ancestors:
        ordered = list(exposures)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                # An ancestor that is itself exposed is already handled above
                # as a direct link; sharing it does not make a and b redundant
                # with each other in the way this is about.
                shared = (ancestors[a] & ancestors[b]) - exposed
                if shared:
                    links[a].add(b)
                    links[b].add(a)

    return {sid: tuple(sorted(peers)) for sid, peers in links.items() if peers}
