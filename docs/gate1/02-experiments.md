# Gate 1 — Phases 2–11: what was tried, what broke, what survived

The brief was explicit: do not assume SPRT is the answer, design experiments to
**falsify** each candidate, and report a negative result if that is what the
data says. Two candidates were falsified outright, one survived the scripted
evaluation and then **failed on the real model**, and was repaired by removing
the thing that failed rather than by tuning it.

---

## 1. The decision, derived

At Gate 1, with Gate 2 (the planner's replay cap) still underneath:

```
RESTART      : cost = N
INVESTIGATE  : cost = A + min(R, N)        the min IS Gate 2
```

```
INVESTIGATE better  <=>  A + min(R,N) < N
   R >= N :  A + N < N          impossible for A > 0
   R <  N :  A + R < N   <=>   A/N + f < 1,   f = R/N
```

So `f* = 1 - A/N` is **derived**, not assumed, and it agrees with the
`f_star_ex_ante` already in `economics.py`.

**The regret structure is asymmetric, and it drives everything that follows:**

| error | regret | bound |
|---|---|---|
| false RESTART (truth was recover) | `N - (A + R)` | ≤ `N` |
| false RECOVERY (truth was restart) | `A + min(R,N) - N` | **≤ `A`** |

Because Gate 2 caps the replay, wrongly investigating costs at most `A`, while
wrongly restarting costs up to `N`. **When `A < N`, investigating is the cheaper
mistake**, and a gate should demand decisive evidence before restarting.

---

## 2. Experimental setup

**Oracle (Phase 6).** For every case the *complete* process is run — pipeline,
detection, full investigation, planning, selective replay — and `N`, `A`, `R`
are read off the trace. `ORACLE = RECOVER iff A + min(R,N) < N`. This is a
measurement of what happened, never a label.

The first held-out set was **thrown away** because it violated this: Gate 1 had
already been wired into `run_generated`, so it suppressed the very
investigations whose cost defines the oracle — 67 cases came back with `A = 0`.
`run_generated` now takes `gate1_enabled=False` and the case collector forces
it, so the gate can never define its own ground truth.

**The tape.** The trace is append-only and interleaves `check` and `usage`
records, so an investigation replays exactly:
`(k, source, event, verdict, cumulative_analysis_tokens)`. A gate that stops
after `k` checks has an exact cost, and every candidate is scored on the *same*
tapes. No verdict is simulated.

**Case family.** Fan-out with `K` analysts and `m` poisoned, `m` swept on a fine
ladder so `f` walks through break-even in small steps; plus chain scenarios
(A/B/C × short/long × influencing/exposed-only); plus exposed-only controls
(wide exposure, zero influence — adversarial for every exposure-derived signal);
plus noisy runs where the planted instruction is followed only 40–70% of the
time.

**Dev / held-out split is by workflow size, not random** — `K ∈ {6,10,14,20,28}`
against `K ∈ {8,12,18,24,32}`, different seeds. A random split would put
near-identical repetitions of one design point on both sides.

| set | n | oracle RECOVER | RESTART | borderline |
|---|---|---|---|---|
| development | 65 | 19 | 46 | 4 |
| borderline grid | 50 | 16 | 34 | 8 |
| held-out | 67 | 22 | 45 | 5 |
| real GPU (`llama3.2:3b`) | 24 | 6 | 18 | — |

The held-out set has a different class balance from development, which is what
exposes a gate that merely learned "usually restart".

---

## 3. Candidates, hypotheses, falsification

### H1 — P, the compromise probability. **FALSIFIED.**

*Hypothesis:* `P = 1 - Π(1-pa_i)^{m_i}` predicts contamination footprint well
enough to gate on.

*Falsifier:* if `P` is near-constant across cases whose true footprint differs,
it carries no decision information.

*Result:* **`P` saturates.** Measured range 0.988–0.998 across cases whose
`f_true` ran 0.00 to 0.83. Gates at `P < 0.5`, `P < 0.9` and `P < 0.99` are
**numerically identical to always-restart** on both sets (67.2% held-out).

This is not a surprise the experiment discovered — `should_investigate`'s own
docstring says "on our own traces this gate never fires" — but it is now
measured across 132 cases. **The cause is structural, not a bad threshold:**
`P` counts *exposure attempts* and is monotone in exposure count, so a long
workflow with many benign retrievals scores higher than a short one with a
single decisive injection. It answers "was this run attacked?", not "how much of
it is contaminated?"

*Role retained:* **none as a gate.** It is not rejected as a concept — it is
sound for what it was built for (ex-ante run-level risk) — but no role as hard
gate, soft prior, ordering or budget mechanism is supported by these data,
because at 0.99±0.005 it cannot order anything.

### H2 — Structural prior. **SURVIVED scripted, FAILED real, repaired.**

*Hypothesis:* the free cost-weighted B2 closure bounds `f` tightly enough.

*Falsifier:* cases where it under-predicts (→ false recovery), or over-predicts
enough to cause expensive false restarts.

*Result:* it **never under-predicted — 0 of 132 cases.** It is a genuine upper
bound, which is why this gate's false-recovery rate is structurally near-zero.
It over-predicts by a mean of +0.20 (fan-out) to +0.43 (chain).

### H3 — Adaptive budget / early peek. **FALSIFIED.**

*Hypothesis:* the tainted rate over the first `k` checks estimates `f`.

*Falsifier:* if early verdicts are biased, or if the tainted rate is not `f`.

*Result:* **both.**

- Early verdicts are biased upward: mean tainted rate 1.000 at `k ≤ 2` against a
  final rate of 0.880. The frontier expansion starts at the flagged source, so
  the first candidate is *always* tainted.
- **Fatal:** the tainted rate is not `f` **at all**. Mean tainted rate 0.880
  against mean `f_true` 0.345, correlation **0.234**.

The reason is structural: `_unchecked_in_region` only ever offers pairs whose
source is *already believed contaminated*, so a high tainted rate is
near-guaranteed by construction. Meanwhile `f = R/N` is a **cost-weighted
fraction of the whole trace**. Different numerators, different denominators.

Every peek gate collapsed to always-restart (67.2%) **while also spending
278–765 tokens to get there**. Strictly dominated.

### H0 — Cost-aware SPRT. **NOT falsified, but dominated.**

*Hypothesis:* sequential evidence with cost-derived thresholds beats a static
gate.

*Falsifier:* SPRT assumes i.i.d. Bernoulli draws from a population whose
parameter is the quantity under test.

*Result:* **the assumption does not hold**, for the same reason H3 failed — the
observations are adaptive (a clean upstream verdict removes downstream
candidates), ordered, and drawn from a non-stationary population that is not
`f`. The test is accumulating evidence about one quantity and deciding about
another.

It still *works* better than chance — 86.6% held-out with an honest `A`
estimate — because "mostly tainted early" does correlate weakly with "widespread
contamination". But:

- it costs **781 tokens per decision** (p95 1379) where the structural gate
  costs **0**;
- its total realized cost on held-out is **1.13 × restart** — worse than simply
  always restarting;
- its errors are **expensive**: mean regret 1957 tokens per error, against 296
  for the structural gate, because an SPRT abort has paid for analysis *and then*
  restarts anyway.

**An earlier run scored SPRT at 94% — that number was a leak.** It was building
its thresholds from `case.a_full`, the true analysis cost, which is unavailable
at gate time. With the estimate a deployment would actually have, 86.6%.

### H4 — Hybrid (structural bound, then a few checks). **Retained but not chosen.**

Matches the structural gate's accuracy (91.0% held-out) while spending 558
tokens to do it. The empirical peek never changed a decision the bound had not
already made correctly, so the extra evidence bought nothing here.

---

## 4. The borderline region — a negative result that matters

Accuracy by distance from break-even, 115 dev + borderline cases:

| gate | \|margin\|<0.05 | 0.05–0.15 | 0.15–0.40 | >0.40 |
|---|---|---|---|---|
| always restart | 25% | 58% | 70% | 75% |
| P < 0.99 | 25% | 58% | 73% | 75% |
| peek k=4 | 25% | 58% | 70% | 75% |
| SPRT .15 | 25% | 58% | 89% | 100% |
| structural | 25% | 58% | **95%** | **100%** |

**No gate beats always-restart within 15% of break-even.** Every mechanism
tested collapses there. The structural gate's advantage comes entirely from
cases where the answer is not close.

**Why this is tolerable rather than fatal:** regret *is* `|margin| × N` by
construction, so an error at the boundary is cheap by definition. The structural
gate's 10 errors across 115 cases cost **2,955 tokens in total** — 296 each,
with 8 of them inside `|margin| < 0.15` costing 1,847 between them and **zero
errors above `|margin| = 0.40`**. It is wrong only where being wrong does not
matter. That is the property to claim, not accuracy near the boundary.

---

## 5. The bias correction, and why it is not a tuned knob

With `total < 1.0`, the structural gate produced **10 false restarts and zero
false recoveries** on development — a systematic one-sided pull, exactly what
the brief warned against. Every failure had the same signature: `f_struct`
over-predicting `f_true` by ~0.15 and `Âhat/N` over-predicting by ~0.07, the two
upward biases compounding to push the sum just over 1.0 when the truth sat just
under.

Both estimators are upper bounds *by construction*, so the compound bias

```
(Âhat/N + f_struct) - (A/N + f_true)
```

was measured on development: **positive on 65 of 65 cases**, median +0.223,
q25 +0.174.

`DECISION_MARGIN = 0.174` is that **q25 — derived from the estimator's
independently-known error, not from an accuracy sweep**. (A sweep would have
picked 0.15, which is close; picking by sweep would have been tuning on the
metric, so it was not used.)

---

## 6. Held-out results (scripted), gate frozen

67 cases, oracle measured with Gate 1 **off**.

| gate | acc | falseRST | falseREC | gate tokens | vs restart | regret |
|---|---|---|---|---|---|---|
| always recover (today) | 32.8% | 0.0% | 67.2% | 0 | 1.13 | 33.1% |
| always restart | 67.2% | 32.8% | 0.0% | 0 | 1.00 | 17.6% |
| P < 0.99 | 67.2% | 32.8% | 0.0% | 0 | 1.00 | 17.6% |
| peek k=4 | 67.2% | 32.8% | 0.0% | 765 | 1.30 | 52.2% |
| SPRT .15 (honest A) | 86.6% | 13.4% | 0.0% | 781 | 1.13 | 33.1% |
| hybrid k=4 | 91.0% | 9.0% | 0.0% | 558 | 1.08 | 26.8% |
| **GATE 1 (chosen)** | **97.0%** | **3.0%** | **0.0%** | **0** | **0.85** | **0.1%** |
| ORACLE (ceiling) | 100% | 0 | 0 | 0 | 0.85 | 0 |

---

## 7. Phase 11 — the real model, where it broke

**The frozen gate scored 25.0% on 24 real GPU runs: 18 false recoveries.** The
opposite failure from the scripted sets, and a complete one.

Diagnosis: `A_SCALE` — the constant converting "region pairs at mean event
cost" into expected analysis tokens — **is a property of the client and prompt
structure, not of the algorithm.**

| configuration | A_SCALE needed |
|---|---|
| scripted client | 0.431 |
| `llama3.2:3b`, lazy self-report | 0.547 |
| `llama3.2:3b`, eager self-report | **1.781** |

A factor of four **between two modes of the same model**. The scripted client
prices a call at `len(text)/4` with no system prompt, no JSON scaffolding and no
source catalogue; the real self-report prompt carries all three.

**The repair is to stop hard-coding it.** `gate1.calibrate()` measures the ratio
from runs the deployment has already made. Evaluated
**leave-one-design-point-out** on the real data — calibrated on three design
points, tested on the fourth, never on itself:

| held out | arm | A_SCALE (calibrated) | acc | falseRST | falseREC |
|---|---|---|---|---|---|
| fan04-inf | eager | 1.832 | 100% | 0 | 0 |
| fan08-exp | eager | 1.832 | 100% | 0 | 0 |
| fan08-inf | eager | 1.730 | 100% | 0 | 0 |
| fan16-inf | eager | 1.730 | 100% | 0 | 0 |
| fan04-inf | lazy | 0.462 | **0%** | 0 | 3 |
| fan08-exp | lazy | 0.632 | 100% | 0 | 0 |
| fan08-inf | lazy | 0.462 | **0%** | 0 | 3 |
| fan16-inf | lazy | 0.632 | 100% | 0 | 0 |
| **total** | | | **75.0%** | **0** | **6** |

**25.0% → 75.0%, with zero false restarts.** Every remaining error is a false
recovery — the error bounded by `A` and still caught by Gate 2.

Realized cost on those 24 runs:

| policy | tokens | vs restart | vs oracle |
|---|---|---|---|
| always recover (today) | 53,688 | 1.58 | 1.84 |
| always restart | 33,936 | 1.00 | 1.16 |
| **GATE 1 (calibrated)** | **30,417** | **0.90** | **1.04** |
| ORACLE | 29,172 | 0.86 | 1.00 |

**43.3% saved against today's behaviour, 10% better than always-restart, within
4.3% of the oracle.**

---

## 8. Failure cases and limitations

1. **The lazy arm at small K is still wrong.** 6 false recoveries, all at
   `K ≤ 8` with lazy self-report, where `A/N ≈ 1` and estimator variance
   dominates the decision.
2. **A latent failure mode that this testbed cannot trigger.** Chain
   exposed-only cases show `f_struct = 0.85–0.91` against `f_true = 0.00` —
   massive over-prediction. The gate survives only because `A/N` there is
   1.17–1.56, so restart is correct anyway. **A run with wide exposure, no
   influence, and `A/N < ~1.0` would be an expensive false restart.** No such
   case exists here; it is an untested gap, not a refuted risk.
3. **Near break-even no gate works**, including this one (§4).
4. **Calibration requires history.** A deployment's first run has no measured
   `A_SCALE` and falls back to a constant that is known not to transfer.
5. **One real model, one workflow family** for the real-model evaluation; 24
   runs, 8 distinct configurations.
6. **`n = 3` per real design point**; the scripted sets are larger but scripted.

---

## 9. What can honestly be claimed

**Supported:**

- Gate 1 as implemented reduces total recovery cost against current behaviour
  by **43.3% on 24 real runs**, and against always-restart by 10%.
- On held-out scripted cases it is **97.0%** accurate with **0% false
  recoveries** and **zero token cost**.
- `P` is **not usable** as a recovery-vs-restart gate — measured, not assumed.
- Early-verdict rate is **not** an estimator of `f` (correlation 0.234), which
  falsifies the adaptive-budget family and undermines the SPRT's assumption.
- The structural prior **never under-predicted** across 132 cases.
- A wrong gate decision can never cost safety: declining to investigate leaves
  pairs `unchecked`, which the walk contaminates.

**Not supported, and not to be claimed:**

- That the gate works near the economic break-even point. It does not, and
  neither does anything else tested.
- That `A_SCALE` transfers across clients or configurations. It does not.
- That SPRT is unusable — it is *dominated here*, not disproven, and its
  assumption violation is argued structurally rather than shown to be fatal.
- Any claim about safety improvement. Safety was unchanged by construction and
  no case in 132 produced an unsafe preservation.
- Cross-model validity of any number here.
