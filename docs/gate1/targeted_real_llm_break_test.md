# Gate 1 — targeted real-LLM break test

**Verdict: FALSIFIED.** Four independent target-regime cases were reached, and
Gate 1 chose RESTART on all four when the oracle said INVESTIGATE, at a total
regret of **14,964 tokens**. In every one the structural bound over-predicted
the true footprint by the maximum possible amount: `f_structural − f_true =
+1.000`.

| | |
|---|---|
| model | `llama3.2:3b`, 100% on GPU |
| design points | **12** (8 primary + 4 follow-up) |
| real runs | **12** (no repeats; prior work showed short-prompt runs byte-identical) |
| `git diff HEAD -- src/recovery/gate1.py` | **empty, before and after** |
| tests | **359 pass** (46 Gate 1: 10 + 22 + 14) |

---

## 1. The hypothesis under test

> **H** — the structural upper bound remains useful even when exposure is wide
> but actual influence is narrow/zero and investigation is cheap.

The previous suite (`real_llm_falsification.md`) could not test this: its
wide-exposure family measured `A/N ≈ 2.05`, so restart was already justified
and Gate 1 agreeing was not an error.

**Target regime, defined before the run:**

```
A/N < 1                      investigation is cheap
A/N + f_true       < 1       oracle says INVESTIGATE
A/N + f_structural > 1       Gate 1 says RESTART
```

Only cases satisfying all three can falsify H (§8 of the brief).

## 2. Workflow construction, and why it targets the missing regime

**The previous failure had a structural cause.** A briefing read by every
analyst *entered* at every analyst. `b2_topology_closure` locates a flagged
source by its `origin_event`, so K entry points meant K compromised agents —
and K separate pairs each needing its own counterfactual. `A` grew with `K`
exactly as fast as the closure did. The two quantities were locked together.

**The construction that separates them: a dispatcher at the head of the call
graph.** Each analyst's first event takes the dispatcher's output event as a
cross-agent parent, which is precisely what `CallGraph.from_trace` turns into a
`calls` edge, so every analyst is `reachable_from("dispatcher")`. The briefing
goes to the dispatcher alone.

Measured offline before any real run:

| shape | flagged sources | f_structural |
|---|---|---|
| K=8, no dispatcher | 8 | 1.000 |
| K=8, **dispatcher** | **1** | **1.000** |
| K=16, no dispatcher | 16 | 1.000 |
| K=16, **dispatcher** | **1** | **1.000** |

One entry event, total structural reach. `src/tracing/fanout.py`, off by
default; pinned by `tests/test_gate1_real.py::TestTheBreakTestConstruction`.

## 3. The second blocker, found by the first run

The dispatcher alone was **not** sufficient. Under the default (self-report)
investigation, D1–D5 still measured `A/N` = 1.96, 1.56, 1.55, 1.31, 1.31.

The trace says why:

```
S1  -> e0003   tainted   self_report      <- the dispatcher CLAIMED it used the bulletin
S3  -> e0006   clean     counterfactual
S3  -> e0009   tainted   self_report
S3  -> e0012   tainted   self_report      ... 14 checks, 10 self-reports
```

**One self-report false positive at the hub re-contaminated everything
downstream.** A self-report *positive* is accepted without evidence (by design,
the conservative direction), so it marked the briefing→dispatcher pair tainted;
the dispatcher's output source then became contaminated, and that source is
exposed to all K analysts — recreating the K checks the dispatcher was meant to
avoid.

So `A` was re-coupled to `K` by a *different* mechanism than last time.

**The third probe — evidence instead of a claim.** The brief (§4) asks whether
the architecture can produce wide exposure without one counterfactual per
exposure, using mechanisms already present. `HybridAttributor` already
documents a `counterfactual_only` ablation: no self-report, evidence for every
verdict. It was not reachable through `run_generated`, so it was exposed there
(`investigation_mode`, default `"self_report"` — no existing behaviour
changed). Gate 1 was not touched.

Result: **one counterfactual clears the hub and the frontier prunes.**

```
D6-disp16-ben-cf: checks written = 1
   S1 -> e0003  clean (counterfactual)
   analysis calls: {'counterfactual': 1}      A = 202 tokens, N = 3812
   events = 53, task_success = True, escalated = False, unsafe = 0
```

## 4. Raw results

`A_SCALE[lazy] = 0.547`, calibrated on 12 prior campaign runs only, never on
these cases. `mode`: `sr` = self-report (default), `co` = counterfactual-only.

| case | K | mode | N | A | R | A/N | f_true | f_struct | predicted | oracle | gate | result |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| D1-disp08-ben | 8 | sr | 1760 | 3442 | 1183 | 1.96 | 0.67 | 1.00 | 2.48 | RESTART | RESTART | ok |
| D2-disp16-ben | 16 | sr | 3812 | 5943 | 2937 | 1.56 | 0.77 | 1.00 | 2.55 | RESTART | RESTART | ok |
| D3-disp16-tar | 16 | sr | 3828 | 5943 | 2937 | 1.55 | 0.77 | 1.00 | 2.55 | RESTART | RESTART | ok |
| D4-disp24-tar | 24 | sr | 6388 | 8374 | 5369 | 1.31 | 0.84 | 1.00 | 2.58 | RESTART | RESTART | ok |
| D5-disp24-ben | 24 | sr | 6372 | 8374 | 5369 | 1.31 | 0.84 | 1.00 | 2.58 | RESTART | RESTART | ok |
| O1-fan08p01 | 8 | sr | 1273 | 983 | 310 | 0.77 | 0.24 | 0.30 | 0.97 | RESTART | INVESTIGATE | **FALSE RECOVERY** |
| O2-fan08p04 | 8 | sr | 1447 | 1218 | 534 | 0.84 | 0.37 | 0.66 | 1.69 | RESTART | RESTART | ok |
| O3-fan24p01 | 24 | sr | 3549 | 1737 | 552 | 0.49 | 0.16 | 0.18 | 0.77 | INVESTIGATE | INVESTIGATE | ok |
| **D6-disp16-ben-cf** | 16 | co | 3812 | **202** | 0 | **0.05** | **0.00** | **1.00** | 2.55 | **INVESTIGATE** | **RESTART** | **FALSE RESTART** |
| **D7-disp24-ben-cf** | 24 | co | 6372 | **266** | 0 | **0.04** | **0.00** | **1.00** | 2.58 | **INVESTIGATE** | **RESTART** | **FALSE RESTART** |
| **D8-disp08-ben-cf** | 8 | co | 1760 | **138** | 0 | **0.08** | **0.00** | **1.00** | 2.48 | **INVESTIGATE** | **RESTART** | **FALSE RESTART** |
| **D9-disp16-tar-cf** | 16 | co | 3828 | **202** | 0 | **0.05** | **0.00** | **1.00** | 2.55 | **INVESTIGATE** | **RESTART** | **FALSE RESTART** |

## 5. Target-regime cases — the falsification

All four satisfy `A/N < 1`, `A/N + f_true < 1`, `A/N + f_structural > 1`.

| case | A/N | f_true | f_struct | over-prediction | regret `N − (A+R)` |
|---|---|---|---|---|---|
| D6-disp16-ben-cf | 0.05 | 0.00 | 1.00 | **+1.000** | **3,610** |
| D7-disp24-ben-cf | 0.04 | 0.00 | 1.00 | **+1.000** | **6,106** |
| D8-disp08-ben-cf | 0.08 | 0.00 | 1.00 | **+1.000** | **1,622** |
| D9-disp16-tar-cf | 0.05 | 0.00 | 1.00 | **+1.000** | **3,626** |

```
false restarts in target regime: 4/4
total regret:                    14,964 tokens
```

In each case a **single 138–266 token counterfactual** would have cleared a
29–77 event workflow with nothing contaminated, `task_success = True`,
`escalated = False`, `unsafe = 0`. Gate 1 instead discards the entire run — up
to **6,106 tokens to avoid spending 266**.

The over-prediction is the largest arithmetically possible: the bound says the
whole trace is contaminated when **none of it is**.

## 6. False restarts

The four above. Mechanism: `f_structural` is agent-reachability from the entry
agent. A hub whose output every downstream agent reads produces a closure of
1.000 regardless of whether anything flowed. Since the gate requires
`Â/N + f_structural ≤ 1.174`, an `f_structural` of 1.000 means **no value of
`A`, however small, can produce an INVESTIGATE decision.** The decision is
saturated by the structural term alone.

This is not a calibration error. Setting `Â = 0` exactly still gives
`0 + 1.000 = 1.000 ≤ 1.174` — which would investigate — but the measured `Â/N`
is 1.55–1.58 here because `region_pairs` counts every exposure edge inside the
region, and the region is the whole trace. **Both terms are inflated by the
same structural fact**, and their sum is 2.48–2.58 against a truth of 0.05.

## 7. False recoveries (the opposite direction, brief §7)

| case | A/N | f_true | f_struct | oracle | gate | regret |
|---|---|---|---|---|---|---|
| O1-fan08p01 | 0.77 | 0.24 | 0.30 | RESTART | INVESTIGATE | 20 |

One case, 20 tokens, at a true margin of −0.016 — essentially exactly on the
break-even line. Both error types therefore occur, and making the estimator
less pessimistic would not be free: O1 shows the gate already investigating
slightly too eagerly where the bound is tight.

## 8. Was the target regime reachable?

**Yes, but only under a non-default investigation configuration.**

| configuration | A/N (dispatcher, benign) | regime reached |
|---|---|---|
| self-report (default), K=8/16/24 | 1.96 / 1.56 / 1.31 | **no** |
| counterfactual-only, K=8/16/24 | 0.08 / 0.05 / 0.04 | **yes** |

Under the default the hub's self-report false positive re-contaminates the
downstream and `A` stays above `N`. Under counterfactual-only, evidence clears
the hub in one call and the frontier prunes.

**This matters for how the falsification should be read.** The gate fails in a
configuration the repository supports and documents, on a workflow shape the
repository can build — but not in the default configuration as shipped. Both
halves of that sentence are load-bearing.

## 9. Limitations

1. **The falsifying cases all use `counterfactual_only`.** Under the default
   self-report configuration the regime was not reached on any of 5 dispatcher
   design points.
2. **`f_true = 0.00` on all four**, including D9, which *intended* one
   influenced analyst — the 3B model did not follow the planted instruction
   (`landed = False`), so ground truth records zero influence. D9 is therefore
   a fourth zero-influence case, not a distinct one-influence case.
3. **12 runs, no repeats**, one model, one workflow family.
4. The dispatcher shape was constructed for this test. It is a plausible
   coordinator topology, but it is a designed adversarial case, not an observed
   deployment.
5. Nothing here measures safety. A false restart is safe; it is expensive.

## 10. What can honestly be claimed

- "Four independent target-regime cases were constructed on a real LLM, and
  Gate 1 chose restart on all four, discarding between 1,622 and 6,106 tokens
  of recoverable work to avoid an investigation costing 138–266."
- "The failure mechanism is saturation of the structural term: when a flagged
  source enters at a hub agent, `f_structural` reaches 1.000 and the decision
  no longer depends on `A` at all."
- "The regime is reachable only when the investigation can clear the hub with
  evidence; an accepted self-report positive at the hub re-couples `A` to the
  fan-out width."

## 11. What cannot be claimed

- **Not** that Gate 1 fails in its default configuration — it did not, on these
  cases.
- **Not** that this is a common deployment shape. It was constructed to break
  the gate.
- **Not** any statistical rate: 4 target-regime cases, no repeats.
- **Not** that a repaired gate would work. No repair was attempted (brief §12).

---

## FINAL DECISION

**FALSIFIED.**

The brief's criterion is: *at least one genuine target-regime case demonstrates
a meaningful false restart caused by structural overestimation.* Four such
cases were produced, each satisfying `A/N < 1`, `A/N + f_true < 1` and
`A/N + f_structural > 1`, with `f_structural − f_true = +1.000` and regret of
1,622–6,106 tokens.

The structural upper bound is **not** sufficient as a stand-alone economic
pre-check when a flagged source enters at a hub agent: the closure saturates at
1.000, the decision stops depending on investigation cost entirely, and the
gate discards a fully recoverable workflow to avoid an investigation costing 4%
of a restart.

The qualification that belongs with the verdict: this was reached under the
`counterfactual_only` investigation configuration. Under the shipped default,
the hub's self-report false positive kept `A/N` above 1 on all five dispatcher
design points, and the gate was correct there. **The hypothesis as stated —
that the bound remains useful when exposure is wide, influence is nil and
investigation is cheap — is refuted. The narrower claim, that the bound is
adequate in the default configuration, is untouched by this experiment.**

No repair was attempted, per §12.
