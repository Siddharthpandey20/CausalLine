"""
The noise floor of the client every reported scripted number was produced on.

WHY THIS IS A REAL MEASUREMENT AND NOT A STAND-IN
-------------------------------------------------
docs/03 #12 records that only the `decision` comparator has ever had its floor
measured, on eight live re-sends of one Gemini call, and that `prose`, `code`,
`json_shape` and `tool_args` have none. That is true and it is about the
*hosted* model.

But every number in docs/07 and docs/08 was produced by `ScriptedClient`, not by
a hosted model. A noise floor belongs to the thing that produced the answers
(D-004, D-026), so for those results the floor that was missing is
`ScriptedClient`'s -- and that one costs nothing to measure, needs no quota, and
has never been taken either.

So this measures the scripted client, across all five comparators, on the real
prompts a real pipeline run produced. It is not a substitute for the hosted
measurement and does not claim to be: it is the floor for the mode the numbers
actually come from, and it is written to its own file with its own model name so
the two can never be confused.

WHAT IT ASKS, EXACTLY
---------------------
Take each pipeline model call from a finished trace. Re-send its prompt N times
with **nothing removed**. For each facet of the comparator that event's output
is scored under, count how often the facet's value differs from the original's.
That fraction is the facet's floor.

`ScriptedClient` is built to reproduce the one live property that matters here:
the text of an answer changes on every call (D-026 measured 8 of 8 distinct)
while the decision underneath it does not move unless an input it used changed.
So a facet with a non-zero floor here is a facet reading the churn -- exactly
what the pre-registered exclusion rule in `signatures.py` exists to drop.

The `carryover` facet (D-064) is measured too, and it needs a second argument:
it is defined against *removed content*. There is no removal on an unchanged
re-send, so it is scored against each exposed source in turn -- which asks the
right question anyway: does which-of-this-source's-text-appears-in-the-answer
move when nothing changes?

    python -m src.provenance.scripted_noise
    python -m src.provenance.scripted_noise --trials 30 --write-calibration
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.common.prompts import parse_sources
from src.provenance.signatures import (
    CARRYOVER_FACET,
    Calibration,
    Signature,
    for_event,
    with_carryover,
)

SCRIPTED_CALIBRATION_PATH = "data/noise/calibration-scripted.json"
SCRIPTED_MODEL = "scripted"
DEFAULT_TRIALS = 20


@dataclass
class FacetFloor:
    comparator: str
    facet: str
    trials: int = 0
    differs: int = 0
    distinct: int = 0

    @property
    def rate(self) -> float:
        return self.differs / self.trials if self.trials else 0.0


@dataclass
class ScriptedFloors:
    trials: int = 0
    events: int = 0
    floors: dict[tuple[str, str], FacetFloor] = field(default_factory=dict)

    def record(
        self, comparator: str, facet: str, differs: int, trials: int, distinct: int
    ) -> None:
        key = (comparator, facet)
        floor = self.floors.setdefault(key, FacetFloor(comparator, facet))
        floor.trials += trials
        floor.differs += differs
        floor.distinct = max(floor.distinct, distinct)

    def excluded(self) -> dict[str, list[str]]:
        """Facets to drop, per comparator, under the pre-registered rule.

        The rule is in `signatures.py` and was written before any trial: a facet
        whose floor on **unchanged** re-sends is non-zero cannot carry evidence,
        so it is excluded. Nothing is ever excluded for what it says about
        influence.
        """
        out: dict[str, list[str]] = {}
        for (comparator, facet), floor in sorted(self.floors.items()):
            out.setdefault(comparator, [])
            if floor.differs:
                out[comparator].append(facet)
        return out

    def calibration(self) -> Calibration:
        calibration = Calibration(model=SCRIPTED_MODEL)
        calibration.excluded = self.excluded()
        for (comparator, facet), floor in self.floors.items():
            calibration.floors.setdefault(comparator, {})[facet] = floor.rate
            calibration.trials[comparator] = max(
                calibration.trials.get(comparator, 0), floor.trials
            )
        return calibration

    def report(self) -> str:
        lines = [
            f"scripted noise floor: {self.trials} unchanged re-sends of each of "
            f"{self.events} pipeline calls",
            "",
            f"{'comparator':<12}{'facet':<16}{'trials':>8}{'differs':>9}"
            f"{'distinct':>10}{'floor':>8}  verdict",
            "-" * 72,
        ]
        for (comparator, facet), floor in sorted(self.floors.items()):
            verdict = "EXCLUDE (cannot carry evidence)" if floor.differs else "keep"
            lines.append(
                f"{comparator:<12}{facet:<16}{floor.trials:>8}{floor.differs:>9}"
                f"{floor.distinct:>10}{floor.rate:>7.0%}  {verdict}"
            )
        dropped = {c: f for c, f in self.excluded().items() if f}
        lines += [
            "",
            f"facets excluded: {dropped or 'none -- every facet held still'}",
        ]
        return "\n".join(lines)


def measure(
    trace: Any,
    client: Any,
    trials: int = DEFAULT_TRIALS,
) -> ScriptedFloors:
    """Re-send every stored pipeline prompt `trials` times, unchanged.

    ONE client for every trial, and that is the whole measurement. A fresh
    client per trial resets the counter its wording churn is derived from, so
    every "re-send" comes back byte-identical and the floor is 0% by
    construction -- a measurement of the harness rather than of the client.
    Reusing the client is what makes consecutive identical requests produce
    different text, which is the live property D-026 measured (8 re-sends, 8
    distinct answers) and the one `ScriptedClient` exists to reproduce.
    """
    from src.recovery.replay import pipeline_model_events

    out = ScriptedFloors(trials=trials)
    for event_id in pipeline_model_events(trace):
        prompt = trace.prompt_text(event_id)
        output = trace.output_text(event_id)
        if not prompt or output is None:
            continue
        system = trace.system_text(event_id)
        kind = trace.event(event_id).kind
        comparator = for_event(kind, output)
        block = trace.source_block_text(event_id) or ""
        contents = [text for _sid, _h, text in parse_sources(block)]

        original = comparator.signature(output)
        repeats = [
            client.generate(prompt, system=system).text for _ in range(trials)
        ]
        signatures = [comparator.signature(text) for text in repeats]

        for facet, value in original.facets.items():
            values = {s.facets.get(facet, "") for s in signatures}
            differs = sum(1 for s in signatures if s.facets.get(facet) != value)
            out.record(comparator.name, facet, differs, trials, len(values | {value}))

        # The removal-aware facet, scored against each source in turn. A
        # non-zero floor here would mean the answer quotes a source
        # inconsistently between identical requests, which would make the facet
        # unusable as evidence -- so it is calibrated like every other one.
        for content in contents:
            # D-079: the facet's span set is what is unique to this source
            # within the request, so the floor has to be measured against the
            # same `elsewhere` the estimator uses -- the request with this
            # source's text taken out. Measuring it against nothing would
            # calibrate a different function from the one that runs.
            elsewhere = prompt.replace(content, " ") if content in prompt else prompt

            def _facet(text: str, _c: str = content, _e: str = elsewhere) -> str:
                return with_carryover(
                    Signature(comparator.name), text, _c, _e
                ).facets[CARRYOVER_FACET]

            base = _facet(output)
            seen = {_facet(text) for text in repeats}
            differs = sum(1 for text in repeats if _facet(text) != base)
            out.record(
                comparator.name, CARRYOVER_FACET, differs, trials, len(seen | {base})
            )
        out.events += 1
    return out


def run(trials: int = DEFAULT_TRIALS, seed: int = 20260906) -> ScriptedFloors:
    """One clean scripted run, then the floors of every comparator it exercised."""
    import tempfile

    from src.eval.scripted import ScriptedClient
    from src.tracing.logger import read_trace
    from src.tracing.pipeline import run_pipeline
    from src.tracing.tools import Tools

    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "floor.jsonl"
        run_pipeline(
            path,
            client=ScriptedClient(seed=seed),
            tools=Tools.from_fixtures(
                memory_path=path.with_suffix(".memory.json"), extended=True
            ),
            research_rounds=3,
            reviewer=True,
        )
        trace = read_trace(path)
        # The long workflow, so the Reviewer and the extra research rounds are
        # exercised too: `json_shape` and `tool_args` only appear on events the
        # short pipeline has one of each of.
        #
        # A SECOND client, not the one that produced the run: the trials are
        # re-sends, and continuing the original client's call counter would make
        # the first trial's churn differ from the original's for a reason that
        # is about our bookkeeping rather than about the client.
        return measure(trace, ScriptedClient(seed=seed), trials=trials)


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    trials = int(args[args.index("--trials") + 1]) if "--trials" in args else DEFAULT_TRIALS
    floors = run(trials=trials)
    print(floors.report())
    if "--write-calibration" in args:
        calibration = floors.calibration()
        calibration.save(SCRIPTED_CALIBRATION_PATH)
        print()
        print(f"written to {SCRIPTED_CALIBRATION_PATH}")
        print(calibration.describe())
    out = Path("data/results/scripted-noise.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "model": SCRIPTED_MODEL,
                "trials": floors.trials,
                "events": floors.events,
                "floors": [
                    {
                        "comparator": f.comparator,
                        "facet": f.facet,
                        "trials": f.trials,
                        "differs": f.differs,
                        "rate": f.rate,
                    }
                    for f in floors.floors.values()
                ],
                "excluded": floors.excluded(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"detail written to {out}")
