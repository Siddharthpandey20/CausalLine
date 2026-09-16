# Gate 1 — hypothesis search after the structural falsification (environmental, no LLM)

**Outcome: (A) two hypotheses survive with a clean, quantified trade-off, and
neither dominates.** Both beat the falsified structural gate substantially
(94% vs 66% over 32 cases). They fail in *opposite directions*, and the
direction matters: H2 has zero false recoveries, H1 has zero false restarts.

Nothing in `src/recovery/` was modified. No LLM was run.

| | |
|---|---|
| cases | 32, across 4 disjoint splits |
| topologies | hub, multi-hub, fan-out, fan-in, chain |
| tests | **375 pass** (16 new) |
| production changes | **none** |

---

## 1. The problem after the structural gate was falsified

`targeted_real_llm_break_test.md` showed the failure mechanism exactly: when a
flagged source enters at a **hub** that everything else is downstream of,
`b2_topology_closure` returns the whole trace, `f_structural` saturates at
1.000, and the decision **stops depending on `A` at all**. No value of the
analysis estimate, however small, can produce an INVESTIGATE decision. Four
real-LLM cases discarded 1,622–6,106 tokens of fully recoverable work to avoid
an investigation costing 138–266.

The gap is not calibration. It is that **topology alone cannot distinguish
exposure from influence**, which is the project's own founding claim turned
against its own gate.

## 2. Repository capabilities relevant to a next hypothesis

Audited by tracing the code, not the docs.

| information | cost | status |
|---|---|---|
| event/call graph, `derived_from`, exposure edges | free | used by the falsified gate |
| `b2_topology_closure`, `structural_prior` | free | falsified as a stand-alone gate |
| per-event replay cost, `restart_all_cost` | free | used |
| detector verdict, flagged set | free (given) | used |
| **`removability.check()`** | **free** — pure string work on the stored prompt | **never used by any gate** |
| `signatures.carried_spans` — copied spans in an output | free | used by the carryover facet only |
| structural/code-path check records | free, written by the pipeline | exact verdicts for tool events |
| one counterfactual | 1 model call ≈ one event's cost | the unit of `A` |
| `group_test` | **one call per group**, recursive halving | a clean half of sixteen costs one call |
| SPRT, `config_for` | needs a verdict sequence | dominated (previous phase) |

**Do not resurrect without a genuinely different hypothesis:** run-probability
`P` (saturates, 0.988–0.998), adaptive-budget/peek (early tainted rate is not
`f`, correlation 0.234), SPRT (assumption violated; dominated on cost),
structural-only gating (falsified above).

## 3. Candidate hypotheses

### H1 — bottleneck probing (SELECTED)

**H:** one counterfactual at the pair whose clean verdict would remove the most
of the closure converts topology into evidence.
**PRE:** the closure and its cost weighting. **CHEAP:** one to three probes.
**DECISION:** `Â/N + f(after verdicts) ≤ 1`, and buy a probe *only if a clean
answer would flip the decision* (value of information).
**WHY:** a clean verdict at a dominating pair removes the dominated region
*exactly*, by the contamination walk's own semantics — not as an estimate.
**FAILURE:** redundancy, where the probe is blind; multiple independent
bottlenecks needing more probes than the budget.
**ECONOMIC:** `probe_cost + A + f·N < N`.
**SAFETY:** a clean verdict that is unsound becomes a preserved contaminated
region.

### H2 — soundness-guarded bottleneck probing (SELECTED)

**H:** H1, but a clean verdict is believed only when `removability.check()`
says the source was actually removable (D-066).
**CHEAP:** the guard is **free**; probes as H1.
**WHY:** leave-one-out is only valid when redaction removes the information.
Refusing invalid evidence is a soundness precondition, not a threshold.
**FAILURE:** when *nothing* is removable there is nothing it may probe, and it
falls back on the falsified structural bound.
**SAFETY:** conservative — an unsound verdict costs preserved work, never
safety.

### H3 — span propagation (tested as a free reference)

**H:** flagged spans appearing in downstream outputs distinguish influence from
exposure at zero cost.
**WHY/FAILURE:** sees copy-like influence; **blind to semantic influence** by
construction.

### H4 — group-testing as an ex-ante probe (considered, not selected)

`group_test` settles a clean half of sixteen in one call. Attractive, but it
answers "which of these mattered", and the gate's question is "does anything
upstream matter at all" — which is one pair, not a set. It would be the right
mechanism for a *second* stage, after a bottleneck probe says tainted.

### H5 — interval gate / bounded value of information (considered, folded in)

Compute `f_lower` (confirmed contaminated from free structural records) and
`f_upper` (closure); decide only when both agree. This is genuinely
threshold-free, but on inspection `f_lower` is ~0 on every trace here because
the free structural records cover tool events only. The *value-of-information
stopping rule* from it was kept and is what H1/H2 use to decide whether to buy
a probe at all.

### H6 — downstream response divergence (considered, rejected)

Compare sibling branches' outputs: if branches that saw the flagged source
agree with branches that did not, influence is unlikely. Rejected because it
requires a clean comparison group that a hub topology does not provide — every
branch is downstream of the hub, so there is nothing to compare against.

## 4. The environmental model

`src/eval/gate1_sim.py`. Builds **real `Trace` objects**, so `contaminate()`,
`b2_topology_closure`, `structural_prior`, `restart_all_cost` and the planner
all run unmodified. Only the generation is synthetic.

**Ground truth is held outside the trace.** A gate learns about influence in
exactly one way: `ProbeOracle.probe(...)`, which charges the event's cost. Two
assertions pin this (`tests/test_gate1_sim.py`): the planted relation never
appears in the trace, and a gate that decides without paying fails the test.

**Three distinct relations, and the differences are the experiment:**

```
influences(s, e)     s genuinely changed e's output          -- the truth
detectable(s, e)     a leave-one-out counterfactual can SEE it
carries_span(s, e)   the change left copied text behind
```

Under **redundancy** `detectable` is empty while `influences` is not — real
influence no probe can see (`docs/03` #16). Under **semantic** influence
`carries_span` is empty — invisible to any free text check.

`removable` is modelled as an **observable**, not as truth, because
`removability.check()` really is free string work on the stored prompt.

Topologies: `hub`, `multihub`, `fanout`, `fanin`, `chain`. Patterns: `none`,
`one`, `all`, `redundant`, `deep`, `semantic_one`, `none_blocked`,
`deep_hidden`. Each consumer retrieves its own source, so the entry event sits
inside that agent — without this, `f_structural` is 1.00 in every topology and
the experiment is vacuous.

## 5. Experimental matrix and splits

Split **by design**, not at random. 32 cases.

| split | n | purpose |
|---|---|---|
| DEVELOPMENT | 9 | the brief's families A–J at K=8/16 |
| HELD-OUT | 9 | same families, K=12/24, different hub counts |
| ADVERSARIAL-1 | 8 | deep influence, semantic influence, redundancy, multi-hub |
| ADVERSARIAL-2 | 6 | built **after** H2 existed, to break H2 specifically |

## 6. Results

### Per split

| split | gate | acc | fRST | fREC | probe tok | vs restart | unsafe |
|---|---|---|---|---|---|---|---|
| DEV | G_STRUCT | 89% | 1 | 0 | 0 | 0.59 | 0 |
| | H1 | 89% | 0 | 1 | 700 | 0.57 | 9 |
| | H2 | **100%** | 0 | 0 | 600 | 0.55 | **0** |
| HELD-OUT | G_STRUCT | 78% | 2 | 0 | 0 | 0.58 | 0 |
| | H1 | **100%** | 0 | 0 | 700 | 0.34 | 0 |
| | H2 | **100%** | 0 | 0 | 700 | 0.34 | 0 |
| ADV-1 | G_STRUCT | 62% | 3 | 0 | 0 | 0.81 | 2 |
| | H1 | 88% | 0 | 1 | 600 | 0.61 | 22 |
| | H2 | **100%** | 0 | 0 | 500 | 0.60 | **5** |
| ADV-2 | G_STRUCT | 17% | 5 | 0 | 0 | — | 0 |
| | H1 | **100%** | 0 | 0 | 500 | — | 3 |
| | H2 | 67% | **2** | 0 | 300 | — | 3 |

### All 32 cases combined

| gate | acc | fRST | fREC | probe cost | vs restart | vs oracle | unsafe |
|---|---|---|---|---|---|---|---|
| B0 always restart | 31% | 22 | 0 | 0% | 1.00 | 2.63 | 0 |
| B1 always investigate | 69% | 0 | 10 | 0% | 0.48 | 1.26 | 34 |
| **G_STRUCT (falsified)** | **66%** | **11** | 0 | 0% | 0.69 | 1.81 | 2 |
| **H1 bottleneck probe** | **94%** | **0** | **2** | **6.2%** | **0.41** | **1.09** | 34 |
| **H2 sound bottleneck** | **94%** | **2** | **0** | **5.2%** | **0.49** | **1.29** | **8** |
| H3 span (free) | 81% | 0 | 6 | 0% | 0.40 | 1.07 | 34 |

**The probe is cheap: 5–6% of a restart**, and it converts 66% accuracy into
94%.

## 7. Adversarial counterexamples

**Against H1 — redundancy at the probed pair (X5, E).** The probe returns
clean because a second source carries the same material. H1 believes it,
investigates, and 17 contaminated events are preserved. **Confirmed failure.**

**Against H2 — nothing removable (`none_blocked`, Y1/Y2).** With no soundly
probeable candidate, H2 has nothing to buy and falls back on the structural
bound, reproducing the original falsification: **2 false restarts** on cases
where `A/N = 0.04–0.11` and `f_true = 0`. **Confirmed failure, and predicted
before the run.**

**Against H3 — semantic influence (X3, X4).** Influence with no copied span is
invisible; H3 says INVESTIGATE where the oracle says RESTART. **Confirmed.**

**Not a counterexample: `deep_hidden` (Y3, Y4, Y6).** Both H1 and H2 decide
correctly; the 1 unsafe event each is produced by the *investigation*, and B1
produces exactly the same 1.

## 8. Safety

The decisive structural fact, asserted as a test:

> A gate only chooses *whether* to investigate. It cannot introduce an unsafe
> preservation the investigation would not have produced anyway; a gate that
> restarts has zero by construction.

Measured: B1 (always investigate) is the ceiling at 34 unsafe over 32 cases.
H1 reaches that ceiling exactly (34) — it exposes every blind spot. **H2 sits
at 8**, avoiding 26 of them, because its removability guard declines to
investigate precisely the cases where the probe is unsound.

**On the brief's own safety ordering — false recovery is much worse than false
restart — H2 is the preferred candidate**: 0 false recoveries against H1's 2,
paid for with 2 false restarts.

## 9. Novelty check

Distinct from detection (this starts after), from structural/checkpoint/memory
rollback (that is B0), from ordinary influence attribution (that is the `A`
this gate decides whether to spend), and from generic counterfactual debugging
(which explains a fault rather than pricing a decision).

The mechanism-level claim, stated narrowly: **using the contamination walk's
own semantics to turn a single counterfactual verdict into an exact reduction
of a cost bound, buying that verdict only when a clean answer would flip the
economic decision, and refusing to believe it when the repository's own
removability precondition says leave-one-out is unsound.**

We are not claiming this is novel against the literature — no literature search
was performed in this phase. It is distinct from the other mechanisms *in this
repository*, which is all that is established.

## 10. What remains unsupported

1. **H2's headline numbers are not a clean held-out result.** H2 was designed
   after seeing H1 fail on redundancy in DEV and ADV-1. The only genuinely
   held-out set for H2 is ADV-2, where it scores **4/6**.
2. **No real model.** The probe oracle is deterministic and exact; a real
   counterfactual is noisy, and D-026's noise floor exists for that reason.
3. **Constant `EVENT_COST`.** Real per-event costs vary by an order of
   magnitude, and `Â` was the term that broke calibration transfer before.
4. **32 synthetic cases, one author.** The patterns are the ones I thought to
   construct; an unimagined topology is not covered.
5. **Neither gate handles the `none_blocked` regime.** H1 does, by trusting
   unsound evidence — which is not a solution, it is the other error.
6. **No claim that either gate is safe.** Safety here means "adds no unsafe
   preservation beyond the investigation's own", which is weaker than safe.

## 11. Recommended next research direction

**Not** to implement either gate in production yet. Two things should come
first, in this order:

1. **Establish whether `removability.check()` fires correctly on real traces.**
   H2's entire advantage rests on it, and it has never been measured as a
   *predictor* of unsound verdicts — only used as a precondition inside the
   estimator. If it is noisy on real prompts, H2 collapses toward H1.
2. **Resolve the `none_blocked` regime honestly.** When nothing is soundly
   probeable, neither trusting the probe (H1) nor falling back on the closure
   (H2) is right. The interesting option is a third action the system does not
   currently have: **restart only the un-probeable region** rather than the
   whole workflow. That is a planner question, not a gate question, and it may
   be where the real answer lives.

A useful negative to keep in view: `f_lower` is ~0 on every trace here because
free structural records cover only tool events. Any interval-based gate needs a
source of *confirmed* contamination that does not cost a model call, and this
repository does not currently have one.

---

## Verdict

**Outcome (A), qualified.** Two mechanisms genuinely distinct from the
falsified structural gate both raise accuracy from 66% to 94% at a probe cost
of 5–6% of a restart, and each has a *known, reproduced* failure mode in the
opposite direction from the other. That is a result worth carrying forward —
and it is not a solution. H2's advantage is not yet measured on data it was
not designed against, beyond 6 cases, and nothing here has touched a real
model.
