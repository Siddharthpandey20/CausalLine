"""D-051: redundant sources are removed together, or they are not caught at all.

THE FAILURE THIS PREVENTS
-------------------------
Two sources carrying the same fact are each individually unnecessary: remove
either alone and the other still supplies it, the decision does not move, and
single-source removal reports **both** clean. Removing them together does move
it. Leave-one-out never tries that, and recursive group testing splits them
apart on the way down, so neither finds it.

The consequence is not a lost percentage point. A false clean here severs the
contamination chain: the poisoned fact reaches the next agent through a source
recovery has been told is clean, and every event downstream is preserved
unsafely. This is the estimator's half of `unsafe preservation`.

WHAT IS AND IS NOT COVERED
--------------------------
Only redundancy the trace **recorded** -- either a summary beside its own
inputs, or two summaries of an upstream source that is not itself in context.
Two unrelated sources that coincidentally state the same fact are not merged
and are not caught; see the module docstring on `derived_links_for` and
docs/06 section 2.2 for why that needs subset testing rather than a link.

    python -m unittest tests.test_derived_from_grouping -v
"""

import unittest

from src.provenance.attribution import derived_links_for
from src.provenance.group_test import (
    GroupTestDiagnostics,
    group_test,
    leave_one_out,
    merge_derived_units,
)


class _JointlyInfluential:
    """A decision that moves only when BOTH members are removed together.

    This is the redundancy case in its purest form: either source alone still
    carries the fact, so the decision only moves once neither is present.
    """

    def __init__(self, pair: tuple[str, str]) -> None:
        self.pair = set(pair)
        self.calls = 0

    def __call__(self, group) -> bool:
        self.calls += 1
        return self.pair.issubset(set(group))


class TestGroupingCatchesWhatSingleRemovalCannot(unittest.TestCase):
    """The headline test the fix exists for."""

    def test_independent_testing_misses_a_jointly_influential_pair(self) -> None:
        """Establishes the bug is real before asserting the fix.

        Without this, the test below could pass for the wrong reason.
        """
        decision = _JointlyInfluential(("S1", "S2"))
        found = leave_one_out(["S1", "S2", "S3"], decision)
        self.assertEqual(
            found,
            [],
            "leave-one-out is expected to find nothing here -- that is the "
            "blind spot D-051 is about",
        )

    def test_group_testing_alone_also_misses_it(self) -> None:
        decision = _JointlyInfluential(("S1", "S2"))
        diagnostics = GroupTestDiagnostics()
        found = group_test(["S1", "S2", "S3", "S4"], decision, diagnostics)
        self.assertEqual(found, [], "recursive halving splits the pair apart")
        self.assertGreaterEqual(
            diagnostics.interaction_suspected,
            1,
            "a group mattered but no single member did; that has to be "
            "counted, or the miss is silent",
        )

    def test_merged_into_one_unit_the_pair_is_caught(self) -> None:
        """The fix. Same decision function, same tester, linked candidates."""
        links = {"S1": ("S2",)}
        units = merge_derived_units(["S1", "S2", "S3", "S4"], links)
        self.assertIn(("S1", "S2"), units)

        by_key = {u[0]: u for u in units}
        decision = _JointlyInfluential(("S1", "S2"))

        def unit_decision(keys):
            return decision([sid for k in keys for sid in by_key[k]])

        influential_keys = group_test(
            [u[0] for u in units], unit_decision, GroupTestDiagnostics()
        )
        influential = {sid for k in influential_keys for sid in by_key[k]}
        self.assertEqual(
            influential,
            {"S1", "S2"},
            "removing the unit whole moves the decision, so both members are "
            "influential -- which is what independent testing could not see",
        )

    def test_a_unit_is_never_split_by_the_recursion(self) -> None:
        """Every group the decision sees contains all of a unit or none of it."""
        links = {"S1": ("S2",)}
        units = merge_derived_units(["S1", "S2", "S3", "S4", "S5", "S6"], links)
        by_key = {u[0]: u for u in units}
        seen: list[set[str]] = []

        def recording_decision(keys):
            group = {sid for k in keys for sid in by_key[k]}
            seen.append(group)
            return True  # force the recursion all the way down

        group_test([u[0] for u in units], recording_decision, GroupTestDiagnostics())

        self.assertTrue(seen, "the recursion made no calls")
        for group in seen:
            split = group & {"S1", "S2"}
            self.assertIn(
                split,
                ({"S1", "S2"}, set()),
                f"unit was split: group {sorted(group)} contains part of it",
            )


class TestLinksComeOnlyFromRecordedProvenance(unittest.TestCase):
    def test_direct_link_summary_beside_its_input(self) -> None:
        """A derived from event E, and B influenced E, both in context."""
        links = derived_links_for(
            ["A", "B", "C"],
            producer_of=lambda s: {"A": "E"}.get(s),
            influencers_of=lambda e: ["B"] if e == "E" else [],
        )
        self.assertEqual(links.get("A"), ("B",))
        self.assertEqual(links.get("B"), ("A",))
        self.assertNotIn("C", links)

    def test_shared_ancestor_two_summaries_of_an_absent_source(self) -> None:
        """The case that actually costs points, and has no direct link.

        The Coder sees five findings and no web pages. Each finding was
        produced by an event the poisoned page influenced, but the page itself
        is not in context, so no finding is derived from another.
        """
        producers = {"F1": "e1", "F2": "e2", "F3": "e3"}
        ancestors = {"e1": ["S4"], "e2": ["S4"], "e3": ["S9"]}
        links = derived_links_for(
            ["F1", "F2", "F3"],
            producer_of=lambda s: producers.get(s),
            influencers_of=lambda e: ancestors.get(e, []),
        )
        self.assertEqual(links.get("F1"), ("F2",))
        self.assertNotIn(
            "F3", links, "F3 shares no ancestor with the others"
        )
        units = merge_derived_units(["F1", "F2", "F3"], links)
        self.assertEqual(units, [("F1", "F2"), ("F3",)])

    def test_an_exposed_ancestor_does_not_make_siblings_redundant(self) -> None:
        """If the shared ancestor is itself in context, it is a direct link.

        Two sources both derived from a source sitting right there are each
        individually testable against it; merging them as siblings as well
        would over-merge and cost resolution for nothing.
        """
        producers = {"F1": "e1", "F2": "e2"}
        ancestors = {"e1": ["S4"], "e2": ["S4"]}
        links = derived_links_for(
            ["F1", "F2", "S4"],
            producer_of=lambda s: producers.get(s),
            influencers_of=lambda e: ancestors.get(e, []),
        )
        # Both link to S4 directly; neither links to the other as a sibling.
        self.assertEqual(links.get("F1"), ("S4",))
        self.assertEqual(links.get("F2"), ("S4",))

    def test_no_recorded_link_means_no_merge(self) -> None:
        """Coincidental redundancy is explicitly out of scope (docs/06 2.2)."""
        links = derived_links_for(
            ["A", "B"],
            producer_of=lambda s: None,
            influencers_of=lambda e: [],
        )
        self.assertEqual(links, {})
        self.assertEqual(
            merge_derived_units(["A", "B"], links), [("A",), ("B",)]
        )

    def test_shared_ancestors_can_be_switched_off(self) -> None:
        producers = {"F1": "e1", "F2": "e2"}
        ancestors = {"e1": ["S4"], "e2": ["S4"]}
        links = derived_links_for(
            ["F1", "F2"],
            producer_of=lambda s: producers.get(s),
            influencers_of=lambda e: ancestors.get(e, []),
            link_shared_ancestors=False,
        )
        self.assertEqual(links, {})


class TestUnitFormation(unittest.TestCase):
    def test_linking_is_transitive(self) -> None:
        units = merge_derived_units(
            ["A", "B", "C", "D"], {"A": ("B",), "B": ("C",)}
        )
        self.assertEqual(units, [("A", "B", "C"), ("D",)])

    def test_links_to_absent_candidates_are_ignored(self) -> None:
        """A link to something outside this context cannot merge anything."""
        self.assertEqual(
            merge_derived_units(["A", "C"], {"A": ("B",)}), [("A",), ("C",)]
        )

    def test_order_is_deterministic(self) -> None:
        """Reruns must test the same groups or call counts are incomparable."""
        first = merge_derived_units(["C", "B", "A"], {"A": ("B",)})
        second = merge_derived_units(["C", "B", "A"], {"A": ("B",)})
        self.assertEqual(first, second)
        self.assertEqual(first, [("C",), ("B", "A")])

    def test_no_links_is_one_unit_per_candidate(self) -> None:
        self.assertEqual(
            merge_derived_units(["A", "B"]), [("A",), ("B",)]
        )


if __name__ == "__main__":
    unittest.main()
