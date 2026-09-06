"""
Measure the noise floor (open issue #10).

Counterfactual replay reads a changed output as evidence that the removed
source mattered. That inference is only valid if an *unchanged* request would
have produced an unchanged output. Nobody has checked whether it does.

So: take one recorded request, send it again with nothing removed, N times,
and count how often the answer differs anyway. That is the floor. Every
influence result has to be read against it, and if the floor is high then
"the output changed" stops being evidence of anything.

WHERE THE REQUESTS COME FROM
----------------------------
The cassette. It stores the exact prompt, system instruction and model of
every call in the recorded run (D-019), which is precisely "the recorded
request" this measurement needs, and the trace cannot supply it -- event
content is stored by reference and there is no content store yet (D-010).

This is a **live** measurement that happens to read its inputs from a
cassette. Nothing is replayed: every trial is a real request, spends real
quota, and is a legitimate number for the paper. D-019 forbids quoting
numbers from a run whose requests did not happen; here they did.

RESUMABLE, BECAUSE OF THE QUOTA
-------------------------------
20 trials is 20 requests and the free tier allows 20 a day for everything
(D-017). Trials accumulate in one file across days and the tool tops up to
the target rather than starting over. It refuses to mix models, because a
noise floor belongs to one model and D-004 has already had a model retired
underneath it once.

    python -m src.provenance.noise --call 4 --trials 8      collect
    python -m src.provenance.noise --call 4 --report        analyse, free
"""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.common.cassette import Cassette
from src.common.config import Settings, load_settings
from src.common.llm import GeminiClient

NOISE_DIR = Path("data/noise")


# --- comparisons -------------------------------------------------------------
# The floor is not one number. It depends on what counts as "the same answer",
# and that is a choice the counterfactual check has to make too. Measuring
# under several comparisons brackets the problem instead of hiding it behind
# one definition -- the strictest and the loosest are both informative, and
# the honest thing is to report the one the method will actually use.

def _exact(text: str) -> str:
    return text


def _whitespace(text: str) -> str:
    """Same words, any spacing. Formatting churn stops counting as a flip."""
    return re.sub(r"\s+", " ", text).strip()


def _alphanumeric(text: str) -> str:
    """Loosest: letters and digits only, lowercased. Punctuation and casing
    drift stop counting. Still catches a genuinely different answer."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


# A fixed vocabulary, written before looking at the trials and deliberately
# not tuned afterwards. Tuning an extractor until it reports a stable answer
# would manufacture the result this measurement exists to test.
LIBRARIES = (
    "datetime", "strptime", "dateutil", "arrow", "pendulum",
    "pandas", "calendar", "time.strptime", "regex",
)


def _decision(text: str) -> str:
    """The substance of the choice, not the sentence that describes it.

    docs/03 issue #2 already says comparison has to happen at the semantic
    or behavioural level. This is the cheapest honest version of that for a
    library-choice decision: which known libraries does the answer name?
    Two answers naming the same set made the same decision, however
    differently they are worded.

    Crude on purpose. It is a floor measurement, not the final comparator --
    but a crude comparator that is fixed in advance beats a clever one tuned
    until it agrees with us.
    """
    lowered = text.lower()
    return ",".join(sorted({lib for lib in LIBRARIES if lib in lowered}))


COMPARISONS: dict[str, Callable[[str], str]] = {
    "exact": _exact,
    "whitespace": _whitespace,
    "alphanumeric": _alphanumeric,
    "decision": _decision,
}


@dataclass
class Floor:
    comparison: str
    trials: int
    distinct: int
    differs_from_original: int

    @property
    def rate(self) -> float:
        """Fraction of trials whose answer differed from the recorded one."""
        return self.differs_from_original / self.trials if self.trials else 0.0


def _path_for(model: str, key: str) -> Path:
    return NOISE_DIR / f"{model}__{key}.jsonl"


def load_trials(model: str, key: str) -> list[dict[str, Any]]:
    path = _path_for(model, key)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def collect(
    cassette_path: str | Path,
    call_index: int,
    target_trials: int,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Top up to `target_trials` real requests for one recorded call.

    Returns every trial on file, old and new. Makes only the requests needed
    to reach the target, so an interrupted collection costs nothing to
    resume -- which matters when the daily cap is 20.
    """
    settings = settings or load_settings()
    cassette = Cassette.load(cassette_path)
    records = [r for group in cassette.entries.values() for r in group]
    records.sort(key=lambda r: r["key"])
    # Preserve the cassette's file order rather than key order.
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in Path(cassette_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec["key"] not in seen:
            seen.add(rec["key"])
            ordered.append(rec)
    if not 0 <= call_index < len(ordered):
        raise IndexError(f"call {call_index} out of range, cassette has {len(ordered)}")
    call = ordered[call_index]

    if call["model"] != settings.model:
        # A floor belongs to a model. Measuring on one and quoting it for
        # another is exactly the mistake D-004's amendment was written about.
        raise RuntimeError(
            f"cassette call was recorded on {call['model']} but settings say "
            f"{settings.model}. A noise floor is not transferable between "
            "models; set GEMINI_MODEL to match, or re-record."
        )

    existing = load_trials(settings.model, call["key"])
    needed = target_trials - len(existing)
    if needed <= 0:
        return existing

    client = GeminiClient(settings)
    path = _path_for(settings.model, call["key"])
    path.parent.mkdir(parents=True, exist_ok=True)

    print(f"call      [{call_index}] {call['key']}")
    print(f"model     {settings.model}  temperature {settings.temperature}")
    print(f"on file   {len(existing)} trials, collecting {needed} more")
    print(f"cost      {needed} requests against the daily 20 (D-017)")
    print()

    for n in range(needed):
        response = client.generate(
            call["prompt"],
            system=call.get("system"),
            # The recorded run used JSON mode only for the planner. The
            # cassette does not store the flag, so infer it the same way the
            # pipeline decides it: the planner is the only JSON call.
            json_output="Reply with JSON" in call["prompt"],
        )
        trial = {
            "key": call["key"],
            "model": settings.model,
            "temperature": settings.temperature,
            "text": response.text,
            "output_tokens": response.output_tokens,
            "total_tokens": response.total_tokens,
            "finish_reason": response.finish_reason,
            "timestamp": time.time(),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(trial) + "\n")
        existing.append(trial)
        same = _whitespace(response.text) == _whitespace(call["text"])
        print(f"  trial {len(existing):>2}  {response.output_tokens:>4} tok  "
              f"{'same as recorded' if same else 'DIFFERS'}")

    return existing


def analyse(
    cassette_path: str | Path, call_index: int, model: str
) -> tuple[dict[str, Any], list[Floor]]:
    """Compute the floor under every comparison. No API calls."""
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in Path(cassette_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec["key"] not in seen:
            seen.add(rec["key"])
            ordered.append(rec)
    call = ordered[call_index]
    trials = load_trials(model, call["key"])

    floors = []
    for name, normalise in COMPARISONS.items():
        original = normalise(call["text"])
        seen_texts = {normalise(t["text"]) for t in trials}
        differs = sum(1 for t in trials if normalise(t["text"]) != original)
        floors.append(
            Floor(
                comparison=name,
                trials=len(trials),
                distinct=len(seen_texts),
                differs_from_original=differs,
            )
        )
    return call, floors


def report(call: dict[str, Any], floors: list[Floor]) -> str:
    trials = floors[0].trials if floors else 0
    lines = [
        f"call        {call['key']}  ({call['output_tokens']} output tokens recorded)",
        f"prompt      {' '.join(call['prompt'].split())[:72]}...",
        f"trials      {trials}",
        "",
        f"{'comparison':<16}{'distinct':>9}{'differs':>9}{'floor':>8}",
        "-" * 42,
    ]
    for f in floors:
        lines.append(f"{f.comparison:<16}{f.distinct:>9}{f.differs_from_original:>9}{f.rate:>7.0%}")
    if trials and trials < 20:
        # With few trials a floor of 0% is weak evidence, and saying so is the
        # difference between a measurement and a reassuring number.
        lines += [
            "",
            f"NOT ENOUGH TRIALS. {trials} of 20. A floor of 0% on {trials} trials is",
            "consistent with a true rate well above 15%. Top up on the next",
            "day's quota before quoting this anywhere.",
        ]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]

    def opt(name: str, default: str) -> str:
        return args[args.index(name) + 1] if name in args else default

    cassette_path = opt("--cassette", "data/cassettes/run1.jsonl")
    call_index = int(opt("--call", "4"))
    only_report = "--report" in args

    settings = load_settings()
    if not only_report:
        trials = int(opt("--trials", "8"))
        collect(cassette_path, call_index, trials, settings)
        print()

    call, floors = analyse(cassette_path, call_index, settings.model)
    print(report(call, floors))
