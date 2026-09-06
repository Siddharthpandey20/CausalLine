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
from src.provenance.signatures import (
    CALIBRATION_PATH,
    Calibration,
    Signature,
    for_event,
    library_facet,
)

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

    The vocabulary moved to `src/provenance/signatures.py` when the real
    comparator was built, so there is one definition rather than two that can
    drift. It is byte-identical to the list that produced D-026's 0% floor, and
    `library_facet` is that function -- which keeps the measured result
    attached to the thing it measured.
    """
    return library_facet(text)


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


def _ordered_calls(cassette_path: str | Path) -> list[dict[str, Any]]:
    """Distinct recorded calls in the order the cassette recorded them.

    File order, not key order: `--call 4` has to mean the same call across the
    collect and analyse paths, and a hash order would renumber the calls the day
    a prompt changes.
    """
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in Path(cassette_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record["key"] not in seen:
            seen.add(record["key"])
            ordered.append(record)
    return ordered


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
    ordered = _ordered_calls(cassette_path)
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
    ordered = _ordered_calls(cassette_path)
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


def facet_floors(
    cassette_path: str | Path, call_index: int, model: str, kind: str = "decision"
) -> tuple[dict[str, Any], list[Floor], list[str]]:
    """The floor of each facet of the real comparator, not just of `_decision`.

    This is the measurement Phase 2 turns on, and it is the reason the facet
    exclusion rule in `signatures.py` is calibration rather than tuning: a facet
    is dropped when it moves on **unchanged** re-sends, which is a statement
    about the instrument and says nothing about influence.

    Free. It reads the trials already on disk and makes no request, so it can be
    re-run after any change to a comparator without spending quota.

    Returns (call, per-facet floors, facet names to exclude).
    """
    ordered = _ordered_calls(cassette_path)
    call = ordered[call_index]
    trials = load_trials(model, call["key"])
    comparator = for_event(kind, call["text"])

    original = comparator.signature(call["text"])
    signatures = [comparator.signature(t["text"]) for t in trials]

    floors: list[Floor] = []
    exclude: list[str] = []
    for facet in original.facets:
        values = {s.facets.get(facet, "") for s in signatures}
        differs = sum(1 for s in signatures if s.facets.get(facet) != original.facets[facet])
        floor = Floor(
            comparison=f"{comparator.name}.{facet}",
            trials=len(trials),
            distinct=len(values),
            differs_from_original=differs,
        )
        floors.append(floor)
        if differs:
            exclude.append(facet)

    whole = Signature(comparator.name, original.facets).value
    floors.append(
        Floor(
            comparison=f"{comparator.name} (all facets)",
            trials=len(trials),
            distinct=len({s.value for s in signatures}),
            differs_from_original=sum(1 for s in signatures if s.value != whole),
        )
    )
    # The number the acceptance criterion turns on: the signature the estimator
    # will actually use, after the exclusion rule has been applied. If this is
    # not 0% the comparator cannot carry evidence and the counterfactual stage
    # has nothing to stand on.
    keep = set(original.facets) - set(exclude)
    restricted = original.restricted_to(keep).value
    floors.append(
        Floor(
            comparison=f"{comparator.name} (calibrated)",
            trials=len(trials),
            distinct=len({s.restricted_to(keep).value for s in signatures}),
            differs_from_original=sum(
                1 for s in signatures if s.restricted_to(keep).value != restricted
            ),
        )
    )
    return call, floors, exclude


def facet_report(
    call: dict[str, Any], floors: list[Floor], exclude: list[str], comparator: str
) -> str:
    trials = floors[0].trials if floors else 0
    lines = [
        f"comparator  {comparator}",
        f"call        {call['key']}",
        f"trials      {trials} unchanged re-sends already on disk (no requests made)",
        "",
        f"{'facet':<28}{'distinct':>9}{'differs':>9}{'floor':>8}",
        "-" * 54,
    ]
    for f in floors:
        lines.append(
            f"{f.comparison:<28}{f.distinct:>9}{f.differs_from_original:>9}{f.rate:>7.0%}"
        )
    lines.append("")
    if exclude:
        lines += [
            f"EXCLUDE {exclude}: these facets move when nothing was changed, so a",
            "flip in them is not evidence. Dropped from the signature by the rule",
            "fixed in signatures.py -- calibrated against the null, not against",
            "any influence result.",
            "",
            "This is the one place the method knowingly trades safety for signal:",
            "a source that would only have moved an excluded facet now reads as",
            "clean. Keeping the facet is not the safe alternative -- at a 75%",
            "floor the check answers 'influenced' whatever was removed, which is",
            "the conservative fallback with extra steps. See Calibration.",
        ]
    else:
        lines += [
            "Every facet held still across all unchanged re-sends, so the whole",
            "signature can carry evidence. This is the property a counterfactual",
            "verdict rests on: a flip now means the removal did something.",
        ]

    calibrated = next(
        (f for f in floors if f.comparison.endswith("(calibrated)")), None
    )
    if calibrated is not None:
        lines += ["", "-" * 54]
        verdict = "PASS" if calibrated.differs_from_original == 0 else "FAIL"
        lines.append(
            f"{verdict}: the signature the estimator will use differs on "
            f"{calibrated.differs_from_original} of {calibrated.trials} unchanged "
            "re-sends."
        )
        if verdict == "PASS":
            lines += [
                "Text comparison differed on every one of the same trials (D-026).",
                "So the instrument holds still where the old one did not, which is",
                "what a counterfactual verdict needs in order to mean anything.",
            ]
        else:
            lines.append(
                "Counterfactual replay cannot produce an edge under this "
                "comparator. Fix the comparator, do not widen the exclusion list."
            )
    if trials and trials < 20:
        lines += [
            "",
            f"NOT ENOUGH TRIALS. {trials} of 20. A 0% floor on {trials} trials is still",
            "consistent with a true rate near 30% (D-026's own caveat). Top up on",
            "the next day's quota before quoting a number from this.",
        ]
    return "\n".join(lines)


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
    facets_only = "--facets" in args
    kind = opt("--kind", "decision")

    # --facets and --report are both free. Only the collect path spends quota,
    # and it is the only path that needs an API key -- so a teammate without one
    # can still re-measure a comparator.
    if facets_only or only_report:
        model = opt("--model", "")
        if not model:
            from src.common.config import DEFAULT_MODEL, read_env_file

            model = read_env_file().get("GEMINI_MODEL") or DEFAULT_MODEL
    else:
        model = load_settings().model

    if facets_only:
        call, floors, exclude = facet_floors(cassette_path, call_index, model, kind)
        comparator = for_event(kind, call["text"]).name
        print(facet_report(call, floors, exclude, comparator))
        if "--write-calibration" in args:
            calibration = Calibration.load(model=model)
            calibration.model = model
            calibration.excluded[comparator] = exclude
            calibration.floors[comparator] = {
                f.comparison.split(".", 1)[-1]: f.rate
                for f in floors
                if "." in f.comparison
            }
            calibration.trials[comparator] = floors[0].trials if floors else 0
            calibration.save()
            print()
            print(f"written to {CALIBRATION_PATH}. The estimator reads it and")
            print("records the excluded facets on every verdict that relied on them.")
        raise SystemExit(0)

    if not only_report:
        settings = load_settings()
        trials = int(opt("--trials", "8"))
        collect(cassette_path, call_index, trials, settings)
        print()

    call, floors = analyse(cassette_path, call_index, model)
    print(report(call, floors))
