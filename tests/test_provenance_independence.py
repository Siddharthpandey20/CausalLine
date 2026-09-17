"""Execution-time provenance must not depend on whether attribution runs.

WHAT THIS FILE ESTABLISHES, AND WHAT IT CORRECTS
--------------------------------------------------
`docs/gate1/final_cost_direction.md` §6.1 reported that the pipeline produces
different provenance depending on whether an attributor is installed, citing
"25 structural records vs 18". **That measurement was a misdiagnosis**, and
these tests are what found it.

`method="structural"` is worn by two different kinds of record:

  * **structural** -- read off the code path by `record_structural()`. A tool
    call's argument, a tool response's value, the Executor's comparison. These
    are execution facts.
  * **carrier** -- written by `record_carrier()`, which is a *pointer to an
    upstream verdict*, not a verdict of its own (D-067). It legitimately depends
    on what attribution has concluded, and `src/provenance/carriers.py` already
    re-resolves it at read time for exactly that reason.

Counting them together made attribution-derived records look like execution
facts. Split apart, the execution facts are identical and the carrier records
are not -- which is the designed behaviour, not a defect.

These tests pin the real contract so the conflation cannot recur.

    python -m unittest tests.test_provenance_independence -v
"""

import tempfile
import unittest
from pathlib import Path

from src.provenance.attribution import CARRIER_NOTE
from src.tracing.logger import read_trace

# --- what counts as an execution fact ----------------------------------------
#
# Anything here must be byte-identical between a run with attribution and a run
# without it. Anything NOT here is permitted to differ, because it is inference.

EXECUTION_FACT_FIELDS = (
    "event ids", "event kinds", "agent ids", "parents", "exposures",
    "tool ids",
    "source ids", "source kinds", "derived_from", "origin_event",
    "structural check records",
)

# WHY THE STORED PROMPT/OUTPUT CONTENT IS NOT IN THE LIST ABOVE
# -------------------------------------------------------------
# It differs between the two runs, and the reason is the TESTBED, not the
# pipeline. `ScriptedClient._answer_self_report` answers truthfully by looking
# the audited call up in its own `by_prompt` table -- so the attributor must be
# handed the SAME client instance or it answers "I used nothing" about
# everything. Sharing the instance means the attributor's calls advance that
# client's state, which changes what later pipeline calls return.
#
# Measured: giving the attributor its own client made input refs match, but
# made the scripted self-report fabricate, and recovery degraded systematically
# across 5 seeds (work preserved 0.789 -> 0.737, pair false negatives 0 -> 1)
# even with the simulated error rates set to zero.
#
# A real model shares an endpoint, not a state table, so this coupling does not
# exist in production. `TestTheContentDifferenceIsATestbedArtefact` pins the
# cause so this is never re-read as a provenance defect -- which is exactly the
# mistake `docs/gate1/final_cost_direction.md` §6.1 made.
CONTENT_FIELDS_EXCLUDED_BECAUSE_OF_THE_SHARED_SCRIPTED_CLIENT = (
    "input refs", "output refs",
)

ATTRIBUTION_DERIVED = (
    "influence edges", "self-report checks", "counterfactual checks",
    "carrier check records", "assumed check records",
)


def execution_facts(trace) -> dict:
    """Everything the pipeline observes, and nothing it infers."""
    return {
        "event ids": [e.id for e in trace.events],
        "event kinds": [e.kind for e in trace.events],
        "agent ids": [e.agent_id for e in trace.events],
        "parents": [tuple(e.parents) for e in trace.events],
        "exposures": [tuple(e.exposures) for e in trace.events],
        "tool ids": [e.tool_id for e in trace.events],
        "input refs": [tuple(e.inputs_ref or ()) for e in trace.events],
        "output refs": [e.output_ref for e in trace.events],
        "source ids": [s.id for s in trace.sources],
        "source kinds": [s.kind for s in trace.sources],
        "derived_from": [s.derived_from for s in trace.sources],
        "origin_event": [s.origin_event for s in trace.sources],
        "structural check records": sorted(
            (c.source_id, c.target_event, c.verdict)
            for c in trace.checks
            if c.method == "structural" and CARRIER_NOTE not in (c.notes or "")
        ),
    }


def _pair(scenario: str, influencing: bool, workflow: str, tmp: Path):
    """The same scenario run with attribution inline, and with none."""
    from src.eval.experiment import _original_run

    inline = tmp / f"{scenario}-{influencing}-{workflow}-inline.jsonl"
    none = tmp / f"{scenario}-{influencing}-{workflow}-none.jsonl"
    _original_run(scenario, influencing, inline, 20260906, "hybrid",
                  workflow=workflow)
    _original_run(scenario, influencing, none, 20260906, "none",
                  workflow=workflow)
    return read_trace(inline), read_trace(none)


class TestExecutionProvenanceIsAttributionIndependent(unittest.TestCase):
    """Chain topology, all three attack channels, benign and attacked.

    A = web, B = memory, C = agent_message -- the three propagation paths the
    provenance system has to record.
    """

    CASES = [
        ("A", True, "short"), ("A", False, "short"), ("A", True, "long"),
        ("B", True, "short"), ("B", False, "short"), ("B", True, "long"),
        ("C", True, "short"), ("C", False, "short"), ("C", True, "long"),
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_execution_fact_is_identical(self) -> None:
        for scenario, influencing, workflow in self.CASES:
            with self.subTest(scenario=scenario, influencing=influencing,
                              workflow=workflow):
                inline, none = _pair(scenario, influencing, workflow,
                                     Path(self.tmp.name))
                got, expected = execution_facts(inline), execution_facts(none)
                for field in EXECUTION_FACT_FIELDS:
                    with self.subTest(field=field):
                        self.assertEqual(
                            got[field], expected[field],
                            f"{field} changed because attribution was installed",
                        )

    def test_the_graph_shape_is_identical_including_content_free_fields(self) -> None:
        """Belt and braces: the pipeline built the same graph, event for event."""
        inline, none = _pair("A", True, "short", Path(self.tmp.name))
        self.assertEqual(len(inline.events), len(none.events))
        self.assertEqual(len(inline.sources), len(none.sources))

    def test_attribution_derived_records_are_allowed_to_differ(self) -> None:
        """The other half of the contract. If these were identical too, the
        attributor would not be doing anything."""
        inline, none = _pair("A", True, "short", Path(self.tmp.name))
        self.assertGreater(len(inline.influence), len(none.influence),
                           "the inline attributor recorded no extra influence")

    def test_carrier_records_are_attribution_derived_not_execution_facts(self) -> None:
        """The misdiagnosis, pinned. Carrier records differ between the two
        runs and are SUPPOSED to: they inherit upstream verdicts (D-067)."""
        inline, none = _pair("A", True, "short", Path(self.tmp.name))

        def carriers(trace):
            return {(c.source_id, c.target_event) for c in trace.checks
                    if c.method == "structural" and CARRIER_NOTE in (c.notes or "")}

        self.assertNotEqual(carriers(inline), carriers(none))

    def test_counting_carriers_as_structural_is_what_produced_the_false_alarm(self) -> None:
        """Documents the arithmetic of the original bad measurement: the two
        kinds together differ, the execution facts alone do not."""
        inline, none = _pair("A", True, "short", Path(self.tmp.name))

        def all_structural(trace):
            return {(c.source_id, c.target_event) for c in trace.checks
                    if c.method == "structural"}

        def code_path_only(trace):
            return {(c.source_id, c.target_event) for c in trace.checks
                    if c.method == "structural"
                    and CARRIER_NOTE not in (c.notes or "")}

        self.assertNotEqual(all_structural(inline), all_structural(none))
        self.assertEqual(code_path_only(inline), code_path_only(none))


class TestFanOutExecutionProvenanceIsAttributionIndependent(unittest.TestCase):
    """The other topology, so the contract is not a property of one shape."""

    def _pair(self, workers, poisoned):
        from src.eval.attacks import label_malicious
        from src.eval.fanout_scenarios import FanoutScenario
        from src.eval.gate1_experiment import FanoutScriptedClient
        from src.provenance.estimator import HybridAttributor
        from src.tracing.pipeline import run_pipeline
        from src.tracing.tools import Tools, fanout_corpus

        scenario = FanoutScenario.build(
            workers, "influencing", poisoned_indices=tuple(range(poisoned)))
        docs = [dict(d) for d in fanout_corpus(workers)]
        out = []
        for label, use_attributor in (("inline", True), ("none", False)):
            path = Path(self.tmp.name) / f"fan{workers}-{label}.jsonl"
            tools = scenario.apply(Tools.from_fixtures(
                memory_path=path.with_suffix(".memory.json")))
            client = FanoutScriptedClient(docs=docs, marker=scenario.marker,
                                          token=scenario.token)
            attributor = HybridAttributor(
                client=client, mode="self_report", model="scripted",
            ) if use_attributor else None
            run_pipeline(path, client=client, tools=tools,
                         attributor=attributor, **scenario.workflow_kwargs)
            label_malicious(path, scenario.marker)
            out.append(read_trace(path))
        return out

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_fan_out_execution_facts_are_identical(self) -> None:
        for workers, poisoned in ((4, 1), (8, 1), (8, 4)):
            with self.subTest(workers=workers, poisoned=poisoned):
                inline, none = self._pair(workers, poisoned)
                got, expected = execution_facts(inline), execution_facts(none)
                for field in EXECUTION_FACT_FIELDS:
                    with self.subTest(field=field):
                        self.assertEqual(got[field], expected[field])


class TestTheContentDifferenceIsATestbedArtefact(unittest.TestCase):
    """The stored prompt/output content differs, and it is the harness's doing.

    `ScriptedClient` answers a self-report by looking the audited call up in its
    own `by_prompt` table. That is what makes the scripted self-report truthful,
    and it is why the attributor is handed the pipeline's client instance. The
    sharing -- not the pipeline -- is what perturbs later outputs.
    """

    def test_the_scripted_self_report_reads_its_own_call_table(self) -> None:
        import inspect

        from src.eval.scripted import ScriptedClient

        source = inspect.getsource(ScriptedClient._answer_self_report)
        self.assertIn("_audited", source)
        self.assertIn("truly_used", source)

    def test_a_fresh_client_cannot_answer_truthfully(self) -> None:
        """The consequence: separating the clients does not fix anything, it
        makes the harness lie."""
        from src.eval.scripted import ScriptedClient

        fresh = ScriptedClient(seed=1)
        self.assertIsNone(fresh._audited("a prompt it never saw"))


class TestTheTaxonomyIsNotAccidental(unittest.TestCase):
    """`CARRIER_NOTE` is the only thing separating the two record kinds.

    `src/eval/real_llm.py` already warns that if the phrase changes, ground
    truth silently starts reading the estimator's own answers back to itself.
    It is also what made the §6.1 measurement wrong. Pinned here as well, since
    this file is now a second reader of it.
    """

    def test_the_marker_still_exists_and_is_non_empty(self) -> None:
        self.assertTrue(CARRIER_NOTE)
        self.assertIn("carries output", CARRIER_NOTE)

    def test_record_structural_never_writes_the_carrier_marker(self) -> None:
        import inspect

        from src.provenance import attribution

        source = inspect.getsource(attribution.record_structural)
        self.assertNotIn("CARRIER_NOTE", source)


if __name__ == "__main__":
    unittest.main()
