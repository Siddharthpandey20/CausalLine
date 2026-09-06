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
from src.eval.metrics import all_exposure_pairs, compare, table
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
    assume_all_checked: bool = False,
) -> AttackRun:
    """One poisoned run, labelled and scored.

    Raises if the planted source never made it into the trace. A run where
    the attack did not land is not a failed attack, it is not a run -- and
    counting it would put a scenario in the results table that never
    happened.

    `assume_all_checked` defaults to **False** and you should think hard
    before turning it on. It asserts that influence analysis examined every
    exposure, so an exposure with no influence edge was tested and cleared.
    On a trace where analysis has not run, every exposure has no edge, and
    that assertion turns "nothing has been analysed" into "nothing was
    influenced" -- 100% work preserved on a run that really was poisoned.
    That is a perfect unsafe preservation, manufactured by an assumption
    rather than by a mistaken measurement, and it is the exact error the
    whole project exists to avoid. It is only true of the authored fixtures,
    where the edges were written by hand alongside the trace.
    """
    path = Path(path)
    clean = Tools.from_fixtures(memory_path=path.with_suffix(".memory.json"))
    result = run_pipeline(path, tools=attack.apply(clean), client=client)

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
    if not trace.influence and assume_all_checked:
        raise RuntimeError(
            f"{path} has no influence edges, so no analysis has run on it. "
            "assume_all_checked would read that as 'every exposure was "
            "tested and came back clean' and report the run as fully "
            "preserved -- an unsafe preservation invented out of an "
            "assumption. Run influence analysis first, or leave the flag off."
        )
    checked = all_exposure_pairs(trace) if assume_all_checked else None
    # The detector stand-in is handed exactly the planted ids -- the perfect
    # detector docs/01-scope.md assumes and does not build.
    scores = compare(trace, malicious=planted, checked=checked)
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
    scenario = next((a for a in args if a in ("A", "B")), "A")
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
