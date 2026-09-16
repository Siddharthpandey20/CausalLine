"""The fan-out workflow, offline.

WHY THIS FILE EXISTS
--------------------
The fan-out shape is the only one in this project where the contaminated
fraction `f` can be small, so it is the only place the economics claim can be
tested at all. Everything asserted here is checked without a model, because the
alternative is finding out after an hour of GPU time -- which is exactly how
D-085 and the three contract gaps in D-084 were found.

    python -m unittest tests.test_fanout -v
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.provenance.contamination import contaminate
from src.tracing.logger import read_trace
from src.tracing.pipeline import run_pipeline
from src.tracing.tools import Tools, fanout_corpus


class EchoClient:
    """Answers each analyst with the value its document carries.

    Deliberately not a mock of `generate`: it parses the prompt the pipeline
    actually composed, so a prompt that failed to include the document, or an
    analyst exposed to somebody else's document, shows up as a wrong answer
    rather than passing silently.
    """

    model = "echo-test"
    total_tokens = 0

    def __init__(self, docs):
        self.docs = docs
        self.calls = 0

    def generate(self, prompt, system=None, json_output=False, temperature=None):
        from src.common.llm import LLMResponse

        self.calls += 1
        # An analyst prompt contains exactly one document; the aggregator's
        # contains the analysts' findings.
        matches = [d for d in self.docs if d["name"].upper() in prompt]
        if len(matches) == 1:
            text = matches[0]["value"]
        else:
            # Aggregator: reproduce every value it was given, in order.
            found = [d["value"] for d in self.docs if f"\n{d['value']}" in prompt
                     or prompt.count(d["value"])]
            text = "\n".join(found)
        self.total_tokens += 10
        return LLMResponse(
            text=text, model=self.model, prompt_tokens=8, output_tokens=2,
            total_tokens=10, thoughts_tokens=0, attempts=1, latency_s=0.0,
            slept_s=0.0,
        )


class TestTheFanoutWorkflowRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "fanout.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, workers):
        docs = fanout_corpus(workers)
        tools = Tools.from_fixtures(memory_path=self.path.with_suffix(".memory.json"))
        tools.fanout_docs = docs
        return run_pipeline(
            self.path, client=EchoClient(docs), tools=tools,
            workflow="fanout", workers=workers,
        ), docs

    def test_the_task_can_actually_succeed(self) -> None:
        """The point of the shape. On the chain workflow the 3B model failed
        the task on 56 of 60 runs (D-090), so `verify()` could never certify a
        replay and selective recovery was never delivered. Here the work is
        extraction, not code generation."""
        outcome, _docs = self._run(4)
        self.assertTrue(outcome.task_success, outcome.detail)

    def test_the_header_records_the_shape_so_replay_can_rebuild_it(self) -> None:
        self._run(3)
        meta = read_trace(self.path).meta
        self.assertEqual(meta["workflow"], "fanout")
        self.assertEqual(meta["workers"], 3)

    def test_no_analyst_sees_another_analysts_sources(self) -> None:
        """The structural precondition for localized contamination.

        Not "exactly one source each" -- an analyst may legitimately hold its
        report and a planted note beside it. The property that matters is
        disjointness: if two analysts shared a source, one poisoned document
        would reach both and the region could never stay small.
        """
        self._run(4)
        trace = read_trace(self.path)
        seen: dict[str, str] = {}
        for event in trace.events:
            if not event.agent_id.startswith("analyst"):
                continue
            for sid in event.exposures:
                owner = seen.setdefault(sid, event.agent_id)
                self.assertEqual(
                    owner, event.agent_id,
                    f"{sid} is exposed to both {owner} and {event.agent_id}",
                )

    def test_f_falls_as_the_workflow_grows(self) -> None:
        """The measurement the whole shape exists to make.

        One poisoned analyst contaminates its own events plus the aggregator
        and the output -- a constant -- while the trace grows with `workers`.
        On the chain workflow this ratio was flat (1.48 at both lengths,
        docs/local_llm_frontier/03 §3); here it has to fall, or the shape has
        not achieved what it was built for.
        """
        fractions = []
        for workers in (3, 6, 12):
            with self.subTest(workers=workers):
                self.setUp()
                _outcome, _docs = self._run(workers)
                trace = read_trace(self.path)
                # Poison the FIRST analyst's document source.
                poisoned = next(
                    s.id for s in trace.sources
                    if s.metadata.get("doc") == "report-01"
                )
                region = contaminate(trace, {poisoned})
                fractions.append(len(region.events) / len(trace.events))
                self.tearDown()
        self.assertLess(fractions[1], fractions[0],
                        f"f did not fall from 3 to 6 workers: {fractions}")
        self.assertLess(fractions[2], fractions[1],
                        f"f did not fall from 6 to 12 workers: {fractions}")
        self.assertLess(fractions[2], 0.35,
                        f"f never became small: {fractions}")



class TestSelectiveRecoveryIsActuallyDelivered(unittest.TestCase):
    """The property the whole shape was built to make testable.

    On the chain workflow CausalLine escalated to a full restart on 17 of 17
    landed runs (D-086), because the 3B model failed the task before any attack
    and `verify()` cannot certify a replay of an already-broken workflow. Every
    "work preserved" number this project has ever reported was therefore about
    what the method *identifies*, never about what it *delivers*.

    Here the task is achievable, the payload is a separate source that redaction
    can remove without taking a required fact with it, and the selective replay
    is certified. This test fails if either of those regresses.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, workers):
        from src.eval.fanout_scenarios import FanoutScenario
        from src.eval.real_llm import run_generated
        from src.common.llm import LLMResponse

        scenario = FanoutScenario.build(workers)
        docs = [dict(d) for d in fanout_corpus(workers)]

        class Poisonable(EchoClient):
            """Follows the planted instruction when it can see it.

            When the replay client has redacted the planted source the marker
            is gone from the prompt, so this falls through to the honest
            answer -- which is exactly what a real model does and what makes
            the recovery meaningful rather than stipulated.
            """

            def generate(self, prompt, system=None, json_output=False,
                         temperature=None):
                if scenario.marker in prompt:
                    self.total_tokens += 10
                    return LLMResponse(
                        text=scenario.token, model=self.model, prompt_tokens=8,
                        output_tokens=2, total_tokens=10, thoughts_tokens=0,
                        attempts=1, latency_s=0.0, slept_s=0.0,
                    )
                return super().generate(prompt, system, json_output, temperature)

        return run_generated(
            scenario, Poisonable(docs), workdir=self.tmp.name,
            detector_name="oracle",
            replay_client_factory=lambda: Poisonable(docs),
        )

    def _row(self, result, method):
        import dataclasses
        for row in result.rows:
            data = dataclasses.asdict(row)
            if data["method"] == method:
                return data
        self.fail(f"no row for {method}")

    def test_the_attack_lands_so_there_is_something_to_recover(self) -> None:
        result = self._run(4)
        self.assertTrue(result.ok, result.failure)
        self.assertTrue(
            result.truth.payload_landed,
            "the payload never landed, so every unsafe count here would be "
            "arithmetic rather than a safety result",
        )

    def test_causalline_recovers_selectively_without_escalating(self) -> None:
        result = self._run(4)
        row = self._row(result, "CausalLine")
        self.assertEqual(
            row["escalations"], 0,
            "CausalLine escalated; the delivered-preservation number is then "
            "about a full restart, not about selective recovery",
        )
        self.assertGreater(row["work_preserved"], 0.0)
        self.assertEqual(row["unsafe_preservations"], 0)

    def test_it_preserves_more_than_every_baseline_and_delivers_it(self) -> None:
        result = self._run(8)
        mine = self._row(result, "CausalLine")
        for method in ("B0 full restart", "B1 agent taint", "B2 topology closure"):
            with self.subTest(method=method):
                theirs = self._row(result, method)
                self.assertGreater(
                    mine["work_preserved"], theirs["work_preserved"],
                    f"CausalLine preserved {mine['work_preserved']:.2f} against "
                    f"{method}'s {theirs['work_preserved']:.2f}",
                )

    def test_the_blast_radius_does_not_grow_with_the_workflow(self) -> None:
        """The economic claim, as a structural fact: one poisoned analyst
        contaminates a constant number of events however large the run is.
        On the chain workflow this number grew with the trace, which is why
        `A/N + f` was flat across lengths."""
        small = self._row(self._run(4), "CausalLine")["blast_radius_events"]
        self.tearDown()
        self.setUp()
        large = self._row(self._run(8), "CausalLine")["blast_radius_events"]
        self.assertEqual(
            small, large,
            f"blast radius moved from {small} to {large} when the workflow "
            "doubled; contamination is not localized after all",
        )


if __name__ == "__main__":
    unittest.main()
