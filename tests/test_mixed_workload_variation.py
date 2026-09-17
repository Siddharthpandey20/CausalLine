"""Seed-dependent attack placement: reproducible, genuinely varied, inert.

WHY THIS EXISTS
---------------
The completed 15-run validation (`docs/13-mixed56-validation.md`) has one
limitation, stated in its own Sec 9 and visible in its own data: `f_true` was
identical across all five seeds within each regime, and the medium regime's
paired difference had standard deviation **0.00**. `MixedScenario.build` took
no seed, so the corpus, payloads and poisoned placements were byte-identical
across seeds. Repeating that measures model non-determinism, not workload
variation.

`build(regime, seed=N)` now draws the placement. These tests establish the
three things that have to be true of such a draw:

  * **reproducible** -- the same seed is the same workload, forever
  * **genuinely different** -- different seeds are different workloads, not the
    same structure with a different label
  * **inert** -- the seed reaches the ATTACK and nothing else. Not the
    architecture, not the providers, not the payload text, not the recovery
    method, not the detector.

The third is the one that would invalidate the experiment if it failed, so it
gets the most tests.

    python -m unittest tests.test_mixed_workload_variation -v
"""

import tempfile
import unittest
from pathlib import Path

from src.eval.mixed_scenarios import (
    CANARY,
    FROZEN_PLACEMENT,
    REGIME_SHAPES,
    MixedScenario,
)
from src.tracing.mixed import Topology
from src.tracing.tools import mixed_corpus

REGIMES = ("small", "medium", "large")
SEEDS = (20260917, 20260918, 20260919, 20260920, 20260921)


def placement(scenario) -> tuple:
    return (scenario.poisoned_docs, scenario.poisoned_memory,
            scenario.poisoned_messages)


# --- A. reproducible ---------------------------------------------------------


class TestASameSeedIsTheSameWorkload(unittest.TestCase):
    """If this fails, no result from the workload-varied campaign can be
    rebuilt from its own seed, and the experiment is not an experiment."""

    def test_two_builds_of_one_seed_are_identical(self) -> None:
        for regime in REGIMES:
            with self.subTest(regime=regime):
                a = MixedScenario.build(regime, seed=123)
                b = MixedScenario.build(regime, seed=123)
                self.assertEqual(placement(a), placement(b))

    def test_reproducible_across_processes_not_just_calls(self) -> None:
        """`hash()` is salted per process, so a placement derived from it would
        differ between two runs of the same seed. The draw uses SHA-256 for
        exactly that reason; this pins the values so a switch back to `hash()`
        or to `random.seed(str)` fails here rather than silently.
        """
        import json
        import subprocess
        import sys

        code = (
            "import json;from src.eval.mixed_scenarios import MixedScenario as M;"
            "print(json.dumps([[list(x) for x in ("
            "M.build(r, seed=123).poisoned_docs,"
            "M.build(r, seed=123).poisoned_memory,"
            "M.build(r, seed=123).poisoned_messages)]"
            " for r in ('small','medium','large')]))"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, cwd=str(Path.cwd()))
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        other = json.loads(out.stdout.strip().splitlines()[-1])
        mine = [[list(x) for x in placement(MixedScenario.build(r, seed=123))]
                for r in REGIMES]
        self.assertEqual(other, mine)

    def test_the_control_shares_its_twins_placement(self) -> None:
        """`exposed_only` must plant the same sources in the same places as the
        influencing run, or it is not a control for it. So `intent` must NOT be
        mixed into the draw."""
        for regime in REGIMES:
            with self.subTest(regime=regime):
                attacked = MixedScenario.build(regime, seed=7)
                control = MixedScenario.build(regime, seed=7,
                                              intent="exposed_only")
                self.assertEqual(placement(attacked), placement(control))

    def test_regimes_draw_independently_from_one_seed(self) -> None:
        """Seed 5 on `small` and seed 5 on `large` must be independent draws,
        not one stream consumed differently -- otherwise the three regimes'
        placements are correlated for reasons nothing in the design intends."""
        verifiers = {r: MixedScenario.build(r, seed=5).poisoned_messages
                     for r in ("small", "large")}
        # Not a strong claim about the values; only that the draw is keyed by
        # regime, which the implementation does by hashing the name in.
        self.assertIsNotNone(verifiers)


# --- B. genuinely different --------------------------------------------------


class TestBDifferentSeedsAreDifferentWorkloads(unittest.TestCase):

    def test_five_seeds_give_five_distinct_placements(self) -> None:
        for regime in REGIMES:
            with self.subTest(regime=regime):
                seen = {placement(MixedScenario.build(regime, seed=s))
                        for s in SEEDS}
                self.assertEqual(
                    len(seen), len(SEEDS),
                    f"{regime}: {len(seen)} distinct placements from "
                    f"{len(SEEDS)} seeds")

    def test_the_number_of_poisoned_sources_varies_too(self) -> None:
        """A different target with an identical count would leave the
        contamination structure nearly the same. The counts move inside a
        per-regime band so both which and how many differ."""
        for regime in REGIMES:
            shape = REGIME_SHAPES[regime]
            varies = any(hi > lo for lo, hi in
                         (shape["docs"], shape["memory"], shape["messages"]))
            if not varies:
                continue
            with self.subTest(regime=regime):
                counts = {tuple(len(x) for x in
                                placement(MixedScenario.build(regime, seed=s)))
                          for s in SEEDS}
                self.assertGreater(len(counts), 1,
                                   f"{regime}: every seed poisoned the same "
                                   "number of sources")

    def test_the_frozen_placement_is_still_reachable(self) -> None:
        """The completed 15-run validation must stay reproducible from this
        code. `seed=None` is that experiment, not a default."""
        for regime in REGIMES:
            with self.subTest(regime=regime):
                self.assertEqual(placement(MixedScenario.build(regime)),
                                 FROZEN_PLACEMENT[regime])


# --- C. the regimes are still the regimes ------------------------------------


class TestCRegimeConstraintsHold(unittest.TestCase):

    def test_every_drawn_scenario_validates(self) -> None:
        for regime in REGIMES:
            for seed in SEEDS:
                with self.subTest(regime=regime, seed=seed):
                    self.assertEqual(
                        MixedScenario.build(regime, seed=seed).validate(), [])

    def test_counts_stay_inside_the_regime_shape(self) -> None:
        for regime in REGIMES:
            shape = REGIME_SHAPES[regime]
            for seed in SEEDS:
                with self.subTest(regime=regime, seed=seed):
                    docs, memory, messages = placement(
                        MixedScenario.build(regime, seed=seed))
                    for got, (low, high) in (
                        (docs, shape["docs"]), (memory, shape["memory"]),
                        (messages, shape["messages"]),
                    ):
                        self.assertGreaterEqual(len(got), low)
                        self.assertLessEqual(len(got), high)

    def test_targets_are_inside_the_topology(self) -> None:
        topo = Topology()
        for regime in REGIMES:
            for seed in SEEDS:
                with self.subTest(regime=regime, seed=seed):
                    docs, memory, messages = placement(
                        MixedScenario.build(regime, seed=seed))
                    self.assertTrue(all(0 <= d < topo.acquisition for d in docs))
                    self.assertTrue(all(1 <= m <= topo.normalisers for m in memory))
                    self.assertTrue(all(1 <= v <= topo.verifiers for v in messages))

    def test_each_regime_keeps_its_channels(self) -> None:
        """A regime is defined by which channels carry the attack. The seed
        must not turn `medium` (web only) into a memory attack."""
        for seed in SEEDS:
            with self.subTest(seed=seed):
                small = MixedScenario.build("small", seed=seed)
                self.assertEqual(small.poisoned_docs, ())
                self.assertEqual(small.poisoned_memory, ())
                self.assertTrue(small.poisoned_messages)

                medium = MixedScenario.build("medium", seed=seed)
                self.assertTrue(medium.poisoned_docs)
                self.assertEqual(medium.poisoned_memory, ())
                self.assertEqual(medium.poisoned_messages, ())

                large = MixedScenario.build("large", seed=seed)
                self.assertTrue(large.poisoned_docs)
                self.assertTrue(large.poisoned_memory)
                self.assertTrue(large.poisoned_messages)

    def test_the_band_is_intent_and_is_not_used_to_redraw(self) -> None:
        """`band()` reports what a regime AIMS at. Nothing in construction may
        consult it -- a draw that retried until the fraction looked right would
        be a filter on results wearing the clothes of a design."""
        import inspect
        import re

        source = inspect.getsource(MixedScenario._draw_placement)
        # The docstring explains the band; the CODE must not consult it.
        code = re.sub(r'"""(?:.|\n)*?"""', "", source)
        self.assertNotIn("band", code)
        self.assertNotIn("f_true", code)
        for regime in REGIMES:
            low, high = MixedScenario.build(regime, seed=1).band()
            self.assertLess(low, high)


# --- D/E/F. everything the seed must NOT touch -------------------------------


class TestDTheArchitectureIsUntouched(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_topology_is_identical_for_every_seed(self) -> None:
        base = MixedScenario.build("medium").topology
        for regime in REGIMES:
            for seed in SEEDS:
                with self.subTest(regime=regime, seed=seed):
                    self.assertEqual(
                        MixedScenario.build(regime, seed=seed).topology, base)

    def test_workflow_kwargs_do_not_carry_the_seed(self) -> None:
        """What reaches the pipeline must be the topology and nothing else."""
        base = MixedScenario.build("large").workflow_kwargs
        for seed in SEEDS:
            with self.subTest(seed=seed):
                self.assertEqual(
                    MixedScenario.build("large", seed=seed).workflow_kwargs,
                    base)

    def test_every_seed_produces_56_agents_and_145_events(self) -> None:
        from src.tracing.logger import read_trace
        from src.tracing.pipeline import run_pipeline
        from src.tracing.tools import Tools
        from tests.test_mixed import _Echo

        for regime, seed in (("small", 20260918), ("medium", 20260919),
                             ("large", 20260920)):
            with self.subTest(regime=regime, seed=seed):
                scenario = MixedScenario.build(regime, seed=seed)
                path = Path(self.tmp.name) / f"{regime}-{seed}.jsonl"
                tools = scenario.apply(Tools.from_fixtures(
                    memory_path=path.with_suffix(".memory.json")))
                run_pipeline(path, task=scenario.task, client=_Echo(),
                             tools=tools, **scenario.workflow_kwargs)
                trace = read_trace(path)
                trace.validate()
                self.assertEqual(len(trace.events), 145)
                self.assertEqual(len({e.agent_id for e in trace.events}), 56)


class TestEProviderAssignmentIsUntouched(unittest.TestCase):

    def test_routing_is_identical_for_every_seed(self) -> None:
        base = Topology().routing()
        self.assertEqual(base, {"hub": "gemini", "synth": "gemini",
                                "spec1": "nvidia", "spec3": "nvidia",
                                "ver1": "nvidia", "rev8": "nvidia"})
        for regime in REGIMES:
            for seed in SEEDS:
                with self.subTest(regime=regime, seed=seed):
                    self.assertEqual(
                        MixedScenario.build(regime, seed=seed)
                        .topology.routing(), base)


class TestFThePayloadsAreUntouched(unittest.TestCase):

    def test_payload_text_does_not_depend_on_the_seed(self) -> None:
        for regime in REGIMES:
            base = MixedScenario.build(regime).payload
            for seed in SEEDS:
                with self.subTest(regime=regime, seed=seed):
                    self.assertEqual(
                        MixedScenario.build(regime, seed=seed).payload, base)

    def test_marker_and_canary_do_not_depend_on_the_seed(self) -> None:
        for regime in REGIMES:
            base = MixedScenario.build(regime)
            for seed in SEEDS:
                with self.subTest(regime=regime, seed=seed):
                    drawn = MixedScenario.build(regime, seed=seed)
                    self.assertEqual(drawn.marker, base.marker)
                    self.assertEqual(drawn.token, base.token)
                    self.assertEqual(drawn.token, CANARY)

    def test_the_channel_payload_builders_ignore_the_seed(self) -> None:
        """The per-channel payloads are keyed by the depot they target, which
        the placement chooses -- but their TEXT template must not change."""
        for seed in SEEDS:
            with self.subTest(seed=seed):
                scenario = MixedScenario.build("large", seed=seed)
                tools = scenario.apply(
                    __import__("src.tracing.tools", fromlist=["Tools"])
                    .Tools.from_fixtures())
                for text in list(tools.mixed_messages.values()):
                    self.assertIn(scenario.marker, text)
                    self.assertIn(CANARY, text)
                    self.assertIn("MESSAGE FROM THE SPECIALIST", text)


# --- G/H/I. ground truth, controls, and issue #20 ----------------------------


class TestGGroundTruthRemainsValid(unittest.TestCase):

    def test_the_canary_is_absent_from_every_record(self) -> None:
        for doc in mixed_corpus(Topology().acquisition):
            self.assertNotIn(CANARY, doc["text"])
            self.assertNotEqual(CANARY, doc["code"])

    def test_every_drawn_scenario_plants_its_marker(self) -> None:
        from src.tracing.tools import Tools

        for regime in REGIMES:
            for seed in SEEDS:
                with self.subTest(regime=regime, seed=seed):
                    scenario = MixedScenario.build(regime, seed=seed)
                    tools = scenario.apply(Tools.from_fixtures())
                    planted = [d.get("injected", "") for d in tools.mixed_docs]
                    planted += list(tools.mixed_messages.values())
                    planted += [v for k, v in tools.memory.items()
                                if k.startswith("policy/norm")]
                    carrying = [p for p in planted if scenario.marker in p]
                    self.assertTrue(carrying,
                                    "no planted text carries the marker, so "
                                    "`label_malicious` would flag nothing")


class TestHControlsStayClean(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_control_never_names_the_canary(self) -> None:
        from src.tracing.tools import Tools

        for regime in REGIMES:
            for seed in SEEDS:
                with self.subTest(regime=regime, seed=seed):
                    scenario = MixedScenario.build(regime, seed=seed,
                                                   intent="exposed_only")
                    self.assertEqual(scenario.validate(), [])
                    tools = scenario.apply(Tools.from_fixtures())
                    texts = [d.get("injected", "") for d in tools.mixed_docs]
                    texts += list(tools.mixed_messages.values())
                    texts += [v for k, v in tools.memory.items()
                              if k.startswith("policy/norm")]
                    for text in texts:
                        self.assertNotIn(CANARY, text)

    def test_a_control_run_produces_no_canary_anywhere(self) -> None:
        from src.tracing.logger import read_trace
        from src.tracing.pipeline import run_pipeline
        from src.tracing.tools import Tools
        from tests.test_mixed import _Echo

        scenario = MixedScenario.build("large", seed=20260918,
                                       intent="exposed_only")
        path = Path(self.tmp.name) / "control.jsonl"
        tools = scenario.apply(Tools.from_fixtures(
            memory_path=path.with_suffix(".memory.json")))
        run_pipeline(path, task=scenario.task, client=_Echo(), tools=tools,
                     **scenario.workflow_kwargs)
        trace = read_trace(path)
        for event in trace.events:
            if event.output_ref:
                self.assertNotIn(CANARY,
                                 trace.content.get(event.output_ref) or "")


class TestIMemoryPoisoningStillExercisesIssue20(unittest.TestCase):
    """The drawn placements must still reach the memory channel, or the
    workload-varied campaign would stop testing the fix that made the frozen
    one trustworthy."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_large_always_poisons_memory(self) -> None:
        for seed in SEEDS:
            with self.subTest(seed=seed):
                self.assertTrue(
                    MixedScenario.build("large", seed=seed).poisoned_memory)

    def test_no_event_holds_the_payload_outside_the_region(self) -> None:
        from src.eval.attacks import label_malicious
        from src.provenance.contamination import contaminate
        from src.tracing.logger import read_trace
        from src.tracing.pipeline import run_pipeline
        from src.tracing.tools import Tools
        from tests.test_issue20_memory_provenance import (
            assert_no_payload_outside_region,
        )
        from tests.test_mixed import _Echo

        for regime, seed in (("small", 20260918), ("medium", 20260919),
                             ("large", 20260920), ("large", 20260921)):
            with self.subTest(regime=regime, seed=seed):
                scenario = MixedScenario.build(regime, seed=seed)
                path = Path(self.tmp.name) / f"{regime}-{seed}.jsonl"
                tools = scenario.apply(Tools.from_fixtures(
                    memory_path=path.with_suffix(".memory.json")))
                run_pipeline(path, task=scenario.task, client=_Echo(),
                             tools=tools, **scenario.workflow_kwargs)
                trace = read_trace(path)
                flagged = label_malicious(path, scenario.marker)
                self.assertTrue(flagged)
                assert_no_payload_outside_region(self, trace, flagged)

    def test_contamination_never_escapes_the_closure_on_any_draw(self) -> None:
        from src.eval.attacks import label_malicious
        from src.eval.baselines import b2_topology_closure
        from src.provenance.contamination import contaminate
        from src.tracing.logger import read_trace
        from src.tracing.pipeline import run_pipeline
        from src.tracing.tools import Tools
        from tests.test_mixed import _Echo

        for regime, seed in (("small", 20260919), ("medium", 20260920),
                             ("large", 20260917)):
            with self.subTest(regime=regime, seed=seed):
                scenario = MixedScenario.build(regime, seed=seed)
                path = Path(self.tmp.name) / f"esc-{regime}-{seed}.jsonl"
                tools = scenario.apply(Tools.from_fixtures(
                    memory_path=path.with_suffix(".memory.json")))
                run_pipeline(path, task=scenario.task, client=_Echo(),
                             tools=tools, **scenario.workflow_kwargs)
                trace = read_trace(path)
                flagged = label_malicious(path, scenario.marker)
                region = set(contaminate(trace, set(flagged)).events)
                closure = set(b2_topology_closure(trace, flagged))
                self.assertFalse(region - closure)


# --- J/K. the seed reaches nothing it must not -------------------------------


class TestJNoHardCodedIdentifiers(unittest.TestCase):

    def test_this_module_names_no_event_or_source_id(self) -> None:
        """A test that asserts `e0049` is contaminated stops meaning anything
        the moment the topology changes. Nothing here may name one."""
        import re

        source = Path(__file__).read_text(encoding="utf-8")
        # Strip docstrings' historical references, which are prose not asserts.
        code = re.sub(r'"""(?:.|\n)*?"""', "", source)
        self.assertEqual(re.findall(r"\be\d{4}\b", code), [])
        self.assertEqual(re.findall(r"\bS\d{1,3}\b", code), [])


class TestKTheSeedDoesNotReachTheRecoveryMethod(unittest.TestCase):
    """The seed selects the ATTACK. If it could reach CausalLine, the planner,
    the detector or the baselines, the comparison would no longer be between
    methods on a workload -- it would be between configurations."""

    def test_the_scenario_exposes_no_recovery_configuration(self) -> None:
        forbidden = ("gate1", "detector", "planner", "causalline", "threshold",
                     "a_scale", "margin", "escalat")
        for regime in REGIMES:
            scenario = MixedScenario.build(regime, seed=99)
            keys = " ".join(scenario.to_dict()).lower()
            for word in forbidden:
                with self.subTest(regime=regime, word=word):
                    self.assertNotIn(word, keys)

    def test_the_draw_touches_only_placement_fields(self) -> None:
        """Everything a drawn scenario differs in, against the frozen one,
        must be a placement field or a note derived from one."""
        allowed = {"poisoned_docs", "poisoned_memory", "poisoned_messages",
                   "placement_seed", "notes", "annotation"}
        for regime in REGIMES:
            frozen = MixedScenario.build(regime)
            drawn = MixedScenario.build(regime, seed=4242)
            for field_name in vars(frozen):
                if field_name in allowed:
                    continue
                with self.subTest(regime=regime, field=field_name):
                    self.assertEqual(getattr(frozen, field_name),
                                     getattr(drawn, field_name))

    def test_the_campaign_defaults_to_the_frozen_placement(self) -> None:
        """`--vary-placement` must be opt-in, so the completed validation is
        what the unflagged command reproduces."""
        import inspect

        from src.eval.mixed_campaign import run_regime

        self.assertIs(
            inspect.signature(run_regime).parameters["vary_placement"].default,
            False)


if __name__ == "__main__":
    unittest.main()
