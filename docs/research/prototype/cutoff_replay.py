"""
Prototype, isolated from production: INTERVENTIONAL CHANGE PROPAGATION.

Recovery replay in which the replay itself is the counterfactual.

    for each pipeline model event e, in the original order:
        p' = the prompt the rerun composes for e, flagged sources redacted
        if p' == stored prompt of e          -> identical input: splice, 0 calls
        else                                  -> re-execute e on p' (1 call)
             if output(p') ~ output(original) -> keep ORIGINAL output (cutoff:
                                                 children see identical input)
             else                             -> keep the NEW output (changed)

No self-report. No separate counterfactual pass. No inline attribution
during the normal run (the original is produced with estimator "none").

Two equivalence modes for "~":
    exact       byte equality only.  No comparator; safety needs nothing but
                input completeness.
    comparator  the SAME calibrated signature comparator + carryover facet the
                production counterfactual uses (src/provenance/estimator.py),
                so the soundness assumptions are exactly CausalLine's.

Nothing here is imported by src/. It reaches the production replay engine
through `run_pipeline` with a client wrapper, exactly as `replay()` does.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.common.llm import LLMResponse
from src.eval.attacks import build
from src.eval.baselines import b0_full_restart, b2_topology_closure
from src.eval.detectors import build as build_detector
from src.eval.experiment import _fresh_tools, _original_run, run_baseline_recovery, scripted_calibration, WORKFLOWS
from src.eval.metrics import ground_truth_events
from src.eval.scripted import ScriptedClient
from src.provenance.signatures import compare, for_event, with_carryover
from src.recovery.replay import SELF_REPORT_MARKER, pipeline_model_events, redact_flagged
from src.tracing.logger import read_trace
from src.tracing.pipeline import run_pipeline


@dataclass
class CutoffClient:
    original: Any
    inner: Any
    flagged: set
    mode: str = "comparator"          # "exact" | "comparator"
    calibration: Any = None
    spliced: list = field(default_factory=list)      # identical input, 0 calls
    unchanged: list = field(default_factory=list)    # re-executed, output kept
    changed: list = field(default_factory=list)      # re-executed, output replaced
    replay_tokens: int = 0
    inner_calls: int = 0
    total_tokens: int = 0
    throttled_s: float = 0.0

    def __post_init__(self):
        self._queue = pipeline_model_events(self.original)
        self._i = 0
        self._pending = None
        self._last_issued = None

    def announce(self, agent_id, kind):
        self._pending = (agent_id, kind)

    def last_issued_prompt(self):
        return self._last_issued

    def _resp(self, text, tokens=0, model="spliced"):
        return LLMResponse(text=text, model=model, prompt_tokens=0, output_tokens=0,
                           thoughts_tokens=0, total_tokens=tokens, attempts=1,
                           latency_s=0.0, slept_s=0.0, finish_reason="SPLICE")

    def _equivalent(self, event, orig_out, new_out, orig_prompt, new_prompt):
        if orig_out == new_out:
            return True
        if self.mode == "exact":
            return False
        comp = for_event(event.kind, orig_out, event.tool_id)
        excluded = (self.calibration.exclude_for(comp.name)
                    if self.calibration else set())
        # Carryover: material present only in the ORIGINAL input (the removed
        # source, or an upstream output that has since changed) must not
        # survive into the kept answer. Same facet the production
        # counterfactual adds (D-064), measured against the same "elsewhere".
        before = with_carryover(comp.signature(orig_out, None), orig_out,
                                orig_prompt, new_prompt)
        after = with_carryover(comp.signature(new_out, None), new_out,
                               orig_prompt, new_prompt)
        same, _moved = compare(before, after, exclude=excluded)
        return same

    def generate(self, prompt, system=None, json_output=False, temperature=None):
        self._last_issued = None
        if SELF_REPORT_MARKER in prompt:
            raise RuntimeError("cutoff replay never asks a self-report")
        eid = self._queue[self._i]
        self._i += 1
        announced, self._pending = self._pending, None
        ev = self.original.event(eid)
        if announced is not None and announced != (ev.agent_id, ev.kind):
            raise RuntimeError(f"rerun diverged at {eid}: {announced}")
        orig_prompt = self.original.prompt_text(eid) or ""
        orig_out = self.original.output_text(eid) or ""
        cleaned = redact_flagged(prompt, self.flagged)
        if cleaned == orig_prompt:
            self.spliced.append(eid)
            return self._resp(orig_out)
        self._last_issued = cleaned
        r = self.inner.generate(cleaned, system=system, json_output=json_output,
                                temperature=temperature)
        self.inner_calls += 1
        self.replay_tokens += r.total_tokens
        self.total_tokens += r.total_tokens
        if self._equivalent(ev, orig_out, r.text, orig_prompt, cleaned):
            self.unchanged.append(eid)
            return self._resp(orig_out, 0, model="kept")
        self.changed.append(eid)
        return r


def _contains_flagged(text: str, payloads: list[str]) -> bool:
    return any(p and len(p) >= 12 and p in (text or "") for p in payloads)


def run_cp(scenario, influencing, workdir: Path, seed=20260906, mode="comparator",
           workflow="short", detector="oracle", cost_model="flat"):
    workdir.mkdir(parents=True, exist_ok=True)
    variant = "influencing" if influencing else "exposed_only"
    stem = f"cp-{scenario}-{variant}-{workflow}-{detector}-{cost_model}"
    orig_path = workdir / f"{stem}.jsonl"
    # NORMAL RUN: no attributor installed at all.
    _res, client, true_inf = _original_run(scenario, influencing, orig_path, seed,
                                           "none", workflow=workflow,
                                           cost_model=cost_model)
    original = read_trace(orig_path)
    attack = build(scenario, influencing)
    verdict = build_detector(detector).flag(original)
    flagged = set(verdict.sources())
    truth = ground_truth_events(original, true_influence=true_inf)
    payloads = [s.content for s in original.sources if s.id in flagged]

    # Baselines on the SAME original trace, through the production replay().
    base = {}
    for name, disc in (("B0", b0_full_restart(original, flagged)),
                       ("B2", b2_topology_closure(original, flagged))):
        out = workdir / f"{stem}-{name}.jsonl"
        # Same extended fixtures every other method replays with. run_cell
        # omits `tools` here, so its long-workflow baselines replay on the
        # SHORT fixtures (see the research report).
        res, rep = run_baseline_recovery(name, disc, original,
                                         ScriptedClient(seed=seed + 1, cost_model=cost_model),
                                         sorted(flagged), attack, out,
                                         tools=_fresh_tools(attack, out, extended=(workflow == "long")))
        base[name] = dict(tokens=rep.replay_tokens, calls=rep.inner_calls,
                          discarded=len(disc), unsafe=len(truth - set(disc)),
                          task=res.task_success)

    # CUTOFF REPLAY.
    out = workdir / f"{stem}-CP-{mode}.jsonl"
    tools = _fresh_tools(attack, out, extended=(workflow == "long"))
    cpc = CutoffClient(original=original,
                       inner=ScriptedClient(seed=seed + 1, cost_model=cost_model),
                       flagged=flagged, mode=mode, calibration=scripted_calibration())
    t0 = time.time()
    res = run_pipeline(out, task=original.meta.get("task"), tools=tools, client=cpc,
                       attributor=None, handoff_hook=attack.handoff_hook,
                       **WORKFLOWS[workflow])
    rec = read_trace(out)
    # What recovery did NOT keep: replaced model outputs, any event whose
    # recomputed bytes differ, and any event whose stored output holds flagged
    # content (issue #20: ingestion is containment, read off the bytes).
    discarded = set(cpc.changed)
    for e in original.events:
        o = original.output_text(e.id)
        if rec.has_event(e.id):
            r2 = rec.output_text(e.id)
            if (o or "") != (r2 or ""):
                discarded.add(e.id)
        else:
            discarded.add(e.id)
        if _contains_flagged(o, payloads):
            discarded.add(e.id)
    # Byte re-check on what was kept (D-068 analogue): no kept output may hold
    # flagged material.
    kept = {e.id for e in original.events} - discarded
    survived = [eid for eid in kept if _contains_flagged(original.output_text(eid), payloads)]
    unsafe = sorted(truth - discarded)
    return dict(scenario=scenario, variant=variant, workflow=workflow, mode=mode,
                detector=detector, events=len(original.events),
                model_events=len(pipeline_model_events(original)),
                truth=len(truth), flagged=sorted(flagged),
                cp_tokens=cpc.replay_tokens, cp_calls=cpc.inner_calls,
                spliced=len(cpc.spliced), unchanged=len(cpc.unchanged),
                changed=len(cpc.changed), cp_discarded=len(discarded),
                cp_unsafe=len(unsafe), unsafe_ids=unsafe, survived=survived,
                cp_false_pos=len(discarded - truth),
                cp_task=res.task_success, pipeline_tokens=original.pipeline_tokens(),
                base=base, wall=round(time.time() - t0, 2))


def run_cl(scenario, influencing, workdir: Path, seed=20260906, estimator_mode="hybrid",
           workflow="short", detector="oracle", cost_model="flat"):
    """Production CausalLine on the same cell and pricing (mirrors run_cell)."""
    from src.eval.baselines import discarded_events
    from src.recovery.causalline import recover
    from src.tracing.checkpoints import CheckpointStore, checkpoint_path_for
    variant = "influencing" if influencing else "exposed_only"
    stem = f"cl-{scenario}-{variant}-{workflow}-{estimator_mode}-{detector}-{cost_model}"
    orig_path = workdir / f"{stem}.jsonl"
    _r, client, true_inf = _original_run(scenario, influencing, orig_path, seed,
                                         estimator_mode, workflow=workflow,
                                         cost_model=cost_model)
    original = read_trace(orig_path)
    attack = build(scenario, influencing)
    flagged = build_detector(detector).flag(original).sources()
    truth = ground_truth_events(original, true_influence=true_inf)
    out = workdir / f"{stem}-CL.jsonl"
    rec = recover(original, flagged, ScriptedClient(seed=seed + 1, cost_model=cost_model),
                  out, tools=_fresh_tools(attack, out, extended=(workflow == "long")),
                  checkpoints=CheckpointStore.load(checkpoint_path_for(orig_path)),
                  handoff_hook=attack.handoff_hook)
    disc = discarded_events(rec.report, rec.invalidated)
    tb = original.tokens_by_purpose()
    return dict(mode=estimator_mode, analysis=rec.analysis_tokens, replay=rec.replay_tokens,
                total=rec.analysis_tokens + rec.replay_tokens, discarded=len(disc),
                events=len(original.events), unsafe=len(truth - disc),
                task=rec.task_success, scope=rec.scope, escalations=rec.escalations,
                by_purpose=tb, truth=len(truth))


if __name__ == "__main__":
    wd = Path(sys.argv[1])
    for wf in ("short", "long"):
        for mode in ("exact", "comparator"):
            for sc in ("A", "B", "C"):
                for infl in (True, False):
                    r = run_cp(sc, infl, wd, mode=mode, workflow=wf)
                    print(r)
