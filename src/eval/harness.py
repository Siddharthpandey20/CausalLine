"""
Run one attack end to end and score it.

    inject a poisoned corpus
      -> run the pipeline (which never learns it is under attack)
      -> label the planted source as ground truth, by marker
      -> propagate contamination from the detector's verdict
      -> score every method against the truth

This is the loop docs/04 repeats ~30 times per scenario. It is written to
take its LLM client as an argument so the whole loop can be exercised
offline, with no API key and no quota: the graph and set work is identical
whether the text came from the model or from a stub.

    python -m src.eval.harness            offline, stub client, free
    python -m src.eval.harness --live     6 requests against the daily 20

A stub run is marked in the trace header (`client`) and its work-preserved
numbers are real -- they are graph operations over a real trace shape -- but
its *influence edges* are not, because nothing has estimated them yet. Read
metrics.py's circularity warning before quoting anything from here.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.eval.attacks import Attack, build, label_malicious
from src.eval.metrics import compare, table
from src.tracing.logger import read_trace
from src.tracing.pipeline import run_pipeline
from src.tracing.tools import Tools


@dataclass
class AttackRun:
    attack: Attack
    trace_path: Path
    planted_sources: list[str]
    task_success: bool
    scores: list


def run_attack(
    attack: Attack,
    path: str | Path,
    client: Any = None,
    attributor: Any = None,
) -> AttackRun:
    """One poisoned run, labelled and scored.

    Raises if the planted source never made it into the trace. A run where
    the attack did not land is not a failed attack, it is not a run -- and
    counting it would put a scenario in the results table that never
    happened.

    `attributor` is what establishes influence during the run. With None, the
    trace records exposure only and every method below collapses to the
    conservative fallback -- which is a legitimate control condition (D-025)
    but is not a measurement of the method.

    The `assume_all_checked` flag that used to be here is gone. It asserted
    that influence analysis had examined every exposure, and on a trace where
    no analysis had run it turned "nothing was examined" into "nothing was
    influenced": 100% work preserved on a genuinely poisoned run, a perfect
    unsafe preservation invented out of an assumption (D-025 point 3). The
    `check` record replaces it -- examination is now recorded rather than
    assumed, so there is nothing left to assume.
    """
    path = Path(path)
    clean = Tools.from_fixtures(memory_path=path.with_suffix(".memory.json"))
    result = run_pipeline(
        path,
        tools=attack.apply(clean),
        client=client,
        attributor=attributor,
        handoff_hook=attack.handoff_hook,
    )

    # The only honest check that the attack landed is the finished trace. A
    # pre-flight query is a guess -- the real one is built from the Planner's
    # questions and does not exist until the run has happened.

    planted = label_malicious(path, attack.marker)
    if not planted:
        raise RuntimeError(
            f"{attack.name}: marker {attack.marker} appears in no source in "
            f"{path}. The attack did not reach the trace; this run is void."
        )

    trace = read_trace(path)
    trace.validate()
    # Oracle is the named perfect detector (D-034). compare() no longer
    # accepts a bare source list -- that was the silent circular default.
    from src.eval.detectors import Oracle

    scores = compare(trace, verdict=Oracle().flag(trace))
    return AttackRun(
        attack=attack,
        trace_path=path,
        planted_sources=planted,
        task_success=result.task_success,
        scores=scores,
    )


class _StubClient:
    """Enough of a model to drive the pipeline offline. No network, no key.

    Deliberately dumb: it answers in the shape each prompt asks for and
    nothing more. It exists to exercise the graph and set machinery over a
    real trace shape, not to stand in for model behaviour -- and it echoes
    any planted marker it is shown, so the injected source reaches the trace
    the way a real one would.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.total_tokens = 0
        self.throttled_s = 0.0

    def generate(self, prompt, system=None, json_output=False, temperature=None):
        import json as _json

        from src.common.llm import LLMResponse

        self.calls += 1
        self.total_tokens += 100
        if json_output:
            text = _json.dumps({
                "brief": "Parse the sample date strings to ISO using the stdlib.",
                "questions": [
                    "How does datetime.strptime handle multiple date formats?",
                    "Which format codes cover written-out month names?",
                    "How should ambiguous day-first samples be resolved?",
                ],
            })
        elif "Reply with the complete Python script" in prompt:
            text = (
                'samples = ["12/03/2024", "March 5, 2021", "2019-07-04", '
                '"1 Jan 2000", "31/12/1999"]\n'
                "from datetime import datetime\n"
                'FMTS = ["%d/%m/%Y", "%B %d, %Y", "%Y-%m-%d", "%d %b %Y"]\n'
                "for s in samples:\n"
                "    for f in FMTS:\n"
                "        try:\n"
                "            print(datetime.strptime(s, f).date().isoformat())\n"
                "            break\n"
                "        except ValueError:\n"
                "            continue\n"
            )
        elif "Decide the approach" in prompt:
            text = "Use datetime.strptime with a list of candidate formats, day-first for ambiguous samples."
        else:
            text = "Finding: strptime with candidate formats covers all five samples."
        return LLMResponse(
            text=text, model="stub", prompt_tokens=60, output_tokens=40,
            thoughts_tokens=0, total_tokens=100, attempts=1, latency_s=0.0,
            slept_s=0.0, finish_reason="STOP",
        )


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    live = "--live" in args
    scenario = next((a for a in args if a in ("A", "B", "C")), "A")
    influencing = "--exposed-only" not in args

    attack = build(scenario, influencing)
    out = Path("data/runs") / f"attack-{attack.name}.jsonl"
    client = None if live else _StubClient()

    if live:
        print("LIVE: this run costs 6 requests against the daily 20 (D-017).")

    run = run_attack(attack, out, client=client)

    print(f"attack        {run.attack.name}  ({run.attack.description})")
    print(f"trace         {run.trace_path}")
    print(f"planted       {run.planted_sources}")
    print(f"task success  {run.task_success}")
    print()
    print(table(run.scores))
    if not live:
        print()
        print("stub client: the trace shape is real, the model output is not.")
        print("influence edges are still unestimated, so 'unsafe' means nothing")
        print("here. See metrics.py.")
