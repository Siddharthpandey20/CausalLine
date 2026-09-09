"""
Group testing: find the few influential sources among many, in O(k log(n/k))
counterfactuals instead of n.

Phase 11.2. Dorfman (1943) pooled every soldier's blood sample eight at a time
and tested the pool; a negative pool cleared eight men with one test. The same
arithmetic applies here for the same reason -- influential sources are sparse,
and a group that changes nothing when removed contains nothing that mattered.

    remove the whole group in ONE call
      group did not move the decision  -> every source in it is clean. Done.
      group moved the decision         -> split it and recurse

The cost that motivates it: a Researcher event in our own pipeline carries
eight exposures, and the Coder's decision event carries eleven. Leave-one-out
spends one counterfactual per candidate, so identifying which of thirty
candidates mattered costs thirty calls -- while the recovery replay those
thirty calls exist to *avoid* may cost one. That ratio is docs/03 issue #7's
collapse condition, stated as a number.

THE ASSUMPTION, AND WHAT BREAKS IT
----------------------------------
Group removal assumes **no interaction between sources**. Two sources that
jointly cause an effect neither causes alone are invisible to it whenever the
split puts them in different halves: each half is then removed without the
other, neither removal moves the decision, and both halves are declared clean.

This is a real false-clean -- the unsafe direction -- and it is an accepted
approximation, not a bug. Three things make it bearable and all three belong
in the writeup rather than in a footnote:

  * single-source leave-one-out has the *same* blind spot for the same reason
    (D-030: when two sources supply the same fact, neither is individually
    necessary, so leave-one-out calls both unused). Group testing does not
    introduce the problem; it inherits it.
  * the poisoned sources in scenarios A, B and C carry a directive no clean
    source carries, so they stay individually necessary and remain detectable.
  * `interaction_suspected` below reports when the recursion's own arithmetic
    is inconsistent -- a group that mattered whose halves both came back clean
    is exactly the fingerprint of an interaction, and it is counted rather
    than swallowed.

The exhaustive alternative is testing every subset, which is 2^n. That is not
a trade we are making.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from src.common.prompts import (
    SourceNotInPrompt,
    redact_source,
    splice_block,
)
from src.provenance.signatures import Calibration, Comparator, compare, for_event

# decision_fn(group) -> True when removing `group` changes the decision.
DecisionFn = Callable[[Sequence[str]], bool]


@dataclass
class GroupTestDiagnostics:
    """What the recursion cost and whether its assumption held.

    `positive_group_rate` is the trigger Phase 11.3 watches: group testing pays
    off only when influential sources are sparse, and a high fraction of groups
    coming back "mattered" is that sparsity failing. When it does, recursive
    halving degenerates towards one call per candidate plus the overhead of the
    splits -- worse than leave-one-out, not better.
    """

    calls: int = 0
    groups_tested: int = 0
    groups_mattered: int = 0
    singleton_tests: int = 0
    max_depth: int = 0
    # Verdicts reached by elimination rather than by a call, under
    # `infer_sibling`. Counted separately because they rest on the parent's
    # verdict rather than on evidence of their own.
    inferred: int = 0
    # A group that mattered but whose halves both came back clean. The
    # fingerprint of an interaction effect the module cannot resolve.
    interaction_suspected: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def positive_group_rate(self) -> float:
        return self.groups_mattered / self.groups_tested if self.groups_tested else 0.0

    def sparsity_failing(self, threshold: float = 0.75) -> bool:
        """Whether to fall back to fixed-budget attribution (Phase 11.3).

        Needs enough groups to be a rate rather than an accident; three splits
        all coming back positive is not evidence of anything.
        """
        return self.groups_tested >= 6 and self.positive_group_rate >= threshold

    def line(self) -> str:
        return (
            f"{self.calls} counterfactual calls, {self.groups_tested} groups "
            f"({self.groups_mattered} mattered, "
            f"{self.positive_group_rate:.0%}), "
            f"{self.singleton_tests} singletons, depth {self.max_depth}"
            + (f", {self.inferred} inferred by elimination" if self.inferred else "")
            + (
                f", {self.interaction_suspected} suspected interaction(s)"
                if self.interaction_suspected
                else ""
            )
        )


def split_in_half(candidates: Sequence[str]) -> tuple[list[str], list[str]]:
    """Halve a candidate list. Deterministic, so a rerun tests the same groups
    and a call-count comparison means something."""
    mid = len(candidates) // 2
    return list(candidates[:mid]), list(candidates[mid:])


def group_test(
    candidates: Sequence[str],
    decision_fn: DecisionFn,
    diagnostics: GroupTestDiagnostics | None = None,
    infer_sibling: bool = False,
    _depth: int = 0,
    _parent_mattered: bool = False,
) -> list[str]:
    """The influential subset of `candidates`, by recursive halving.

    `decision_fn(group)` removes the whole group in one call and reports
    whether the decision signature moved. It is the only expensive thing here;
    every call is counted in `diagnostics`.

    The recursion descends into a half **only if that half mattered**, which is
    where the saving comes from: a clean half of sixteen costs one call instead
    of sixteen.

    `infer_sibling` skips one call per split. Inside the recursion the parent
    group is already known to have mattered, so if the first half comes back
    clean the second half *must* contain the influence -- testing it asks a
    question whose answer is implied. It roughly halves the call count and it
    is **off by default**, for a reason worth stating: the inference is only as
    sound as the parent's verdict. A parent "mattered" that was really signature
    instability propagates into a sibling declared influential without ever
    being tested, and that error is then invisible. Off is the version Phase
    11.2 specifies; on is measured beside it so the saving can be reported
    rather than assumed.
    """
    diagnostics = diagnostics if diagnostics is not None else GroupTestDiagnostics()
    diagnostics.max_depth = max(diagnostics.max_depth, _depth)
    candidates = list(candidates)

    if not candidates:
        return []

    if len(candidates) == 1:
        if infer_sibling and _parent_mattered:
            # A singleton whose parent mattered and whose sibling was cleared
            # is the influence, by elimination. No call to make.
            diagnostics.inferred += 1
            return list(candidates)
        diagnostics.calls += 1
        diagnostics.groups_tested += 1
        diagnostics.singleton_tests += 1
        mattered = bool(decision_fn(candidates))
        diagnostics.groups_mattered += int(mattered)
        return list(candidates) if mattered else []

    half_a, half_b = split_in_half(candidates)
    result: list[str] = []

    diagnostics.calls += 1
    diagnostics.groups_tested += 1
    a_mattered = bool(decision_fn(half_a))
    diagnostics.groups_mattered += int(a_mattered)
    if a_mattered:
        result += group_test(
            half_a, decision_fn, diagnostics, infer_sibling, _depth + 1, True
        )

    if infer_sibling and _parent_mattered and not a_mattered:
        b_mattered = True
        diagnostics.inferred += 1
    else:
        diagnostics.calls += 1
        diagnostics.groups_tested += 1
        b_mattered = bool(decision_fn(half_b))
        diagnostics.groups_mattered += int(b_mattered)
    if b_mattered:
        result += group_test(
            half_b, decision_fn, diagnostics, infer_sibling, _depth + 1, True
        )

    if (a_mattered or b_mattered) and not result:
        # A half mattered, but recursing into it found no single source that
        # did. Either the effect is an interaction between sources now split
        # across sub-groups, or the decision signature is unstable. Both make
        # the clean verdict below untrustworthy, and both are invisible unless
        # counted here.
        diagnostics.interaction_suspected += 1
        diagnostics.notes.append(
            f"depth {_depth}: a group of {len(candidates)} moved the decision "
            "but no individual source in it did -- interaction effect or an "
            "unstable signature; this half's 'clean' verdict is not evidence"
        )
    return result


def leave_one_out(
    candidates: Sequence[str],
    decision_fn: DecisionFn,
    diagnostics: GroupTestDiagnostics | None = None,
) -> list[str]:
    """Exhaustive single-source removal. The n-call baseline group testing is
    measured against, kept here so both are driven by the same decision_fn."""
    diagnostics = diagnostics if diagnostics is not None else GroupTestDiagnostics()
    found: list[str] = []
    for candidate in candidates:
        diagnostics.calls += 1
        diagnostics.groups_tested += 1
        diagnostics.singleton_tests += 1
        if decision_fn([candidate]):
            diagnostics.groups_mattered += 1
            found.append(candidate)
    return found


# --- the real decision function -----------------------------------------------


def redact_group(block: str, source_ids: Iterable[str]) -> str:
    """Remove several sources from a rendered block, in one pass.

    Chained through `redact_source`, so the surgery is the same one a
    single-source counterfactual does and there is no second implementation of
    it to drift. Each removal re-parses the shrinking block, which is what
    keeps the spans right after the first one.
    """
    out = block
    for sid in source_ids:
        out = redact_source(out, sid)
    return out


@dataclass
class CounterfactualDecision:
    """Removes a whole group in one call and reports whether the decision moved.

    Reuses `for_event` + `compare` from src/provenance/signatures.py -- Phase
    2's comparator, unchanged. Text comparison is not available here for the
    reason D-026 settled it is not available anywhere: an 8-of-8 textual noise
    floor makes a flip carry zero information.

    A group that cannot be redacted, or an event with no stored prompt, returns
    True -- "it mattered". That routes the group into the recursion instead of
    clearing it, which costs calls and never costs safety. Returning False on a
    redaction that removed nothing is the false clean D-029 is about.
    """

    client: Any
    request: Any  # AttributionRequest
    comparator: Comparator | None = None
    calibration: Calibration | None = None
    context: dict[str, Any] | None = None
    calls: int = 0
    tokens: int = 0
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.comparator = self.comparator or for_event(
            self.request.kind, self.request.output
        )
        self.calibration = self.calibration or Calibration()
        self._excluded = self.calibration.exclude_for(self.comparator.name)
        self._before = self.comparator.signature(self.request.output, self.context)

    def __call__(self, group: Sequence[str]) -> bool:
        if not self.request.prompt or not self.request.source_block:
            self.errors.append(
                f"{self.request.event_id}: no stored prompt or source block; "
                "cannot re-issue, defaulting to influenced"
            )
            return True
        try:
            reduced = redact_group(self.request.source_block, group)
            prompt = splice_block(
                self.request.prompt, self.request.source_block, reduced
            )
        except (SourceNotInPrompt, KeyError) as exc:
            self.errors.append(
                f"{self.request.event_id}: could not redact {list(group)}: {exc}"
            )
            return True

        response = self.client.generate(prompt, system=self.request.system)
        self.calls += 1
        self.tokens += getattr(response, "total_tokens", 0)
        after = self.comparator.signature(response.text, self.context)
        same, _moved = compare(self._before, after, exclude=self._excluded)
        return not same


def group_test_event(
    client: Any,
    request: Any,
    candidates: Sequence[str] | None = None,
    calibration: Calibration | None = None,
    context: dict[str, Any] | None = None,
) -> tuple[list[str], GroupTestDiagnostics, CounterfactualDecision]:
    """Group-test one event's exposures against the real model/client.

    Returns the influential set, the diagnostics (which say whether the
    sparsity assumption held), and the decision function (which carries the
    token cost, so the caller can log it with `purpose="counterfactual"` like
    every other analysis call).
    """
    candidates = list(candidates if candidates is not None else request.exposures)
    decision = CounterfactualDecision(
        client=client,
        request=request,
        calibration=calibration,
        context=context,
    )
    diagnostics = GroupTestDiagnostics()
    influential = group_test(candidates, decision, diagnostics)
    for error in decision.errors:
        diagnostics.notes.append(error)
    return influential, diagnostics, decision


# --- the measurement Phase 11.2 requires --------------------------------------


@dataclass
class GroupTestComparison:
    """Group testing against exhaustive leave-one-out, on one real event."""

    event_id: str
    agent_id: str
    candidates: int
    truly_influential: list[str] = field(default_factory=list)
    loo_found: list[str] = field(default_factory=list)
    group_found: list[str] = field(default_factory=list)
    loo_calls: int = 0
    group_calls: int = 0
    group_calls_inferred: int = 0
    diagnostics: GroupTestDiagnostics | None = None

    @property
    def agrees(self) -> bool:
        return set(self.loo_found) == set(self.group_found)

    @property
    def reduction(self) -> float:
        """Fraction of leave-one-out's calls that group testing did not make."""
        if not self.loo_calls:
            return 0.0
        return 1.0 - (self.group_calls / self.loo_calls)

    def line(self) -> str:
        return (
            f"{self.event_id} {self.agent_id:<10} n={self.candidates:>2} "
            f"k={len(self.truly_influential):<2} "
            f"LOO={self.loo_calls:>2} calls  GT={self.group_calls:>2} calls "
            f"(GT+infer={self.group_calls_inferred:>2})  "
            f"reduction={self.reduction:+.0%}  "
            f"{'AGREE' if self.agrees else 'DISAGREE'}"
        )


def measure_on_scenario(
    scenario: str = "A",
    influencing: bool = True,
    workdir: str | Path = "data/runs/grouptest",
    seed: int = 20260906,
    min_candidates: int = 4,
) -> list[GroupTestComparison]:
    """Run a scripted scenario, then group-test every multi-source event on it.

    Both methods are driven by the **same** `CounterfactualDecision`, so a
    difference in call count is a difference in how many questions were asked
    and nothing else. Ground truth is the scripted client's leave-one-out
    record (D-030), which neither method is shown.
    """
    from src.eval.attacks import build, label_malicious
    from src.eval.scripted import ScriptedClient, ground_truth_influence
    from src.provenance.estimator import request_for
    from src.tracing.logger import read_trace
    from src.tracing.pipeline import run_pipeline
    from src.tracing.tools import Tools

    out_dir = Path(workdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    variant = "influencing" if influencing else "exposed_only"
    attack = build(scenario, influencing)
    path = out_dir / f"gt-{scenario}-{variant}.jsonl"
    tools = attack.apply(
        Tools.from_fixtures(memory_path=path.with_suffix(".memory.json"))
    )
    client = ScriptedClient(seed=seed)
    run_pipeline(path, client=client, tools=tools, handoff_hook=attack.handoff_hook)
    if not label_malicious(path, attack.marker):
        raise RuntimeError(f"{attack.name}: marker never reached the trace")

    trace = read_trace(path)
    truth = ground_truth_influence(trace, client)
    comparisons: list[GroupTestComparison] = []

    for event in trace.events:
        request = request_for(trace, event.id)
        if request is None or len(request.exposures) < min_candidates:
            continue
        candidates = list(request.exposures)

        loo_decision = CounterfactualDecision(client=client, request=request)
        loo_diag = GroupTestDiagnostics()
        loo_found = leave_one_out(candidates, loo_decision, loo_diag)

        gt_decision = CounterfactualDecision(client=client, request=request)
        gt_diag = GroupTestDiagnostics()
        gt_found = group_test(candidates, gt_decision, gt_diag)

        infer_decision = CounterfactualDecision(client=client, request=request)
        infer_diag = GroupTestDiagnostics()
        group_test(candidates, infer_decision, infer_diag, infer_sibling=True)

        comparisons.append(
            GroupTestComparison(
                event_id=event.id,
                agent_id=event.agent_id,
                candidates=len(candidates),
                truly_influential=sorted(
                    sid for sid in candidates if (sid, event.id) in truth
                ),
                loo_found=sorted(loo_found),
                group_found=sorted(gt_found),
                loo_calls=loo_diag.calls,
                group_calls=gt_diag.calls,
                group_calls_inferred=infer_diag.calls,
                diagnostics=gt_diag,
            )
        )
    return comparisons


if __name__ == "__main__":
    import random

    print("Group testing vs exhaustive leave-one-out, on synthetic candidate")
    print("sets with a known influential subset. The saving is a function of")
    print("sparsity, which is the assumption the method rests on.")
    print()
    print(
        f"{'n':>5}{'k':>4}{'LOO':>7}{'GT':>6}{'GT+infer':>10}"
        f"{'ratio':>8}{'k log2(n/k)':>13}"
    )
    print("-" * 55)
    import math

    for n in (8, 16, 32, 64):
        for k in sorted({1, 2, max(1, n // 4)}):
            rng = random.Random(4242)
            truly = set(rng.sample(range(n), k))
            candidates = [f"S{i}" for i in range(n)]
            influential_ids = {f"S{i}" for i in truly}

            def decide(group, ids=influential_ids):
                return bool(set(group) & ids)

            plain = GroupTestDiagnostics()
            found = group_test(candidates, decide, plain)
            assert set(found) == influential_ids, (found, influential_ids)

            smart = GroupTestDiagnostics()
            found_smart = group_test(candidates, decide, smart, infer_sibling=True)
            assert set(found_smart) == influential_ids

            bound = k * math.log2(max(2.0, n / k))
            print(
                f"{n:>5}{k:>4}{n:>7}{plain.calls:>6}{smart.calls:>10}"
                f"{smart.calls / n:>8.2f}{bound:>13.1f}"
            )
    print()
    print("LOO = exhaustive leave-one-out (n calls). GT = the recursion Phase")
    print("11.2 specifies. GT+infer skips the sibling call the parent's verdict")
    print("already implies -- cheaper, and sound only while the parent's verdict")
    print("is. Sparsity is what buys the saving: at k = n/4 neither beats LOO.")
    print()

    print("ON REAL SCRIPTED RUNS -- same decision function driving both, so a")
    print("call-count difference is a difference in questions asked and nothing")
    print("else. Ground truth is the scripted client's own leave-one-out record.")
    print()
    total_loo = total_gt = total_infer = 0
    for scenario in ("A", "B", "C"):
        for influencing in (True, False):
            for row in measure_on_scenario(scenario, influencing):
                label = f"{scenario}-{'infl' if influencing else 'exp'}"
                print(f"  {label:<8} {row.line()}")
                total_loo += row.loo_calls
                total_gt += row.group_calls
                total_infer += row.group_calls_inferred
                if row.diagnostics and row.diagnostics.sparsity_failing():
                    print(f"           SPARSITY FAILING: "
                          f"{row.diagnostics.line()} -- fall back to "
                          f"budget attribution (Phase 11.3)")
    print()
    print(f"  TOTAL  leave-one-out {total_loo} calls, group testing {total_gt} "
          f"({1 - total_gt/total_loo:+.0%}), with sibling inference "
          f"{total_infer} ({1 - total_infer/total_loo:+.0%})")
