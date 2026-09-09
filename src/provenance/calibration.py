"""
How much is a self-reported *positive* worth, per channel? Measure it.

Phase 11.1. The cheapest of the three cost reductions, and the one that has to
come first: it removes candidates from the expensive stages rather than making
those stages faster.

THE ASYMMETRY, ONE MORE TIME
----------------------------
`src/provenance/selfreport.py` already accepts positive claims at face value
because a wrong positive costs recomputation and never costs safety. That is
true and it is not the whole story: a self-reporter that over-claims sends
every source it names into the invalidation set, and paying replay tokens for
sources that changed nothing is exactly the waste Phase 11 is about.

So the question here is not "is a positive claim safe" (it is) but "is a
positive claim *accurate enough to skip verifying*". That is a precision
question, and precision is measurable, because `ScriptedClient` records what
the agent really used (D-030) and the estimator is never shown it.

WHY PER CHANNEL
---------------
An agent reports differently about different kinds of input. A retrieved web
page it skimmed and a memory entry it acted on are not equally easy to be
honest about, and pooling them produces one number that describes neither.
Channels are the same partition `src/risk/attack_model.py` uses, so a claim's
reliability and its attack probability are indexed the same way.

WHAT IS AND IS NOT MEASURED HERE
--------------------------------
Measured: precision and recall of positive self-reports, per channel, on
scripted runs, against leave-one-out ground truth.

Not measured, and stated rather than glossed: this is the *scripted* agent's
honesty, whose error rate is a knob we set
(`self_report_false_positive_rate`). It answers "does the calibration
machinery recover a reporting bias that is really there", not "how honest is
gemini-3.6-flash". A live calibration needs live ground truth, which nobody
has -- that is docs/03 issue #1 and it is still open.

Numbers are therefore never hardcoded in this module. `measure()` runs the
pipeline and counts; changing the scripted agent's behaviour changes the
output, and there is a test that asserts exactly that.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from src.risk.attack_model import channel_for

CALIBRATION_PATH = "data/noise/selfreport_calibration.json"

# Precision above which a positive self-report is accepted without a
# counterfactual behind it. 0.85 is the starting point Phase 11 specifies; it
# is a policy knob, not a measurement, and it is stored on the calibration so a
# result can be read against the threshold that produced it.
DEFAULT_PRECISION_THRESHOLD = 0.85


@dataclass
class ChannelStats:
    """Positive-claim accuracy for one channel."""

    channel: str
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0

    @property
    def claimed(self) -> int:
        return self.true_positive + self.false_positive

    @property
    def actual(self) -> int:
        return self.true_positive + self.false_negative

    @property
    def precision(self) -> float:
        """Of the sources the agent claimed to use, how many it really used.

        Undefined with no positive claims. Returns 0.0 there, not 1.0: an
        untested channel must not clear the acceptance threshold by default,
        which is what a vacuous 1.0 would do.
        """
        return self.true_positive / self.claimed if self.claimed else 0.0

    @property
    def recall(self) -> float:
        return self.true_positive / self.actual if self.actual else 0.0

    @property
    def support(self) -> int:
        return (
            self.true_positive + self.false_positive
            + self.false_negative + self.true_negative
        )

    def line(self) -> str:
        return (
            f"{self.channel:<20} precision={self.precision:.3f} "
            f"recall={self.recall:.3f}  "
            f"tp={self.true_positive} fp={self.false_positive} "
            f"fn={self.false_negative} tn={self.true_negative} "
            f"(n={self.support})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "true_negative": self.true_negative,
            "precision": self.precision,
            "recall": self.recall,
        }


@dataclass
class SelfReportCalibration:
    """Per-channel positive-claim accuracy, plus the acceptance rule.

    `min_support` exists because precision on three observations is not a
    measurement. A channel below it is never accepted however good its ratio
    looks, and the refusal is reported rather than silent.
    """

    by_channel: dict[str, ChannelStats] = field(default_factory=dict)
    precision_threshold: float = DEFAULT_PRECISION_THRESHOLD
    min_support: int = 10
    model: str = ""
    runs: int = 0

    def stats(self, channel: str) -> ChannelStats | None:
        return self.by_channel.get(channel)

    def accepts_positive(self, channel: str) -> bool:
        """Whether a positive claim on this channel can skip verification."""
        stats = self.by_channel.get(channel)
        if stats is None or stats.claimed < self.min_support:
            return False
        return stats.precision >= self.precision_threshold

    def accepted_channels(self) -> list[str]:
        return sorted(c for c in self.by_channel if self.accepts_positive(c))

    def explain(self, channel: str) -> str:
        stats = self.by_channel.get(channel)
        if stats is None:
            return f"{channel}: never observed, so no positive claim is accepted"
        if stats.claimed < self.min_support:
            return (
                f"{channel}: only {stats.claimed} positive claims observed "
                f"(need {self.min_support}); not accepted"
            )
        verdict = "accepted" if self.accepts_positive(channel) else "verify"
        return (
            f"{channel}: precision {stats.precision:.3f} vs threshold "
            f"{self.precision_threshold:.2f} -> {verdict}"
        )

    def describe(self) -> str:
        lines = [
            f"self-report calibration ({self.runs} runs, model={self.model or '?'}, "
            f"threshold={self.precision_threshold:.2f}, "
            f"min_support={self.min_support})"
        ]
        for channel in sorted(self.by_channel):
            lines.append("  " + self.by_channel[channel].line())
        lines.append(f"  accepted without verification: {self.accepted_channels() or ['none']}")
        return "\n".join(lines)

    # --- persistence -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "runs": self.runs,
            "precision_threshold": self.precision_threshold,
            "min_support": self.min_support,
            "by_channel": {k: v.to_dict() for k, v in sorted(self.by_channel.items())},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SelfReportCalibration":
        by_channel = {}
        for name, raw in (data.get("by_channel") or {}).items():
            by_channel[name] = ChannelStats(
                channel=raw.get("channel", name),
                true_positive=raw.get("true_positive", 0),
                false_positive=raw.get("false_positive", 0),
                false_negative=raw.get("false_negative", 0),
                true_negative=raw.get("true_negative", 0),
            )
        return cls(
            by_channel=by_channel,
            precision_threshold=data.get(
                "precision_threshold", DEFAULT_PRECISION_THRESHOLD
            ),
            min_support=data.get("min_support", 10),
            model=data.get("model", ""),
            runs=data.get("runs", 0),
        )

    def save(self, path: str | Path = CALIBRATION_PATH) -> Path:
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return file

    @classmethod
    def load(cls, path: str | Path = CALIBRATION_PATH) -> "SelfReportCalibration":
        """Read the calibration, or return an empty one.

        An empty calibration accepts nothing, so every candidate goes on to the
        expensive stages. That is the conservative default: an uncalibrated
        deployment pays full price rather than skipping checks on the strength
        of a measurement nobody made.
        """
        file = Path(path)
        if not file.exists():
            return cls()
        return cls.from_dict(json.loads(file.read_text(encoding="utf-8")))


# --- scoring one trace --------------------------------------------------------


def score_trace(
    trace: Any,
    truth: set[tuple[str, str]],
    into: SelfReportCalibration | None = None,
) -> SelfReportCalibration:
    """Fold one scripted run's self-report claims into a calibration.

    Only events the model actually wrote are scored. Tool calls, memory reads
    and the Executor's comparison are attributed structurally -- their verdicts
    are facts read off the code path, not claims -- and counting them would put
    an easy majority of correct answers into a precision figure that is
    supposed to describe the hard part (the same reason
    `influence_eval.score_estimator` sets them aside).

    A pair with no self-report record at all is a *negative*: the attributor
    deliberately leaves unverified negatives unrecorded, so absence is the only
    way a negative claim is expressed.
    """
    calibration = into or SelfReportCalibration()
    model_events = {
        u.event_id for u in trace.usage if u.purpose == "pipeline" and u.event_id
    }

    for event in trace.events:
        if event.id not in model_events:
            continue
        for sid in event.exposures:
            record = trace.check_record(event.id, sid)
            # Structural verdicts are not claims. Skip them entirely.
            if record is not None and record.method != "self_report":
                continue
            channel = channel_for(trace.source(sid).kind)
            stats = calibration.by_channel.setdefault(channel, ChannelStats(channel))
            claimed_used = record is not None and record.verdict == "tainted"
            truly_used = (sid, event.id) in truth
            if claimed_used and truly_used:
                stats.true_positive += 1
            elif claimed_used:
                stats.false_positive += 1
            elif truly_used:
                stats.false_negative += 1
            else:
                stats.true_negative += 1
    return calibration


def measure(
    scenarios: Iterable[str] = ("A", "B", "C"),
    variants: Iterable[bool] = (True, False),
    workdir: str | Path = "data/runs/calibration",
    seeds: Iterable[int] = (20260906, 20260907, 20260908),
    precision_threshold: float = DEFAULT_PRECISION_THRESHOLD,
    min_support: int = 10,
    client_kwargs: dict[str, Any] | None = None,
) -> SelfReportCalibration:
    """Run the scripted scenarios and measure self-report precision per channel.

    `client_kwargs` is passed to `ScriptedClient`, which is how a test changes
    the agent's honesty and checks that the measured numbers move. Nothing here
    is hardcoded -- if it were, that test could not exist.
    """
    from src.eval.attacks import build, label_malicious
    from src.eval.scripted import ScriptedClient, ground_truth_influence
    from src.provenance.estimator import HybridAttributor
    from src.provenance.signatures import Calibration
    from src.tracing.logger import read_trace
    from src.tracing.pipeline import run_pipeline
    from src.tracing.tools import Tools

    out_dir = Path(workdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    calibration = SelfReportCalibration(
        precision_threshold=precision_threshold,
        min_support=min_support,
        model="scripted",
    )

    for seed in seeds:
        for scenario in scenarios:
            for influencing in variants:
                variant = "influencing" if influencing else "exposed_only"
                attack = build(scenario, influencing)
                path = out_dir / f"cal-{scenario}-{variant}-{seed}.jsonl"
                tools = attack.apply(
                    Tools.from_fixtures(
                        memory_path=path.with_suffix(".memory.json")
                    )
                )
                client = ScriptedClient(seed=seed, **(client_kwargs or {}))
                run_pipeline(
                    path,
                    client=client,
                    tools=tools,
                    attributor=HybridAttributor(
                        client=client,
                        mode="self_report",
                        calibration=Calibration.load(),
                        model="scripted",
                        seed=seed,
                    ),
                    handoff_hook=attack.handoff_hook,
                )
                if not label_malicious(path, attack.marker):
                    raise RuntimeError(
                        f"{attack.name}: marker never reached the trace"
                    )
                trace = read_trace(path)
                truth = ground_truth_influence(trace, client)
                score_trace(trace, truth, into=calibration)
                calibration.runs += 1
    return calibration


# --- the filter the expensive stages sit behind -------------------------------


@dataclass
class FilterResult:
    """Candidates split by whether calibration can settle them."""

    accepted: list[tuple[str, str]] = field(default_factory=list)
    needs_verification: list[tuple[str, str]] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def reduction(self) -> float:
        total = len(self.accepted) + len(self.needs_verification)
        return len(self.accepted) / total if total else 0.0

    def line(self) -> str:
        return (
            f"calibration accepted {len(self.accepted)} positive claims "
            f"without verification, {len(self.needs_verification)} still need "
            f"a counterfactual ({self.reduction:.0%} removed)"
        )


def filter_candidates(
    trace: Any,
    candidates: Iterable[tuple[str, str]],
    calibration: SelfReportCalibration,
) -> FilterResult:
    """Split (source, event) candidates into settled and still-to-verify.

    A candidate is accepted only when **all** of these hold:

      * the agent claimed a positive on it (a self-reported `tainted` record).
        A negative claim is never settled this way -- accepting one is how an
        unsafe preservation happens, which is the rule the whole method rests
        on and which calibration does not get to override.
      * its channel's measured precision clears the threshold, with support.

    Everything else goes on to group testing (Phase 11.2). Accepting a positive
    claim marks the pair contaminated without evidence, which costs replay
    tokens and never costs safety -- so the threshold trades money for money,
    not safety for money.
    """
    result = FilterResult()
    for sid, eid in candidates:
        record = trace.check_record(eid, sid)
        channel = channel_for(trace.source(sid).kind)
        if record is None or record.method != "self_report" or record.verdict != "tainted":
            result.needs_verification.append((sid, eid))
            result.reasons[f"{sid}->{eid}"] = "no positive self-report to accept"
            continue
        if not calibration.accepts_positive(channel):
            result.needs_verification.append((sid, eid))
            result.reasons[f"{sid}->{eid}"] = calibration.explain(channel)
            continue
        result.accepted.append((sid, eid))
        result.reasons[f"{sid}->{eid}"] = (
            f"positive self-report on {channel}, precision "
            f"{calibration.by_channel[channel].precision:.3f} >= "
            f"{calibration.precision_threshold:.2f}"
        )
    return result


if __name__ == "__main__":
    import sys

    write = "--write" in sys.argv
    calibration = measure()
    print(calibration.describe())
    print()
    for channel in sorted(calibration.by_channel):
        print("  " + calibration.explain(channel))
    print()
    print(
        "These are the SCRIPTED agent's numbers. Its dishonesty is a rate we "
        "set\n(self_report_false_positive_rate), so this measures whether the "
        "calibration\nrecovers a bias that is really there -- not how honest a "
        "real model is."
    )
    if write:
        path = calibration.save()
        print()
        print(f"written to {path}")
