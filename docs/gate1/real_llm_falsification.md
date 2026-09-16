# Gate 1 — falsification-first validation on a real LLM

**Verdict: PARTIALLY SUPPORTED.** The mechanism survived every pre-registered
falsification criterion, but the suite **could not demonstrate that it is
useful**: it differs from a trivial always-restart policy on only 2 of 10
design points, and the sign test on that comparison cannot reach significance
(p = 1.000, minimum achievable p = 0.500). §15 gives the full reasoning.

---

## 1. Research question

Does the Gate 1 mechanism — a structural upper bound on the recovery footprint
plus a calibrated estimate of `A/N` — transfer to a real LLM well enough to
justify the claim that it is a *useful zero-cost pre-investigation economic
gate*?

The objective was to find cases that make it wrong, not to make it pass.

## 2. Frozen hypothesis

> **H_G1** — a cheap structural upper bound on the eventual recovery footprint,
> combined with an estimate of investigation cost `A/N`, can make a useful
> pre-investigation recovery-vs-restart decision.

Decision rule, unchanged from D-092:

```
INVESTIGATE  iff  Â/N + f_structural  <=  1 + DECISION_MARGIN
DECISION_MARGIN = 0.174           (frozen; q25 of the measured estimator bias)
Â = A_SCALE * region_pairs * mean_event_cost
```

`git diff HEAD -- src/recovery/gate1.py` was empty before the run. Nothing in
the gate, the margin or the calibration procedure was changed at any point
during this experiment.

## 3. Experimental design

18 real runs over 10 design points, `llama3.2:3b`, 100% on GPU, Gate 1
**disabled** throughout so the oracle is measurable.

**Falsification criteria, written down before any case was run** (in
`src/eval/gate1_real.py`'s module docstring, committed with the runner):

| id | H_G1 is NOT SUPPORTED if… | fired? |
|---|---|---|
| F1 | repeated false RESTARTS in the localized family (A) | **no** — 0 |
| F2 | repeated false RECOVERIES in the widespread family (B) | **no** — 0 |
| F3 | false restarts in the wide-exposure/no-influence family (D) | **no** — but see §13 |
| F4 | total Gate-1 cost ≥ always-restart cost | **no** — 0.96× |
| F5 | collapse to a trivial always-restart policy (no INVESTIGATE at all) | **no** — but see §15 |
| F6 | calibration fails to transfer between eager and lazy | **partially** — see §12 |

"Repeated" = on more than one distinct design point, not merely one repetition.

**F3 was a pre-registered prediction of failure.** It did not fire, and §13
explains why — the reason is not that the gate handled the case.

## 4. Case matrix

| family | design point | arm | repeats | intent |
|---|---|---|---|---|
| A localized (moderate) | `fan08p01` | lazy | 2 | 1 of 8 analysts poisoned |
| A localized (larger) | `fan16p01` | lazy | 2 | 1 of 16 poisoned |
| B widespread | `fan08p08` | lazy | 2 | 8 of 8 poisoned |
| C near break-even | `fan08p03` | lazy | 2 | 3 of 8 poisoned |
| C near break-even | `fan12p04` | lazy | 2 | 4 of 12 poisoned |
| D wide exposure, no influence | `share08ben` | lazy | 2 | briefing to all 8, instructs nothing |
| D wide exposure, no influence | `share16ben` | lazy | 2 | briefing to all 16, instructs nothing |
| E wide exposure, narrow influence | `share08tar` | lazy | 2 | briefing to all 8, names one report |
| F eager/lazy | `fan08p01` | **eager** | 1 | pairs with family A |
| F eager/lazy | `share08ben` | **eager** | 1 | pairs with family D |

Families D and E required new workflow capability (a briefing every analyst
retrieves). **The first implementation failed to be adversarial**: logging the
briefing as one shared source gave `f_structural` = 0.13–0.20, no wider than an
ordinary single-analyst poisoning, because `b2_topology_closure` locates a
flagged source by its *entry event* and one entry point yields one compromised
agent however many contexts the source sits in. Each analyst now retrieves the
bulletin for itself, which is both the realistic shape and the one that
actually stresses the bound: `f_structural` = **1.000**. Pinned by
`tests/test_gate1_real.py`.

## 5. Ground-truth methodology

Every case ran with `gate1_enabled=False`. The complete process executed —
pipeline, detection, full investigation, Gate 2 planning, selective replay —
and `N`, `A`, `R` were read off the trace afterwards.

```
ORACLE = INVESTIGATE   iff   A + min(R, N) < N
```

The gate was then replayed **offline** against those traces. It never
influenced anything it is judged on.

## 6. Raw per-case results

| case | fam | N | A | R | A/N | f_true | true margin | f_struct | predicted | gate | oracle | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A-fan08p01-lazy ×2 | A | 1273 | 983 | 310 | 0.77 | 0.24 | −0.02 | 0.30 | 0.97 | INVESTIGATE | RESTART | **FALSE RECOVERY** |
| A-fan16p01-lazy ×2 | A | 2413 | 1246 | 432 | 0.52 | 0.18 | +0.30 | 0.21 | 0.82 | INVESTIGATE | INVESTIGATE | ok |
| B-fan08p08-lazy ×2 | B | 1679 | 1218 | 982 | 0.73 | 0.58 | −0.31 | 1.00 | 2.52 | RESTART | RESTART | ok |
| C-fan08p03-lazy ×2 | C | 1389 | 1218 | 423 | 0.88 | 0.30 | −0.18 | 0.55 | 1.46 | RESTART | RESTART | ok |
| C-fan12p04-lazy ×2 | C | 2015 | 1218 | 594 | 0.60 | 0.29 | +0.10 | 0.50 | 1.38 | RESTART | INVESTIGATE | **FALSE RESTART** |
| D-share08ben-lazy ×2 | D | 1783 | 3668 | 308 | 2.06 | 0.17 | −1.23 | 1.00 | 2.52 | RESTART | RESTART | ok |
| D-share16ben-lazy ×2 | D | 3491 | 7149 | 430 | 2.05 | 0.12 | −1.17 | 1.00 | 2.58 | RESTART | RESTART | ok |
| E-share08tar-lazy ×2 | E | 1911 | 3668 | 1095 | 1.92 | 0.57 | −1.49 | 1.00 | 2.52 | RESTART | RESTART | ok |
| F-fan08p01-eager ×1 | F | 1273 | 2851 | 310 | 2.24 | 0.24 | −1.48 | 0.30 | 2.48 | RESTART | RESTART | ok |
| F-share08ben-eager ×1 | F | 1783 | 3668 | 308 | 2.06 | 0.17 | −1.23 | 1.00 | 5.95 | RESTART | RESTART | ok |

**The repeats are not independent samples.** All 8 repeated design points
returned **byte-identical** `(N, A, R)`. On these short extraction prompts the
backend was deterministic, unlike the chain workflow where D-084 measured
non-determinism at temperature 0. **Effective n = 10 design points, not 18
runs**, and every aggregate below should be read that way.

## 7. Confusion matrix

| | gate INVESTIGATE | gate RESTART |
|---|---|---|
| **oracle INVESTIGATE** | 2 | 2 |
| **oracle RESTART** | 2 | 12 |

```
accuracy        14/18 = 77.8%
FALSE RESTART    2/18 = 11.1%
FALSE RECOVERY   2/18 = 11.1%
```

**Against the scripted held-out set this is a substantial degradation**:
97.0% → 77.8%, and the 0% false-recovery property did **not** transfer.

## 8. Economic cost comparison

| policy | tokens | vs restart | vs oracle |
|---|---|---|---|
| always investigate (pre-Gate-1 behaviour) | 57,021 | 1.63 | 1.72 |
| always restart | 34,964 | 1.00 | 1.06 |
| **GATE 1** | **33,534** | **0.96** | **1.01** |
| ORACLE | 33,088 | 0.95 | 1.00 |

Gate 1 saves 23,487 tokens (41.2%) against always-investigate. **But
always-restart alone saves 22,057 (38.7%).** The gate's marginal contribution
beyond the trivial policy is **1,430 tokens — 6.5% of what always-restart
already delivers**. Of the 1,876 tokens available above always-restart, the
gate captured 1,430 (76%) — from a single design point.

**Paired against always-restart, per design point:**

| design point | Gate 1 − always-restart |
|---|---|
| A-fan16p01-lazy | **+735** |
| A-fan08p01-lazy | **−20** |
| the other 8 | 0 (identical decision) |

```
1W – 1L – 8T over 10 design points, net +715 tokens
sign test p = 1.000   (with 2 discordant points, minimum achievable p = 0.500)
```

**The suite is structurally incapable of demonstrating an advantage over
always-restart.** This is the single most important number in the report.

## 9. False-restart analysis

| case | A/N | f_true | f_struct | over-prediction | predicted | regret |
|---|---|---|---|---|---|---|
| C-fan12p04-lazy ×2 | 0.60 | 0.29 | 0.50 | **+0.21** | 1.38 | 203 each |

Total false-restart regret **406 tokens** (1.2% of the always-restart total).

Both are the same design point, and both sit at true margin **+0.101** — inside
the near-break-even band. The mechanism is the known one: `f_structural`
over-predicts by +0.21 and `Â/N` adds its own excess, pushing the sum to 1.38
against a truth of 0.89.

## 10. False-recovery analysis

| case | A/N | f_true | regret | A |
|---|---|---|---|---|
| A-fan08p01-lazy ×2 | 0.77 | 0.24 | 20 each | 983 |

Total false-recovery regret **40 tokens**. Both at true margin **−0.016** —
essentially exactly on the break-even line, where the oracle itself barely
prefers restart. Regret is bounded by `A` as predicted, and here came in three
orders of magnitude below that bound because Gate 2 caught the replay.

## 11. Break-even analysis

Every one of the four errors lies within |true margin| < 0.15:

| case | true margin | predicted | verdict |
|---|---|---|---|
| A-fan08p01-lazy ×2 | −0.016 | 0.97 | FALSE RECOVERY |
| C-fan12p04-lazy ×2 | +0.101 | 1.38 | FALSE RESTART |

**No case outside the near-break-even band was decided wrongly.** This
reproduces the scripted finding: the gate is wrong only where being wrong is
cheap, and total error regret is 446 tokens — **1.3% of the always-restart
budget**. It is also the same limitation restated: near break-even the gate
carries no information.

## 12. Calibration analysis

`A_SCALE` measured from the 24 pre-existing campaign runs only
(`fanout-campaign.json`), never from this suite.

| arm | A_SCALE | calibrated on | mean (Â/N − A/N) on this suite | n |
|---|---|---|---|---|
| lazy | 0.547 | 12 prior lazy runs | **−0.039** | 16 |
| eager | 1.781 | 12 prior eager runs | **+1.414** | 2 |

**Lazy transferred well** (−0.039 — the estimate is near-unbiased on unseen
lazy cases). **Eager did not**: a mean over-estimate of +1.414 in `A/N` units.
Neither eager case produced an error, because the oracle said RESTART there
anyway and over-estimating only reinforces that — so **F6 fired numerically but
not behaviourally**, on n = 2. This is weak evidence and is reported as such.

## 13. Wide exposure / no influence (family D) — the pre-registered prediction

**The prediction did not come true, and the reason is not that the gate handled
the case.**

The shape worked exactly as designed: 8 and 16 flagged sources, `f_structural`
= **1.000**, `landed = False` on every run (the briefing genuinely influenced
nothing). The gate said RESTART, as predicted.

But the prediction required `A/N < 1`, and the measured `A/N` was **2.06 and
2.05**. The oracle therefore also said RESTART, and the gate was correct.

| family | mean flagged sources | mean region pairs | mean A/N |
|---|---|---|---|
| A (one poisoned source) | 1.0 | 15 | 0.64 |
| D (briefing everywhere) | 12.0 | 37 | **2.05** |

**Wide exposure inflates `A` and `f_structural` together.** More flagged
sources means more pairs to check, so the investigation the gate is deciding
about becomes genuinely expensive at the same moment the bound becomes
pessimistic. On this architecture the feared failure mode appears
**self-limiting**.

That is a real structural observation, but it rests on **two design points**,
and it is not a proof. The combination that would break the gate — wide
exposure with *cheap* analysis — was not produced here and remains untested.

## 14. Eager vs lazy (family F)

| design | arm | A | A/N | predicted | gate | oracle |
|---|---|---|---|---|---|---|
| fan08p01 | lazy | 983 | 0.77 | 0.97 | INVESTIGATE | RESTART |
| fan08p01 | **eager** | 2851 | 2.24 | 2.48 | RESTART | RESTART |
| share08ben | lazy | 3668 | 2.06 | 2.52 | RESTART | RESTART |
| share08ben | **eager** | 3668 | 2.06 | 5.95 | RESTART | RESTART |

On the same workflow, eager self-report costs **2.9× the analysis** of lazy
(2851 vs 983) and flips both the oracle and the gate from INVESTIGATE to
RESTART. **The arm changes the economics more than the workflow does**, which
is why a single `A_SCALE` cannot serve both — confirming D-092's finding on
independent cases.

Note `share08ben` eager: predicted 5.95 against a lazy 2.52 on the *same*
measured `A` (3668). That gap is the eager scale (1.781) applied where the
realized cost matched the lazy one — the calibration error of §12 made visible.

## 15. Did the hypothesis survive?

**PARTIALLY SUPPORTED.**

*What the evidence supports:*

- The mechanism is genuinely **zero-cost** — it makes no model calls.
- The **structural bound held**: `f_structural < f_true` in **0 of 18** cases,
  mean over-prediction +0.392. It remains a valid upper bound on real traces.
- **No pre-registered falsification criterion fired.** In particular the gate
  did not throw away recoverable work in the localized family (F1: 0 false
  restarts) and did not over-investigate in the widespread family (F2: 0).
- **All four errors lie within |margin| < 0.15**, total regret 446 tokens =
  1.3% of the always-restart budget. The gate is wrong only where it is cheap
  to be wrong.
- Against the pre-Gate-1 behaviour (always investigate) it saves **41.2%**.

*What the evidence does not support:*

- **That it is useful, in the sense the hypothesis claims.** The correct
  baseline for a *gate* is always-restart, not always-investigate. Against
  always-restart the gate differs on **2 of 10 design points**, net **+715
  tokens (2.0%)**, sign test **p = 1.000**. With two discordant points the
  experiment cannot reach significance whatever the outcome. **This is an
  underpowered comparison, not a demonstrated benefit.**
- **That accuracy transfers.** 97.0% scripted → **77.8%** real, and the 0%
  false-recovery property did not survive (11.1% here).
- **That family D's success is evidence of robustness.** The regime it was
  built to test was never reached (§13).
- **That calibration transfers across arms.** Lazy did (−0.039); eager did not
  (+1.414), on n = 2.

## 16. Limitations

1. **Effective n = 10 design points.** All 8 repeated points returned identical
   `(N, A, R)`; repetition bought no variance information here.
2. **One model** (`llama3.2:3b`), **one workflow family** (fan-out), one attack
   channel (`web`).
3. **Only 2 discordant design points** against always-restart, so no
   statistical claim of benefit is available.
4. **Family D did not reach its target regime**; the wide-exposure-with-cheap-
   analysis case remains untested.
5. **Eager arm has n = 2**, so §12 and §14's eager conclusions are indicative
   only.
6. No case in this suite produced an unsafe preservation, but **this experiment
   does not test safety** and no safety claim is made from it.
7. Families D and E required a new workflow capability written for this
   experiment; they are constructed adversarial shapes, not observed ones.

## 17. What can honestly be claimed

- "On 18 real-LLaMA runs across 10 adversarial design points, Gate 1 made no
  systematic error: every mistake fell within 15% of the economic break-even
  point and the total error regret was 1.3% of the restart budget."
- "The structural bound did not under-predict the recovery footprint on any
  real run (0/18)."
- "Gate 1 reduced total cost by 41% against investigating unconditionally."
- "Analysis cost is configuration-dependent: eager self-report cost 2.9× lazy
  on the same workflow, and a scale calibrated on one arm mis-estimated the
  other."
- "Wide exposure inflated analysis cost and the structural bound together, so
  the predicted wide-exposure failure did not occur in this suite."

## 18. What cannot be claimed

- **Not** "Gate 1 is validated." The comparison against the trivial
  always-restart baseline is underpowered (2 discordant points, p = 1.000).
- **Not** "robust." Four errors in 18 runs, and one failure prediction went
  untested.
- **Not** "generalizes to real LLMs." One model, one workflow family.
- **Not** "safe." Safety is a separate property and was not measured here.
- **Not** that the 97% scripted accuracy transfers — it measurably did not.
- **Not** that the wide-exposure failure mode is refuted — it was not reached.

---

## FINAL DECISION

**PARTIALLY SUPPORTED.**

The mechanism survived every pre-registered falsification attempt, kept its
upper-bound property on real traces (0/18 under-predictions), confined all of
its errors to the near-break-even band at 1.3% total regret, and cost nothing
to run. Those are genuine results and they are evidence *for* the structural
half of H_G1.

But H_G1 claims the gate is **useful**, and usefulness must be measured against
always-restart, which is free and trivial. On this suite Gate 1 differed from
always-restart on 2 of 10 design points for a net 2.0% saving at p = 1.000. The
evidence is **preliminary and underpowered**, not negative — and the one
adversarial family designed to break the mechanism never reached the regime
that would have done so.

The structural component of H_G1 is supported. The usefulness component is
**not yet demonstrated on a real LLM.**
