"""IREC: the properties that carry its safety argument, and the default it must not change.

Every failure mode demonstrated during validation gets a test here, because
each one is cheap to reintroduce:

1. The cutoff is EXACT. Substituting the production signature comparator was
   measured to produce 1 and 4 unsafe preservations on A/influencing.
2. IREC asks no self-report. Its whole claim is that the replay subsumes
   attribution; a self-report reaching the client voids the cost measurement.
3. With nothing flagged, every prompt reproduces byte-for-byte and no model
   call is made -- the fidelity property that lets a later difference be read
   as the intervention rather than the harness.
4. `(S3, e0010)` is the case production attribution gets WRONG: the
   counterfactual records it clean under `targeted_only` while ground truth
   says S3 influenced e0010. IREC must not preserve it.
5. Cost never exceeds `min(B0, B2)`, and the re-executed set is inside B2's
   closure (Theorem 2's premises).
6. A differing reply is treated as CHANGED. Nondeterminism must cost replay,
   never safety.
7. The production default does not move.

    python -m unittest tests.test_irec -v

Free and deterministic: scripted client, no key, no network.
See docs/research/IREC_SAFETY_AND_NOVELTY.md.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from src.eval.attacks import build
from src.eval.baselines import (
    b0_full_restart,
    b2_topology_closure,
    discarded_events,
)
from src.eval.detectors import build as build_detector
from src.eval.experiment import (
    WORKFLOWS,
    _fresh_tools,
    _original_run,
    run_baseline_recovery,
)
from src.eval.metrics import ground_truth_events
from src.eval.scripted import ScriptedClient
from src.recovery.irec import CutoffClient, recover_irec
from src.recovery.replay import pipeline_model_events
from src.tracing.logger import read_trace

SEED = 20260906


def _setup(tmp: Path, scenario: str, influencing: bool, workflow: str = "short",
           detector: str = "oracle", estimator: str = "none"):
    """One original run, plus everything a recovery needs to be scored."""
    variant = "influencing" if influencing else "exposed_only"
    stem = f"irec-{scenario}-{variant}-{workflow}-{detector}"
    orig_path = tmp / f"{stem}.jsonl"
    _result, _client, true_inf = _original_run(
        scenario, influencing, orig_path, SEED, estimator, workflow=workflow)
    original = read_trace(orig_path)
    attack = build(scenario, influencing)
    flagged = set(build_detector(detector).flag(original).sources())
    truth = ground_truth_events(original, true_influence=true_inf)
    return original, attack, flagged, truth, stem


def _run_irec(tmp: Path, original, attack, flagged, stem, workflow="short",
              flagged_override=None):
    out = tmp / f"{stem}-IREC.jsonl"
    use = flagged if flagged_override is None else flagged_override
    return recover_irec(
        original, use, ScriptedClient(seed=SEED + 1), out,
        task=original.meta.get("task"),
        tools=_fresh_tools(attack, out, extended=(workflow == "long")),
        handoff_hook=attack.handoff_hook,
        workflow_kwargs=dict(WORKFLOWS[workflow]),
    ), out


class TestTheCutoffIsExact(unittest.TestCase):
    """Property 1. The safety argument is 'the model produced this output on
    this prompt', which survives no comparator."""

    def test_the_source_compares_bytes_and_nothing_else(self) -> None:
        source = inspect.getsource(CutoffClient.generate)
        self.assertIn("response.text == original_output", source,
                      "the cutoff must be byte equality")
        for forbidden in ("cosine", "similarity", "threshold", "noise_floor",
                          "signature", "compare("):
            self.assertNotIn(
                forbidden, source,
                f"{forbidden!r} appears in the cutoff rule; relaxing exact "
                "equality was measured to produce unsafe preservations",
            )

    def test_zero_unsafe_preservations_on_the_scripted_matrix(self) -> None:
        """Exact ground truth per (source, event), every cell."""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            for scenario in ("A", "B", "C"):
                for influencing in (True, False):
                    with self.subTest(cell=f"{scenario}/{influencing}"):
                        original, attack, flagged, truth, stem = _setup(
                            tmp, scenario, influencing)
                        result, _out = _run_irec(tmp, original, attack,
                                                 flagged, stem)
                        kept = truth - set(
                            discarded_events(result.report, result.invalidated))
                        self.assertFalse(
                            kept,
                            f"IREC preserved truly-contaminated events {sorted(kept)}",
                        )


class TestIRECAsksNoSelfReport(unittest.TestCase):
    """Property 2."""

    def test_a_self_report_prompt_is_refused(self) -> None:
        from src.recovery.replay import SELF_REPORT_MARKER

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            original, _attack, flagged, _truth, _stem = _setup(tmp, "A", True)
            client = CutoffClient(original=original,
                                  inner=ScriptedClient(seed=SEED + 1),
                                  flagged=flagged)
            with self.assertRaises(RuntimeError):
                client.generate(f"something {SELF_REPORT_MARKER} something")

    def test_a_recovery_reports_no_analysis_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            original, attack, flagged, _truth, stem = _setup(tmp, "A", True)
            result, _out = _run_irec(tmp, original, attack, flagged, stem)
            self.assertEqual(result.analysis_tokens, 0,
                             "IREC has no attribution stage to charge for")


class TestTheFidelityProperty(unittest.TestCase):
    """Property 3. With no flagged sources nothing is redacted, so every prompt
    must reproduce byte-for-byte and the replay must make no model call at all.

    This is also the evidence for assumption A1 (input completeness) and A5
    (complete mediation): a prompt is assembled from upstream outputs and from
    tool and memory state, so if any mediating surface had drifted, downstream
    prompts would differ.
    """

    def test_nothing_flagged_means_everything_splices(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            for workflow in ("short", "long"):
                with self.subTest(workflow=workflow):
                    original, attack, _flagged, _truth, stem = _setup(
                        tmp, "A", True, workflow=workflow)
                    model_events = pipeline_model_events(original)
                    result, _out = _run_irec(tmp, original, attack, set(),
                                             stem + f"-gate-{workflow}",
                                             workflow=workflow,
                                             flagged_override=set())
                    self.assertEqual(
                        result.spliced, len(model_events),
                        "a replay with nothing removed must reproduce every "
                        "prompt byte-for-byte",
                    )
                    self.assertEqual(result.calls, 0,
                                     "and must therefore make no model call")
                    self.assertEqual(result.replay_tokens, 0)


class TestTheCaseProductionAttributionGetsWrong(unittest.TestCase):
    """Property 4. `(S3, e0010)` on A/influencing.

    Under `targeted_only` the production counterfactual records the pair as
    clean while ground truth says S3 influenced e0010, so CausalLine's
    event-level safety there rests on a self-report positive rather than on the
    counterfactual. IREC has no counterfactual stage to be wrong.
    """

    def test_the_production_counterfactual_still_gets_it_wrong(self) -> None:
        """Pinned so that if this is ever fixed, the claim gets re-read rather
        than silently becoming stale."""
        from src.provenance.checks import CheckLedger

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = tmp / "a-targeted.jsonl"
            _r, _c, true_inf = _original_run("A", True, path, SEED,
                                             "targeted_only")
            trace = read_trace(path)
            truth_pairs = {(s, e) for (s, e) in (true_inf or set())}
            if ("S3", "e0010") not in truth_pairs:
                self.skipTest("the scripted ground truth for this pair moved")
            cleared = CheckLedger.from_trace(trace).cleared_pairs()
            self.assertIn(
                ("S3", "e0010"), cleared,
                "the counterfactual no longer returns a false clean here; "
                "re-read docs/research/IREC_SAFETY_AND_NOVELTY.md section 4",
            )

    def test_irec_does_not_preserve_e0010(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            original, attack, flagged, truth, stem = _setup(tmp, "A", True)
            if "e0010" not in truth:
                self.skipTest("e0010 is not contaminated in this scenario")
            result, _out = _run_irec(tmp, original, attack, flagged, stem)
            discarded = set(discarded_events(result.report, result.invalidated))
            self.assertIn(
                "e0010", discarded,
                "IREC kept the event the production counterfactual clears "
                "unsafely",
            )


class TestTheCostAndClosureBounds(unittest.TestCase):
    """Property 5. Theorem 2's premises, as assertions."""

    def _bounds(self, tmp: Path, scenario: str, influencing: bool,
                workflow: str):
        original, attack, flagged, _truth, stem = _setup(
            tmp, scenario, influencing, workflow=workflow)
        result, _out = _run_irec(tmp, original, attack, flagged, stem,
                                 workflow=workflow)
        costs = {}
        for name, discard in (("B0", b0_full_restart(original, flagged)),
                              ("B2", b2_topology_closure(original, flagged))):
            out = tmp / f"{stem}-{name}.jsonl"
            _res, rep = run_baseline_recovery(
                name, discard, original, ScriptedClient(seed=SEED + 1),
                sorted(flagged), attack, out,
                tools=_fresh_tools(attack, out, extended=(workflow == "long")))
            costs[name] = rep.replay_tokens
        closure = set(b2_topology_closure(original, flagged))
        return result, costs, closure

    def test_irec_never_costs_more_than_the_cheaper_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            for workflow in ("short", "long"):
                for scenario in ("A", "B", "C"):
                    with self.subTest(cell=f"{scenario}/{workflow}"):
                        result, costs, _cl = self._bounds(tmp, scenario, True,
                                                          workflow)
                        self.assertLessEqual(
                            result.replay_tokens,
                            min(costs["B0"], costs["B2"]),
                            "IREC exceeded min(B0, B2), which Theorem 2 says "
                            "cannot happen for a subset of the closure",
                        )

    def test_the_reexecuted_set_lies_inside_the_b2_closure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            for scenario in ("A", "B", "C"):
                with self.subTest(scenario=scenario):
                    result, _costs, closure = self._bounds(tmp, scenario, True,
                                                           "short")
                    extra = set(result.invalidated) - closure
                    self.assertFalse(
                        extra,
                        f"IREC re-executed events outside B2's closure: "
                        f"{sorted(extra)}",
                    )


class TestNondeterminismIsConservative(unittest.TestCase):
    """Property 6. A differing reply must be treated as CHANGED -- the original
    discarded and the change propagated. Noise may cost replay; it may never
    produce an unsafe preservation."""

    def test_a_differing_reply_is_treated_as_changed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            original, _attack, flagged, _truth, _stem = _setup(tmp, "A", True)

            class Noisy:
                """Returns something different every time, on any prompt."""

                def __init__(self):
                    self.n = 0

                def generate(self, prompt, system=None, json_output=False,
                             temperature=None):
                    from src.common.llm import LLMResponse

                    self.n += 1
                    return LLMResponse(
                        text=f"noise-{self.n}", model="noisy", prompt_tokens=1,
                        output_tokens=1, thoughts_tokens=0, total_tokens=2,
                        attempts=1, latency_s=0.0, slept_s=0.0,
                        finish_reason="stop")

            events = pipeline_model_events(original)
            client = CutoffClient(original=original, inner=Noisy(),
                                  flagged=flagged)
            # Drive the first model event with a prompt that cannot match.
            event = original.event(events[0])
            client.announce(event.agent_id, event.kind)
            response = client.generate("a prompt that is not the original")
            self.assertEqual(response.text, "noise-1",
                             "a differing reply must be returned, not the "
                             "original -- the change has to propagate")
            self.assertEqual(client.changed, [events[0]])
            self.assertEqual(client.unchanged, [])


class TestTheProductionDefaultDoesNotMove(unittest.TestCase):
    """Property 7. IREC is additive and off by default."""

    def test_run_generated_defaults_to_causalline_only(self) -> None:
        from src.eval.real_llm import run_generated

        default = inspect.signature(run_generated).parameters[
            "recovery_strategies"].default
        self.assertEqual(tuple(default), ("causalline",),
                         "the default recovery strategy set must not include "
                         "IREC")

    def test_nothing_in_src_imports_irec_unconditionally(self) -> None:
        """A module-level import would make the strategy reachable without the
        flag, and would couple the production path to it."""
        import re

        offenders = []
        for path in Path("src").rglob("*.py"):
            if path.name == "irec.py":
                continue
            for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if re.match(r"^\s*(from|import)\s+.*recovery\.irec", line):
                    if not line.startswith(" "):      # module level
                        offenders.append(f"{path}:{number}")
        self.assertFalse(offenders,
                         f"IREC is imported at module level in {offenders}")


if __name__ == "__main__":
    unittest.main()
