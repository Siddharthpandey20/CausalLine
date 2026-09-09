"""
How likely is it that *this* node was successfully attacked?

Phase 8. The number a cost model needs and the repository did not have.

WHAT WAS WRONG WITH THE OLD NUMBER
----------------------------------
The figure previously used was a single scalar

    pa = successful_attacks / total_attempts = 3.33%

taken from a large public red-teaming competition. Two things are wrong with
it, and they pull in opposite directions from what a deployment cares about:

  * **it conflates a heterogeneous attempt pool with a realistic attack.** A
    public competition's denominator is dominated by weak, duplicate and
    malformed submissions. Dividing by all of them answers "what fraction of
    everything anyone typed worked", not "if a competent attacker gets a
    payload into this channel, does it land". The second question is the one a
    recovery system is sized against, and 3.33% understates it.
  * **it is one scalar for every channel.** Published per-channel rates differ
    by roughly 4.8x between the two channels that have been measured, so a
    single number hides the only structural fact in the data: what reaches the
    agent through retrieved content is far more dangerous than what the user
    typed.

SOURCE OF THE REPLACEMENT VALUES
--------------------------------
Zou et al., "Security Challenges in AI Agent Deployment", arXiv 2507.20526
(2025). Attack-success rates reported by injection channel: 12.7% overall,
27.1% for indirect prompt injection, 5.7% for direct prompt injection. The
overall figure is not used here -- it is a mixture whose weights are that
paper's threat model, not ours, and our traces tell us the mixture directly.

Nothing in this module derives a pa value. Where no published figure exists
the channel defaults to the conservative one and says so; see
UNCALIBRATED_CHANNELS. Inventing a number to fill a gap would put an
unfalsifiable constant in the middle of the cost model.

THE COMBINATION RULE
--------------------
For one node, with m_i exposure edges of channel type i:

    P(node compromised) = 1 - PROD_i (1 - pa_i) ^ m_i

Independence between attempts is assumed, which is the standard and is
optimistic in a knowable way: correlated payloads (the same poisoned page
retrieved twice, one attacker writing every injected message) make the true
probability *lower* than this, because a second copy of a failed attack fails
again. Reported as an upper bound rather than a point estimate for that
reason.

Exposure, not influence, is what is counted. This is deliberate and it is the
one place in the project where that is the right choice: a source that reached
the model's context is an attack *attempt* whether or not it changed anything,
and P is the probability that one of those attempts succeeded. Influence is
what we measure afterwards, on the trace we actually have.
"""

from dataclasses import dataclass, field
from typing import Any, Iterable

from src.common.models import SOURCE_KINDS
from src.tracing.graphs import exposure_graph
from src.tracing.logger import Trace

# Attack success rate per injection channel.
# Source: Zou et al. 2025 (arXiv 2507.20526), reported ASR by injection
# channel. 27.1% indirect, 5.7% direct, 12.7% overall (the overall figure is
# deliberately not used -- see the module docstring).
CHANNEL_PA: dict[str, float] = {
    "indirect_injection": 0.271,  # web content, RAG documents, tool outputs read by the agent
    "direct_injection": 0.057,    # user-authored or directly-supplied prompt content
    # "inter_agent_message" and "memory_write": no published per-channel figure
    # exists yet. They default to "indirect_injection" (0.271) as the
    # conservative choice. This is an OPEN CALIBRATION GAP, not a measurement:
    # both channels plausibly differ from retrieved web content (an inter-agent
    # message arrives with more implicit trust; a memory write is attacker-
    # chosen but was already accepted once), and neither has a number behind
    # it. Do not quote 0.271 for them as though it were measured.
}

# The conservative default for a channel with no published figure.
DEFAULT_CHANNEL = "indirect_injection"

# Channels this project can name but cannot cite a rate for. Reported beside
# every risk figure so the gap travels with the number.
UNCALIBRATED_CHANNELS: frozenset[str] = frozenset(
    {"inter_agent_message", "memory_write"}
)

# Which channel a source kind arrives through. `Source.kind` is the frozen
# vocabulary in src/common/models.py; this is a total map over it, so a new
# source kind is a loud KeyError rather than a silent default.
CHANNEL_FOR_SOURCE_KIND: dict[str, str] = {
    "user_input": "direct_injection",
    "web": "indirect_injection",
    "database": "indirect_injection",
    "tool_output": "indirect_injection",
    "agent_message": "inter_agent_message",
    "memory": "memory_write",
}

# Per-model overrides, if a published ASR for a specific model ever exists.
# Empty on purpose: this project does not run its own red-teaming campaign to
# fill it (see docs/06-limitations.md), and a privately measured number here
# would be indistinguishable from a cited one to any later reader.
MODEL_PA: dict[str, dict[str, float]] = {}


def _missing_kinds() -> set[str]:
    return set(SOURCE_KINDS) - set(CHANNEL_FOR_SOURCE_KIND)


if _missing_kinds():  # pragma: no cover - a schema change, caught at import
    raise RuntimeError(
        "CHANNEL_FOR_SOURCE_KIND does not cover source kind(s) "
        f"{sorted(_missing_kinds())}. Every source kind needs a channel or its "
        "exposures vanish from the risk model without anyone noticing."
    )


def channel_for(source_kind: str) -> str:
    """Which injection channel a source of this kind arrives through."""
    try:
        return CHANNEL_FOR_SOURCE_KIND[source_kind]
    except KeyError:  # pragma: no cover - guarded at import
        raise KeyError(
            f"no injection channel defined for source kind {source_kind!r}"
        ) from None


def pa_for(channel: str, model: str | None = None) -> float:
    """The attack-success rate for one channel.

    Falls back to the conservative default for a channel with no published
    figure. `model` selects a per-model override if one has ever been
    published; there are none, so it changes nothing today and exists so that
    adding one later does not require touching every call site.
    """
    if model and model in MODEL_PA and channel in MODEL_PA[model]:
        return MODEL_PA[model][channel]
    if channel in CHANNEL_PA:
        return CHANNEL_PA[channel]
    return CHANNEL_PA[DEFAULT_CHANNEL]


def is_calibrated(channel: str) -> bool:
    """False for a channel whose rate is the conservative default rather than
    a published measurement."""
    return channel in CHANNEL_PA


# --- counting exposure edges -------------------------------------------------


def channel_counts(trace: Trace, event_id: str) -> dict[str, int]:
    """{channel -> m}, the exposure edges of each channel type reaching a node.

    Built from `exposure_graph()` -- the same edges every other part of the
    project counts (Phases 1-2). A separate counter here would drift from the
    figure the paper prints, and the two disagreeing about "how many sources
    reached this event" would be impossible to notice.
    """
    graph = exposure_graph(trace)
    counts: dict[str, int] = {}
    for sid in graph.sources_for(event_id):
        channel = channel_for(trace.source(sid).kind)
        counts[channel] = counts.get(channel, 0) + 1
    return counts


def trace_channel_counts(trace: Trace) -> dict[str, int]:
    """{channel -> m} over every exposure edge in the trace.

    The whole-run version: one attempt per (source, event) exposure, which is
    how many times a payload had a chance to act.
    """
    counts: dict[str, int] = {}
    for sid, _eid in exposure_graph(trace).edges:
        channel = channel_for(trace.source(sid).kind)
        counts[channel] = counts.get(channel, 0) + 1
    return counts


# --- the probability ---------------------------------------------------------


def compromise_probability(
    counts: dict[str, int], model: str | None = None
) -> float:
    """P = 1 - PROD_i (1 - pa_i) ^ m_i

    Implemented per channel, never as a single scalar raised to the total edge
    count: with 27.1% and 5.7% in the same run those are different numbers, and
    the whole reason Phase 8 exists is that collapsing them hid a 4.8x spread.
    """
    survival = 1.0
    for channel, m in counts.items():
        if m < 0:
            raise ValueError(f"exposure count for {channel} is negative: {m}")
        survival *= (1.0 - pa_for(channel, model)) ** m
    return 1.0 - survival


@dataclass
class NodeRisk:
    """The compromise probability of one event, and what it was built from."""

    event_id: str
    agent_id: str
    counts: dict[str, int] = field(default_factory=dict)
    probability: float = 0.0

    @property
    def exposures(self) -> int:
        return sum(self.counts.values())

    @property
    def uses_uncalibrated_channel(self) -> bool:
        """True when some of this node's exposures used a channel whose rate is
        the conservative default rather than a published figure."""
        return any(c in UNCALIBRATED_CHANNELS for c in self.counts)

    def line(self) -> str:
        parts = ", ".join(f"{c}={m}" for c, m in sorted(self.counts.items()))
        flag = "  (uncalibrated channel)" if self.uses_uncalibrated_channel else ""
        return (
            f"{self.event_id} {self.agent_id:<10} P={self.probability:.3f}  "
            f"[{parts or 'no exposures'}]{flag}"
        )


def node_risk(trace: Trace, event_id: str, model: str | None = None) -> NodeRisk:
    counts = channel_counts(trace, event_id)
    return NodeRisk(
        event_id=event_id,
        agent_id=trace.event(event_id).agent_id,
        counts=counts,
        probability=compromise_probability(counts, model),
    )


def risk_profile(trace: Trace, model: str | None = None) -> list[NodeRisk]:
    """Every event's compromise probability, in trace order."""
    return [node_risk(trace, e.id, model) for e in trace.events]


def run_probability(trace: Trace, model: str | None = None) -> float:
    """P that *something* in this run was successfully attacked.

    One attempt per exposure edge across the whole trace, combined by the same
    rule.

    READ THIS BEFORE USING IT AS AN ATTACK RATE
    -------------------------------------------
    It saturates. An 18-event run of our own pipeline carries ~64 exposure
    edges, and 1 - (1-0.271)^36 over the indirect ones alone is 1.000 to three
    places. That is not a bug in the arithmetic; it is the independence
    assumption failing at run scale. Thirty-six *distinct competent attacks*
    would indeed compromise a run almost surely -- but the thirty-six edges in
    our traces are a handful of documents re-sent to several questions, and a
    document that failed once fails again.

    So this is an upper bound on a single run, useful for ranking nodes and for
    saying "the indirect channel dominates", and it is **not** the deployment
    attack rate. `src/eval/economics.py` takes attack_rate as a parameter and
    sweeps it, which is the honest treatment of a quantity we cannot measure
    from our own traces.
    """
    return compromise_probability(trace_channel_counts(trace), model)


def residual_risk(
    trace: Trace,
    preserved_events: Iterable[str],
    model: str | None = None,
) -> float:
    """P that at least one *preserved* event was compromised.

    The lambda term in the cost model: what we are still exposed to after
    deciding to keep this set rather than recompute it. Combined across
    preserved nodes by the same independence assumption, so it is an upper
    bound in the same direction as everything else here.
    """
    survival = 1.0
    for eid in preserved_events:
        if not trace.has_event(eid):
            continue
        survival *= 1.0 - node_risk(trace, eid, model).probability
    return 1.0 - survival


def describe_calibration() -> str:
    """What is measured and what is defaulted, for printing beside a result."""
    lines = ["attack-success rates (Zou et al. 2025, arXiv 2507.20526):"]
    for channel in sorted(set(CHANNEL_FOR_SOURCE_KIND.values())):
        rate = pa_for(channel)
        mark = "measured" if is_calibrated(channel) else "DEFAULTED (no published figure)"
        lines.append(f"  {channel:<20} pa={rate:.3f}   {mark}")
    lines.append(
        "  defaulted channels take the indirect-injection rate as the "
        "conservative choice; this is an open calibration gap, not a "
        "measurement."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    from src.tracing.logger import read_trace

    path = sys.argv[1] if len(sys.argv) > 1 else "data/runs/fake.jsonl"
    trace = read_trace(path, content=False)
    trace.validate()

    print(describe_calibration())
    print()
    print(f"trace {path}  ({len(trace.events)} events, {len(trace.sources)} sources)")
    print()
    profile = risk_profile(trace)
    for risk in profile:
        if risk.exposures:
            print("  " + risk.line())
    print()
    counts = trace_channel_counts(trace)
    print(f"whole run: exposure edges by channel {counts}")
    print(f"           P(some attempt succeeded) = {run_probability(trace):.3f}")
    print("           (an upper bound that saturates -- independent attempts is")
    print("            wrong at run scale, where the same document is re-sent.")
    print("            Not a deployment attack rate; economics.py sweeps that.)")
    print()
    worst: Any = max(profile, key=lambda r: r.probability, default=None)
    if worst is not None and worst.exposures:
        print(f"highest-risk node: {worst.line()}")
