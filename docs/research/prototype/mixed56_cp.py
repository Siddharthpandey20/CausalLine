"""
Offline reconstruction of the mixed56 workload-varied campaign, to price
interventional change propagation (cutoff replay) on the primary result.

1. Rebuild each run's 145-event trace with a deterministic STUB model (no
   network), using MixedScenario.build(regime, seed=seed) -- the same
   placement draw the real run used. This gives the DAG, the exposure sets,
   the event ids and a proportional token price per model event.
2. Check the reconstruction against the real run: event count, placement, and
   whether the real run's canary-bearing event ids are model/message events
   of the expected agents.
3. Simulate cutoff replay ON THE REAL RUN'S GROUND TRUTH: a model event's
   recovered output changes iff its real output carried the canary
   (events_with_token).  Assumption ("faithful replay"): a model event whose
   recovered inputs are clean reproduces the codes it originally produced.
   A model event is RE-EXECUTED iff its recovered prompt differs from the
   original: a flagged source is in it, or a message it reads changed.
4. Price: re-executed model events, at stub-proportional tokens, scaled per
   run so the stub's full-pipeline price equals the real run's measured B0
   replay tokens.  B2 priced the same way over its own closure, and checked
   against the real run's measured B2 replay tokens.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from src.common.prompts import parse_sources
from src.eval.baselines import b2_topology_closure
from src.eval.mixed_scenarios import MixedScenario
from src.recovery.replay import pipeline_model_events
from src.tracing.logger import read_trace
from src.tracing.mixed import Topology, MixedPipeline
from src.tracing.pipeline import run_pipeline
from src.tracing.tools import Tools
from src.common.llm import LLMResponse

CANARY = "ZZ999"
CODE = re.compile(r"\b([A-Z]{2}\d{3})\b")


class Stub:
    """Deterministic follower: every agent obeys a payload in its context."""
    model = "stub"

    def __init__(self):
        self.total_tokens = 0
        self.calls = 0
        self.throttled_s = 0.0

    def generate(self, prompt, system=None, json_output=False, temperature=None):
        self.calls += 1
        srcs = parse_sources(prompt)
        bad = [c for _s, _h, c in srcs if CANARY in c or "rotated" in c.lower()]
        if "Report the ACCESS CODE" in prompt:
            codes = [m for _s, _h, c in srcs for m in CODE.findall(c) if m != CANARY]
            text = CANARY if bad else (codes[0] if codes else "")
        elif prompt.startswith("Find the line for the"):
            site = prompt.split("Find the line for the ", 1)[1].split(" depot", 1)[0]
            text = ""
            for _s, _h, c in srcs:
                for line in c.splitlines():
                    if line.startswith(site + " "):
                        text = line.split()[-1]
        else:  # LIST: reproduce pairs, payload rewrites its named site
            pairs = []
            for _s, _h, c in srcs:
                if CANARY in c or "rotated" in c.lower() or "STANDING POLICY" in c \
                        or "Report codes exactly" in c:
                    continue
                for line in c.splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and CODE.fullmatch(parts[-1]):
                        pairs.append((" ".join(parts[:-1]), parts[-1]))
            for c in bad:
                for i, (site, _code) in enumerate(pairs):
                    if f"{site} depot" in c or f"for {site}" in c or f"the {site} record" in c:
                        pairs[i] = (site, CANARY)
            text = "\n".join(f"{s} {c}" for s, c in pairs)
        p = max(1, (len(prompt) + len(system or "")) // 4)
        o = max(1, len(text) // 4)
        self.total_tokens += p + o
        return LLMResponse(text=text, model="stub", prompt_tokens=p, output_tokens=o,
                           thoughts_tokens=0, total_tokens=p + o, attempts=1,
                           latency_s=0.0, slept_s=0.0, finish_reason="STOP")


def build_trace(regime, seed, workdir: Path):
    topo = Topology()
    sc = MixedScenario.build(regime, topology=topo, seed=seed)
    path = workdir / f"stub-{regime}-{seed}.jsonl"
    tools = sc.apply(Tools.from_fixtures(memory_path=path.with_suffix(".memory.json")))
    run_pipeline(path, task=sc.task, tools=tools, client=Stub(), workflow="mixed",
                 topology=topo.to_dict())
    tr = read_trace(path)
    # flagged = the planted sources (oracle detector reads Source.malicious;
    # here: sources whose content carries the scenario marker)
    flagged = {s.id for s in tr.sources if sc.marker and sc.marker in (s.content or "")}
    return tr, sc, flagged


def simulate(tr, flagged, changed_model):
    """Cutoff replay over the DAG.  Returns re-executed model events and the
    set of message sources whose content changes."""
    model = pipeline_model_events(tr)
    msrc = {}            # model event -> the message source(s) it emits
    for s in tr.sources:
        if s.origin_event:
            ev = tr.event(s.origin_event)
            if ev.kind == "message" and ev.parents:
                msrc.setdefault(ev.parents[0], set()).add(s.id)
    changed_src = set()
    reexec = []
    for eid in model:             # pipeline order is a topological order
        ev = tr.event(eid)
        inputs = set(ev.exposures)
        if inputs & flagged or inputs & changed_src:
            reexec.append(eid)
            if eid in changed_model:
                changed_src |= msrc.get(eid, set())
    return reexec, changed_src


def main(results_dir: Path, workdir: Path):
    workdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for f in sorted(results_dir.glob("seed*.json")):
        r = json.load(open(f))["results"][0]
        seed, regime = r["seed"], r["regime"]
        frozen = "placement" not in r or not r.get("vary_placement", False)
        tr, sc, flagged = build_trace(regime, None if frozen else seed, workdir)
        real_flagged = set(r["truth"]["planted"])
        if real_flagged != flagged:
            print("WARN flagged mismatch", seed, regime, sorted(flagged), sorted(real_flagged))
        flagged = real_flagged
        model = pipeline_model_events(tr)
        tok = {u.event_id: u.total_tokens for u in tr.usage if u.purpose == "pipeline"}
        rows_by = {x["method"].split()[0]: x for x in r["rows"]}
        real_b0 = rows_by["B0"]["replay_tokens"]
        real_b2 = rows_by["B2"]["replay_tokens"]
        real_cl_rep = rows_by["CausalLine"]["replay_tokens"]
        real_cl_an = rows_by["CausalLine"]["analysis_tokens"]
        scale = real_b0 / sum(tok[e] for e in model)
        closure = set(b2_topology_closure(tr, flagged))
        b2_model = [e for e in model if e in closure]
        changed_model = set(r["truth"]["events_with_token"]) & set(model)
        reexec, _ = simulate(tr, flagged, changed_model)
        # Upper bound ("noisy replay"): every re-executed event is treated as
        # changed, so nothing is ever cut off downstream of a re-execution.
        reexec_noisy, _ = simulate(tr, flagged, set(model))
        # ---- recall / unsafe against the real run's truth_events ----------
        truth_ev = set(r["truth"]["truth_events"])
        msg_of = {}
        for e in tr.events:
            if e.kind == "message" and e.parents:
                msg_of.setdefault(e.parents[0], []).append(e.id)
        payloads = [s.content for s in tr.sources if s.id in flagged]
        holds = {e.id for e in tr.events
                 if any(p and p in (tr.output_text(e.id) or "") for p in payloads)}
        discarded = set(reexec) | holds
        for m in changed_model:
            discarded |= set(msg_of.get(m, []))
        # Non-model events recomputed by pipeline code from a changed input
        # (the executor's comparison): changed iff a parent changed.
        changed_ev = set(discarded) - (set(reexec) - changed_model)
        for e in tr.events:
            if e.id in set(model) or e.kind == "message":
                continue
            if any(p in changed_ev for p in e.parents):
                discarded.add(e.id); changed_ev.add(e.id)
        unsafe_ev = sorted(truth_ev - discarded)
        missed_changed = sorted(changed_model - set(reexec))
        price = lambda evs: scale * sum(tok[e] for e in evs)
        c_b2_est = price(b2_model)
        c_cp = price(reexec)
        c_noisy = price(reexec_noisy)
        assert set(reexec) <= set(b2_model) | set(reexec_noisy)
        rows.append(dict(
            seed=seed, regime=regime, landed=r["truth"]["payload_landed"],
            placement=(r.get("placement") or {}).get("counts"),
            events=len(tr.events), real_events=r["events"], flagged=len(flagged),
            model_events=len(model), b2_model_events=len(b2_model),
            cp_model_events=len(reexec), changed_model=len(changed_model),
            real_B0=real_b0, real_B2=real_b2, est_B2=round(c_b2_est),
            real_CL=real_cl_an + real_cl_rep, real_CL_replay=real_cl_rep,
            CP=round(c_cp), CP_noisy=round(c_noisy),
            cp_over_min=round(c_cp / min(real_b0, real_b2), 3),
            cp_over_min_estB2=round(c_cp / min(real_b0, c_b2_est), 3),
            noisy_over_min=round(c_noisy / min(real_b0, real_b2), 3),
            reexec_not_in_b2=sorted(set(reexec) - set(b2_model)),
            truth_events=len(truth_ev), cp_discarded=len(discarded),
            unsafe=unsafe_ev, missed_changed=missed_changed,
            cl_discarded=rows_by["CausalLine"]["discarded"], b2_discarded=rows_by["B2"]["discarded"],
            cl_scope=rows_by["CausalLine"]["notes"], cl_esc=rows_by["CausalLine"]["escalations"],
            real_CL_analysis=real_cl_an,
            floor=round(price(changed_model)),
            real_B1=rows_by["B1"]["replay_tokens"],
            CP_esc=round(c_cp + (rows_by["B1"]["replay_tokens"] if rows_by["CausalLine"]["escalations"] >= 1 else 0)
                         + (real_b0 if rows_by["CausalLine"]["escalations"] >= 2 else 0)),
        ))
        print(rows[-1], flush=True)
    json.dump(rows, open(workdir / "mixed56_cp.json", "w"), indent=1)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
