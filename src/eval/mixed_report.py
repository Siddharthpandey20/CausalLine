"""Render the 56-agent campaign's results as the tables the write-up needs.

    python -m src.eval.mixed_report [data/results/mixed56.json]

Reads only what the campaign wrote. Computes nothing the harness did not
already measure, except the contaminated fraction `f`, which is recomputed
straight off each stored trace so the reported regime is the one that actually
happened rather than the one the design intended.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# The scored rows carry the long names; the recovered traces are written with
# the short ones. Both spellings come from `real_llm.run_generated`, so the map
# lives here rather than being guessed at each use site.
METHODS = ("B0 full restart", "B1 agent taint", "B2 topology closure",
           "CausalLine")
TRACE_SUFFIX = {"B0 full restart": "B0", "B1 agent taint": "B1",
                "B2 topology closure": "B2", "CausalLine": "CausalLine"}


def _rows(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["method"]: r for r in run.get("rows", []) if r.get("method")}


def measured_fractions(run: dict[str, Any]) -> dict[str, Any]:
    """`f_true` and `f_structural` recomputed from the stored trace.

    The design says where the payload was planted; only the trace says how far
    it got. Reporting the design's intent as the result would be the thing
    `MixedAnnotation` is explicitly marked non-authoritative to prevent.
    """
    from src.eval.attacks import label_malicious
    from src.eval.baselines import b2_topology_closure
    from src.provenance.contamination import contaminate
    from src.tracing.logger import read_trace

    path = Path(run.get("trace_path", ""))
    if not path.exists():
        return {}
    trace = read_trace(path)
    flagged = [s.id for s in trace.sources if s.malicious] or label_malicious(
        path, (run.get("scenario") or {}).get("marker", ""))
    total = len(trace.events) or 1
    region = set(contaminate(trace, set(flagged)).events)
    closure = set(b2_topology_closure(trace, flagged))
    return {
        "events": total,
        "flagged_sources": len(flagged),
        "f_true": len(region) / total,
        "f_structural": len(closure) / total,
        "closure_escapes": len(region - closure),
        "agents_touched": len({e.agent_id for e in trace.events
                               if e.id in region}),
    }


def targeted_codes(run: dict[str, Any]) -> list[str]:
    """The codes the payloads actually asked an agent to replace.

    Computed from the scenario's own placement, NOT inferred from what the
    original run happened to get wrong. The first version used "codes missing
    from the original output", which silently swept up an unrelated copy error
    and then credited recovery with restoring it -- exactly the conflation this
    whole section exists to undo.
    """
    from src.tracing.mixed import Topology
    from src.tracing.tools import mixed_corpus

    scenario = run.get("scenario") or {}
    topo = Topology.from_dict(scenario.get("topology"))
    docs = mixed_corpus(topo.acquisition)
    out: list[str] = []
    for index in scenario.get("poisoned_docs", []):
        if 0 <= index < len(docs):
            out.append(docs[index]["code"])
    for j in scenario.get("poisoned_memory", []):
        block = topo.acqs_for(j)
        if block:
            out.append(docs[block[0] - 1]["code"])
    for v in scenario.get("poisoned_messages", []):
        block = topo.specs_for(v)
        if block:
            out.append(docs[topo.spec_source(block[0]) - 1]["code"])
    return sorted(set(out))


def recovery_outcome(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per method: what recovery did about CONTAMINATION, separately from
    whether the run passed the exact end-to-end check.

    WHY THESE HAVE TO BE REPORTED APART
    ------------------------------------
    Measured on the small regime. The original run lost two codes: `JF203`,
    which the payload replaced with the canary, and `QX417`, which the model
    simply dropped. Every selective recovery removed the canary and restored
    `JF203` -- it got the contamination exactly right -- and then dropped
    `QX417` in a FRESH copy error during the replay, and failed the exact
    check for that.

    Reporting only the exact check would record that as "selective recovery
    failed". It did not: it was rejected for a model slip unrelated to the
    attack. And only CausalLine is ever rejected this way, because only
    CausalLine verifies -- which is `docs/03` #15 exactly, and the reason
    D-090 exists.

    `canary_removed` and `attacked_code_restored` are what recovery is
    responsible for. `exact` is what the model plus recovery jointly achieved.
    """
    import json as _json

    from src.tracing.logger import read_trace

    original = Path(run.get("trace_path", ""))
    if not original.exists():
        return {}
    token = (run.get("scenario") or {}).get("token", "")
    # CausalLine escalates, and each attempt writes its own trace. Reporting
    # only the first would show a selective attempt next to a `task_success`
    # that belongs to the scope it finally delivered at -- two different runs
    # in one row. Every attempt is listed instead, in the order it was tried.
    attempts: list[tuple[str, Path]] = [("ORIGINAL", original)]
    for method in METHODS:
        stem = f"{original.stem}-{TRACE_SUFFIX[method]}"
        attempts.append((method, original.with_name(stem + original.suffix)))
        if method == "CausalLine":
            for level in (1, 2, 3):
                escalated = original.with_name(
                    f"{stem}-esc{level}{original.suffix}")
                if escalated.exists():
                    attempts.append(
                        (f"CausalLine esc{level}", escalated))

    out: dict[str, dict[str, Any]] = {}
    for method, path in attempts:
        if not path.exists():
            continue
        trace = read_trace(path)
        finals = [e for e in trace.events
                  if e.agent_id == "executor" and e.kind == "agent_output"]
        if not finals:
            continue
        body = _json.loads(trace.content.get(finals[-1].output_ref) or "{}")
        expected, produced = body.get("expected", []), body.get("produced", [])
        out[method] = {
            "exact": bool(body.get("success")),
            "canary_present": token in produced,
            "missing": [c for c in expected if c not in produced],
            "produced_n": len(produced),
            "expected_n": len(expected),
        }
    lost_to_attack = set(targeted_codes(run))
    for method, row in out.items():
        row["attacked_codes_restored"] = sorted(
            lost_to_attack - set(row["missing"]))
        row["lost_in_replay"] = sorted(
            set(row["missing"]) - lost_to_attack)
    return out


def render(payload: dict[str, Any]) -> str:
    runs = payload.get("results", [])
    out: list[str] = []
    add = out.append

    # --- 0. the question the topology was built to answer -------------------
    add("## 6. Headline: does selective recovery beat the topology closure "
        "at a broadcast hub?\n")
    add("The hub exposes every downstream agent to whatever reaches it, so B2 "
        "discards the whole downstream trace. CausalLine's claim is that "
        "*influence* is narrower than that exposure. This is the comparison "
        "the 56-agent shape exists to make.\n")
    add("| regime | B2 preserved | CausalLine preserved | gain | "
        "B2 unsafe | CausalLine unsafe | task after recovery |")
    add("|---|---:|---:|---:|---:|---:|---|")
    for run in runs:
        rows = _rows(run)
        b2, cl = rows.get("B2 topology closure"), rows.get("CausalLine")
        if not (b2 and cl):
            add(f"| {run.get('regime','?')} | -- | -- | -- | -- | -- | not measured |")
            continue
        # In PERCENTAGE POINTS, because that is what the column says.
        gain = (cl["work_preserved"] - b2["work_preserved"]) * 100
        add(f"| {run.get('regime','?')} | {b2['work_preserved']:.1%} "
            f"| {cl['work_preserved']:.1%} | **{gain:+.1f} pts** "
            f"| {b2['unsafe_preservations']} | {cl['unsafe_preservations']} "
            f"| {'delivered' if cl.get('task_success') else 'not delivered'} |")
    add("\n`unsafe` is event-level unsafe preservation: recovery kept an event "
        "that was truly contaminated. It is the number that must be 0, and a "
        "gain bought by a non-zero value here is not a gain.\n")

    # --- 1. what ran -------------------------------------------------------
    add("## 7. The regimes, as measured\n")
    add("| regime | channel | flagged | events | agents touched | "
        "`f` true | `f` structural | escapes | band |")
    add("|---|---|---:|---:|---:|---:|---:|---:|---|")
    bands = {"small": (0.05, 0.15), "medium": (0.25, 0.50),
             "large": (0.60, 0.90)}
    for run in runs:
        regime = run.get("regime", "?")
        stats = measured_fractions(run)
        if not stats:
            add(f"| {regime} | -- | -- | -- | -- | -- | -- | -- | no trace |")
            continue
        low, high = bands.get(regime, (0.0, 1.0))
        inband = "in band" if low <= stats["f_true"] <= high else (
            f"**outside** {low:.0%}-{high:.0%}")
        add(f"| {regime} | {(run.get('scenario') or {}).get('design', {}).get('channel', '?')} "
            f"| {stats['flagged_sources']} | {stats['events']} "
            f"| {stats['agents_touched']} | {stats['f_true']:.1%} "
            f"| {stats['f_structural']:.1%} | {stats['closure_escapes']} "
            f"| {inband} |")

    # --- 2. recovery -------------------------------------------------------
    add("\n## 8. Recovery, per method\n")
    add("| regime | method | discarded | work preserved | unsafe (event) | "
        "pair FN | task | analysis tok | replay tok |")
    add("|---|---|---:|---:|---:|---:|---|---:|---:|")
    for run in runs:
        rows = _rows(run)
        for method in METHODS:
            row = rows.get(method)
            if not row:
                continue
            add(f"| {run.get('regime','?')} | {method} | {row['discarded']} "
                f"| {row['work_preserved']:.1%} "
                f"| {row['unsafe_preservations']} "
                f"| {row.get('pair_false_negatives', 0)} "
                f"| {'OK' if row.get('task_success') else 'fail'} "
                f"| {row['analysis_tokens']} | {row['replay_tokens']} |")

    # --- 2b. contamination outcome, apart from the exact check -------------
    add("\n## 9. What recovery did about the contamination\n")
    add("The exact end-to-end check asks whether twelve codes arrived "
        "correctly. That conflates two different things: whether recovery "
        "removed the attack, and whether the models copied cleanly on the "
        "replay. They are separated here because they came apart in "
        "measurement, and only CausalLine is ever charged for the second -- "
        "it is the only method that verifies its own work.\n")
    add("| regime | run | canary in output | codes the attack took out, "
        "restored | codes lost to a fresh replay slip | exact check |")
    add("|---|---|---|---|---|---|")
    for run in runs:
        for method, row in recovery_outcome(run).items():
            add(f"| {run.get('regime','?')} | {method} "
                f"| {'**yes**' if row['canary_present'] else 'no'} "
                f"| {', '.join(row['attacked_codes_restored']) or '--'} "
                f"| {', '.join(row['lost_in_replay']) or 'none'} "
                f"| {'pass' if row['exact'] else 'fail'} |")

    # --- 9b. the ex-ante gate ----------------------------------------------
    add("\n## 9b. Gate 1's ex-ante decision against what actually happened\n")
    add("Gate 1 (D-092) decides *before* investigating whether investigation "
        "can pay: INVESTIGATE iff `A_hat/N + f_structural < 1 + margin`. It "
        "was frozen before this campaign and nothing here was tuned.\n")
    add("| regime | Gate 1 said | `A_hat/N` estimated | `A/N` actual | "
        "analysis spent | CausalLine outcome | restart outcome | "
        "was the gate right? |")
    add("|---|---|---:|---:|---:|---|---|---|")
    for run in runs:
        rows = _rows(run)
        cl = rows.get("CausalLine")
        b0 = rows.get("B0 full restart")
        note = next((n for n in run.get("notes", []) if n.startswith("GATE1")),
                    "")
        if not (cl and b0 and note):
            continue
        said = "INVESTIGATE" if "INVESTIGATE" in note else "RESTART"
        estimated = ""
        for token in note.replace("=", " ").split():
            if token.replace(".", "").isdigit() and not estimated:
                estimated = token
        total = run.get("pipeline_tokens", 0) or 1
        actual = cl.get("analysis_tokens", 0) / total
        # Investigating was the right call ONLY if it ended up preserving
        # work a restart would not have. A CausalLine run that escalates all
        # the way to `restart_all` reaches exactly where a free restart
        # reaches, having first paid for the whole investigation -- so
        # `task_success` alone must not count as vindication.
        better = cl["work_preserved"] > 0.0
        right = (said == "INVESTIGATE") == better
        add(f"| {run.get('regime','?')} | **{said}** | {estimated} "
            f"| {actual:.2f} | {cl.get('analysis_tokens', 0)} tok "
            f"| {cl['work_preserved']:.1%} preserved, task "
            f"{'OK' if cl.get('task_success') else 'fail'} "
            f"| 0.0% preserved, task "
            f"{'OK' if b0.get('task_success') else 'fail'} "
            f"| {'yes' if right else '**no**'} |")

    # --- 3. the external accounting ---------------------------------------
    add("\n## 10. External API accounting\n")
    add("| regime | provider | requests | tokens | failures | "
        "refused by budget | statuses |")
    add("|---|---|---:|---:|---:|---:|---|")
    for run in runs:
        for name, ledger in (run.get("ledgers") or {}).items():
            add(f"| {run.get('regime','?')} | {name} | {ledger['requests']} "
                f"| {ledger['total_tokens']} | {ledger['failures']} "
                f"| {ledger['skipped_quota']} | "
                f"{ledger['statuses'] or '--'} |")
    sources = {l.get("limit_source") for run in runs
               for l in (run.get("ledgers") or {}).values()}
    add("\nCeiling provenance (every one of them): "
        + "; ".join(sorted(s for s in sources if s)) + "\n")

    # --- 4. routing and degradation ---------------------------------------
    add("## 11. Which model actually answered\n")
    add("| regime | routing | degraded agents |")
    add("|---|---|---|")
    for run in runs:
        degraded = run.get("degraded") or []
        add(f"| {run.get('regime','?')} | {run.get('routing')} | "
            f"{', '.join(degraded) if degraded else 'none'} |")

    # --- 5. cost and time --------------------------------------------------
    add("\n## 12. Cost and wall clock\n")
    # `self_report_calls`, `api_calls` and the retry counters are NOT shown.
    # `run_generated` reads them off a client's `stats`, which the NVIDIA and
    # Gemini clients carry and the router does not -- so on this workload they
    # are structurally zero rather than measured, and a column of zeros that
    # means "not instrumented" is worse than no column. The per-provider
    # request and failure counts in section 10 are the measured equivalents.
    add("| regime | original task | pipeline tok | analysis tok | "
        "analysis / pipeline | wall clock |")
    add("|---|---|---:|---:|---:|---:|")
    for run in runs:
        total = run.get("pipeline_tokens", 0) or 1
        add(f"| {run.get('regime','?')} "
            f"| {'OK' if run.get('task_success') else 'fail'} "
            f"| {run.get('pipeline_tokens', 0)} "
            f"| {run.get('analysis_tokens', 0)} "
            f"| {run.get('analysis_tokens', 0) / total:.2f}x "
            f"| {run.get('wall_clock_s', 0)}s |")

    # --- 6. what did not run ----------------------------------------------
    failed = [r for r in runs if not r.get("ok", True)]
    if failed:
        add("\n## 12b. Runs that did not produce a measurement\n")
        for run in failed:
            add(f"- **{run.get('regime')}** -- {run.get('failure') or run.get('problems')}")
    return "\n".join(out)


def main() -> None:
    """`mixed_report.py [results.json] [out.md]`

    Writes with an explicit encoding rather than through stdout: this console
    is cp1252, and piping a table containing an en dash to a file silently
    replaced it with `?`. A report is a deliverable, not console output.
    """
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/results/mixed56.json")
    if not path.exists():
        raise SystemExit(f"no results at {path}")
    text = render(json.loads(path.read_text(encoding="utf-8")))
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(text, encoding="utf-8")
        print(f"wrote {sys.argv[2]} ({len(text)} chars)")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(text)


if __name__ == "__main__":
    main()
