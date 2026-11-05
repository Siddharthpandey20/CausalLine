"""The seven industry architectures: structurally valid, and actually distinct.

The campaign's claim is that recovery generalises across DIFFERENT workflow
architectures. That claim is empty if the seven are relabellings of one shape,
so the structural distinctness is asserted here rather than assumed.

    python -m unittest tests.test_architectures -v

Free: no model, no key, no network.
"""

from __future__ import annotations

import unittest

from src.tracing.archspec import PROVIDERS, SHARES, assign
from src.tracing.architectures import _BUILDERS, architecture, names


class TestEveryArchitectureIsRunnable(unittest.TestCase):
    def test_no_architecture_has_problems(self) -> None:
        for key in sorted(_BUILDERS):
            with self.subTest(architecture=key):
                spec = architecture(key)
                self.assertEqual(spec.problems(), [],
                                 f"{spec.name} is not runnable")

    def test_the_agent_floor_is_met(self) -> None:
        """At least 55 distinct logical agents, per the campaign design."""
        for key in sorted(_BUILDERS):
            with self.subTest(architecture=key):
                spec = architecture(key)
                self.assertGreaterEqual(spec.agent_count, 55)
                self.assertLessEqual(spec.agent_count, 80)

    def test_agent_ids_are_unique_and_roles_are_meaningful(self) -> None:
        """Guards against inflating the count with dummy agents: every agent
        needs a distinct id and a role description that says something."""
        for key in sorted(_BUILDERS):
            spec = architecture(key)
            ids = [a.id for a in spec.agents]
            self.assertEqual(len(ids), len(set(ids)), f"{spec.name}: duplicate ids")
            for agent in spec.agents:
                with self.subTest(architecture=key, agent=agent.id):
                    self.assertGreaterEqual(
                        len(agent.role.split()), 3,
                        f"{agent.id}'s role is not a described responsibility")
                    self.assertIn(agent.provider, PROVIDERS)

    def test_every_agent_is_reachable_and_does_work(self) -> None:
        """An agent configured but never executed must not be counted. Every
        agent appears in `order`, so every agent runs; and every agent except
        the roots consumes something."""
        for key in sorted(_BUILDERS):
            spec = architecture(key)
            self.assertEqual(set(spec.order), {a.id for a in spec.agents})
            roots = [a for a in spec.agents
                     if not a.reads_from and not a.reads_docs
                     and not a.reads_memory]
            with self.subTest(architecture=key):
                # A root is an intake agent. More than a handful means the
                # graph is a pile of independent agents, which the campaign
                # design forbids.
                self.assertLessEqual(
                    len(roots), 3,
                    f"{spec.name} has {len(roots)} agents with no inputs")

    def test_dependencies_are_real_not_a_flat_fan_in(self) -> None:
        """'60 independent agents sending outputs to one manager' is excluded
        by requiring depth, and every non-root agent to consume something.

        The edge count is NOT required to exceed the agent count: a pure
        hand-off chain has exactly one edge per non-root agent, which is the
        correct shape for architecture D and not a defect. D-handoff-network
        tripped an earlier version of this assertion for that reason.
        """
        for key in sorted(_BUILDERS):
            spec = architecture(key)
            with self.subTest(architecture=key):
                self.assertGreaterEqual(spec.depth(), 4,
                                        f"{spec.name} is too shallow")
                # The property that matters: nearly every agent consumes a
                # named upstream agent's output. A flat fan-in to one manager
                # would still satisfy this, which is why `depth` above is
                # asserted too -- together they exclude both failure shapes.
                consuming = sum(1 for a in spec.agents if a.reads_from)
                self.assertGreaterEqual(
                    consuming, spec.agent_count * 0.8,
                    f"{spec.name}: only {consuming}/{spec.agent_count} agents "
                    f"have an upstream dependency")


class TestTheSevenAreActuallyDistinct(unittest.TestCase):
    """The campaign's generalisation claim rests on this."""

    def _fingerprint(self, spec) -> tuple:
        fan_out, fan_in = spec.fan()
        return (spec.depth(), fan_out, fan_in,
                len(spec.shared_topic) > 0,
                # bucketed so a one-edge difference is not called distinct
                spec.cross_branch_edges() // 20)

    def test_no_two_share_a_structural_fingerprint(self) -> None:
        seen: dict[tuple, str] = {}
        for key in sorted(_BUILDERS):
            spec = architecture(key)
            print_key = self._fingerprint(spec)
            if print_key in seen:
                self.fail(f"{spec.name} and {seen[print_key]} have the same "
                          f"structural fingerprint {print_key}: the set would "
                          f"be variations of one shape")
            seen[print_key] = spec.name

    def test_the_set_spans_deep_narrow_and_wide_shallow(self) -> None:
        depths = {k: architecture(k).depth() for k in _BUILDERS}
        fanouts = {k: architecture(k).fan()[0] for k in _BUILDERS}
        self.assertGreaterEqual(max(depths.values()), 15,
                                "no deep delegation chain in the set")
        self.assertLessEqual(min(depths.values()), 8,
                             "no shallow architecture in the set")
        self.assertGreaterEqual(max(fanouts.values()), 8,
                                "no wide fan-out in the set")
        self.assertLessEqual(min(fanouts.values()), 2,
                             "no narrow hand-off chain in the set")

    def test_each_architecture_cites_a_source(self) -> None:
        for key in sorted(_BUILDERS):
            spec = architecture(key)
            with self.subTest(architecture=key):
                self.assertRegex(spec.source_id, r"^S[1-8]$")
                self.assertTrue(spec.source_url.startswith("https://"))
                self.assertTrue(spec.evidence,
                                "the kind of evidence must be recorded")
                self.assertTrue(spec.notes,
                                "where the source is silent must be recorded")

    def test_no_architecture_claims_production_without_grounds(self) -> None:
        """Only S1 describes a system its authors run in production."""
        for key in sorted(_BUILDERS):
            spec = architecture(key)
            if "production" in spec.evidence and spec.source_id != "S1":
                self.assertIn(
                    "no named customer", spec.evidence,
                    f"{spec.name} claims production on source {spec.source_id} "
                    f"without the qualification its source requires")


class TestProviderAssignment(unittest.TestCase):
    def test_it_is_deterministic_in_the_index(self) -> None:
        """The same logical agent must get the same provider in every trial,
        or trials of one architecture are not comparable."""
        for index in range(40):
            self.assertEqual(assign(index), assign(index))
            self.assertEqual(assign(index, heavy=True),
                             assign(index, heavy=True))

    def test_the_default_shares_are_the_stated_target(self) -> None:
        picks = [assign(i) for i in range(2000)]
        for provider, want in (("local", 0.45), ("gemini", 0.40),
                               ("nvidia", 0.15)):
            share = picks.count(provider) / len(picks)
            self.assertAlmostEqual(share, want, delta=0.02,
                                   msg=f"{provider} share is {share:.0%}")

    def test_gemini_does_meaningful_work_in_every_architecture(self) -> None:
        """Not 'nearly everything to Ollama': every architecture must give the
        hosted providers a real share of its calls."""
        for key in sorted(_BUILDERS):
            spec = architecture(key)
            mix = spec.provider_mix()
            total = sum(mix.values())
            with self.subTest(architecture=key):
                self.assertGreaterEqual(
                    mix["gemini"] / total, 0.20,
                    f"{spec.name} routes only {mix['gemini']}/{total} to Gemini")
                self.assertGreaterEqual(
                    mix["local"] / total, 0.20,
                    f"{spec.name} routes only {mix['local']}/{total} locally")

    def test_a_local_only_share_is_available_for_offline_rehearsal(self) -> None:
        picks = {assign(i, shares=SHARES["local-only"]) for i in range(50)}
        self.assertEqual(picks, {"local"})

    def test_a_local_heavy_share_exists_for_a_constrained_quota(self) -> None:
        """The campaign is quota-bound, not cost-bound. This share is what a
        free-tier Gemini project can actually fund."""
        picks = [assign(i, shares=SHARES["local-heavy"]) for i in range(2000)]
        self.assertGreater(picks.count("local") / len(picks), 0.7)
        self.assertGreater(picks.count("gemini"), 0,
                           "even the constrained share must keep Gemini real")


class TestTheArchitectureRegistry(unittest.TestCase):
    def test_seven_architectures_are_registered(self) -> None:
        self.assertEqual(len(_BUILDERS), 7)
        self.assertEqual(len(set(names())), 7)

    def test_lookup_by_letter_and_by_name(self) -> None:
        for key in sorted(_BUILDERS):
            spec = architecture(key)
            self.assertIs(architecture(spec.name), spec)

    def test_an_unknown_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            architecture("Z-nonexistent")

    def test_the_header_dict_round_trips_the_shape(self) -> None:
        """`replay()` rebuilds the shape from the trace header, so the header
        has to carry enough to identify it."""
        for key in sorted(_BUILDERS):
            spec = architecture(key)
            data = spec.to_dict()
            with self.subTest(architecture=key):
                self.assertEqual(data["agents"], spec.agent_count)
                self.assertEqual(data["order"], list(spec.order))
                self.assertEqual(data["source_id"], spec.source_id)


if __name__ == "__main__":
    unittest.main()
