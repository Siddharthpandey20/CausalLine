"""
Phase 8 exit test: the per-channel compromise probability.

Three things are checked, and the first two are the ones that would let a
regression through silently:

  * the multi-channel product reduces **exactly** to 1-(1-pa)^m when only one
    channel is present. The whole point of Phase 8 is that the old code used
    the single-scalar form; if the new form does not contain the old one as a
    special case, one of them is wrong.
  * hand-calculated values at m=1, 5 and 20 for the indirect channel. Written
    out as literals rather than recomputed from CHANNEL_PA, so a typo in the
    constant is caught rather than propagated into the expectation.
  * exposure counts come from the same exposure-edge machinery the rest of the
    project uses, not from a private counter.

    python -m unittest tests.test_attack_model -v
"""

import unittest

from src.common.models import Event, Source
from src.risk.attack_model import (
    CHANNEL_PA,
    UNCALIBRATED_CHANNELS,
    channel_counts,
    channel_for,
    compromise_probability,
    node_risk,
    pa_for,
    residual_risk,
    trace_channel_counts,
)
from src.tracing.logger import Trace


def _trace(events, sources) -> Trace:
    return Trace(events=list(events), sources=list(sources))


class TestSingleChannelReduction(unittest.TestCase):
    """P = 1 - PROD (1-pa_i)^m_i must equal 1-(1-pa)^m with one channel."""

    def test_reduces_to_the_scalar_form(self) -> None:
        pa = CHANNEL_PA["indirect_injection"]
        for m in range(0, 25):
            with self.subTest(m=m):
                self.assertAlmostEqual(
                    compromise_probability({"indirect_injection": m}),
                    1 - (1 - pa) ** m,
                    places=12,
                )

    def test_reduces_for_the_direct_channel_too(self) -> None:
        pa = CHANNEL_PA["direct_injection"]
        for m in (1, 3, 11):
            self.assertAlmostEqual(
                compromise_probability({"direct_injection": m}),
                1 - (1 - pa) ** m,
                places=12,
            )

    def test_no_exposures_is_zero(self) -> None:
        self.assertEqual(compromise_probability({}), 0.0)
        self.assertEqual(compromise_probability({"indirect_injection": 0}), 0.0)


class TestHandCalculatedCases(unittest.TestCase):
    """Literals, worked by hand from pa = 0.271 (Zou et al. 2025)."""

    def test_m_equals_one(self) -> None:
        # 1 - 0.729^1 = 0.271
        self.assertAlmostEqual(
            compromise_probability({"indirect_injection": 1}), 0.271, places=10
        )

    def test_m_equals_five(self) -> None:
        # 0.729^5 = 0.20589113... -> 1 - that = 0.79410886...
        self.assertAlmostEqual(
            compromise_probability({"indirect_injection": 5}),
            0.7941088679053511,
            places=10,
        )

    def test_m_equals_twenty(self) -> None:
        # 0.729^20 = 1.79701029...e-3 -> 1 - that = 0.99820298...
        self.assertAlmostEqual(
            compromise_probability({"indirect_injection": 20}),
            0.9982029897000856,
            places=10,
        )

    def test_mixed_channels_multiply(self) -> None:
        # Two indirect and three direct: 1 - 0.729^2 * 0.943^3
        expected = 1 - (0.729 ** 2) * (0.943 ** 3)
        self.assertAlmostEqual(
            compromise_probability(
                {"indirect_injection": 2, "direct_injection": 3}
            ),
            expected,
            places=10,
        )

    def test_mixing_is_not_the_same_as_one_scalar(self) -> None:
        """The 4.8x spread the old single-scalar model hid."""
        mixed = compromise_probability(
            {"indirect_injection": 2, "direct_injection": 3}
        )
        as_if_all_indirect = compromise_probability({"indirect_injection": 5})
        as_if_all_direct = compromise_probability({"direct_injection": 5})
        self.assertLess(mixed, as_if_all_indirect)
        self.assertGreater(mixed, as_if_all_direct)


class TestChannelMapping(unittest.TestCase):
    def test_published_channels_are_calibrated_and_the_others_are_not(self) -> None:
        self.assertEqual(pa_for("indirect_injection"), 0.271)
        self.assertEqual(pa_for("direct_injection"), 0.057)
        for channel in UNCALIBRATED_CHANNELS:
            self.assertNotIn(channel, CHANNEL_PA)
            self.assertEqual(
                pa_for(channel),
                CHANNEL_PA["indirect_injection"],
                "an uncalibrated channel must default to the conservative rate",
            )

    def test_source_kinds_map_to_the_expected_channels(self) -> None:
        self.assertEqual(channel_for("web"), "indirect_injection")
        self.assertEqual(channel_for("user_input"), "direct_injection")
        self.assertEqual(channel_for("agent_message"), "inter_agent_message")
        self.assertEqual(channel_for("memory"), "memory_write")


class TestCountsComeFromExposureEdges(unittest.TestCase):
    def setUp(self) -> None:
        self.trace = _trace(
            events=[
                Event(id="e0001", agent_id="a", kind="agent_output",
                      exposures=["S1", "S2", "S3"]),
                Event(id="e0002", agent_id="b", kind="agent_output",
                      exposures=["S2"]),
                Event(id="e0003", agent_id="b", kind="tool_call", exposures=[]),
            ],
            sources=[
                Source(id="S1", kind="web", content="page"),
                Source(id="S2", kind="user_input", content="task"),
                Source(id="S3", kind="database", content="row"),
            ],
        )

    def test_per_event_counts(self) -> None:
        self.assertEqual(
            channel_counts(self.trace, "e0001"),
            {"indirect_injection": 2, "direct_injection": 1},
        )
        self.assertEqual(
            channel_counts(self.trace, "e0002"), {"direct_injection": 1}
        )
        self.assertEqual(channel_counts(self.trace, "e0003"), {})

    def test_whole_trace_counts_every_exposure_edge(self) -> None:
        # Four edges: (S1,e1) (S2,e1) (S3,e1) (S2,e2).
        self.assertEqual(
            trace_channel_counts(self.trace),
            {"indirect_injection": 2, "direct_injection": 2},
        )

    def test_node_risk_matches_the_hand_calculation(self) -> None:
        risk = node_risk(self.trace, "e0001")
        self.assertAlmostEqual(
            risk.probability, 1 - (0.729 ** 2) * 0.943, places=10
        )
        self.assertEqual(risk.exposures, 3)
        self.assertFalse(risk.uses_uncalibrated_channel)

    def test_event_with_no_exposures_has_zero_risk(self) -> None:
        self.assertEqual(node_risk(self.trace, "e0003").probability, 0.0)

    def test_residual_risk_over_preserved_events(self) -> None:
        p1 = node_risk(self.trace, "e0001").probability
        p2 = node_risk(self.trace, "e0002").probability
        self.assertAlmostEqual(
            residual_risk(self.trace, ["e0001", "e0002"]),
            1 - (1 - p1) * (1 - p2),
            places=10,
        )
        self.assertEqual(residual_risk(self.trace, []), 0.0)
        self.assertEqual(residual_risk(self.trace, ["e0003"]), 0.0)


if __name__ == "__main__":
    unittest.main()
