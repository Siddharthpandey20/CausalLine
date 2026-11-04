# 15 — The cost direction, reopened: the loss that closed it is inside the noise

**Verdict: the suspected mechanism is REJECTED, because its premise is false.**
Closure-scoped deferred self-report was proposed to recover work that deferral
was believed to cost. Replicated across 30 seeds, deferral does not cost that
work. The mechanism is unnecessary, and the saving it was meant to unlock is
already available through `lazy` (D-082), which has been implemented and off by
default since Phase 3.

**No production default changed.** `src/provenance/`, `src/recovery/` and
`src/eval/experiment.py` are untouched by this document. What changed is what
we are entitled to say.

| | |
|---|---|
| new LLM runs | **0** |
| scripted matrices run | **180** (30 seeds x 3 arms x 2 randomness regimes) |
| what moved | the *interpretation* of a −3.6 point figure, not any code |
| raw results | `data/results/seedsweep/sweep-30.json`, `sweep-30.txt` (committed 05-11-2026; previously local-only under `data/runs/`) |
| replicated at 100 seeds | `data/results/seedsweep/sweep-100.json` — see [`16-six-investigations.md`](16-six-investigations.md) section 6. The interval narrowed as predicted and the verdict held |

---

## 1. What was suspected, and why

`gate1/final_cost_direction.md` found, and proved on 290 of 290 traces, that
**28–68% of self-report calls land outside the B2 closure of the flagged set**
and are provably irrelevant to the recovery outcome. Self-report is **80% of
all analysis cost**; attribution is **62–83% of total cost**. So the pocket is
large and the irrelevance is exact, not heuristic.

It could not be taken, because identifying an out-of-closure event requires the
detector's flagged set, which requires a finished run. "Skip it" is therefore
only implementable as "defer it". And deferral, measured once, cost preserved
work:

| mode | analysis | recovery | work preserved |
|---|---|---|---|
| hybrid (baseline) | 4500 | 5300 | **78.9%** |
| lazy (frontier scope) | 2500 | 3500 | 75.4% |
| closure scope | 3200 | 4100 | 77.2% |

Closure scoping recovered about half the loss and still sat 1.7 points below
baseline, which the hard constraint *preserved work ≥ baseline* makes a failure.
The stated cause was that deferral changes the pipeline's own provenance
records.

**That cause was retracted** in the same document's §6.1 correction
(17-09-2026), pinned by `tests/test_provenance_independence.py`: execution-time
structural records are identical with and without attribution, **16 = 16**. The
original "25 vs 18" had counted `carrier` records — pointers to upstream
verdicts (D-067), attribution-derived by design — together with `structural`
ones. The correction says plainly that the residual gap is *"still unexplained
and is the open question, not the settled one."*

So the live hypothesis was: **with that blocker gone, closure scoping should be
outcome-identical, and the 28–68% saving becomes available.** This document
tests it.

## 2. First, the baseline had moved

Re-measured on current code (`python -m src.eval.lazy_selfreport`, scripted,
deterministic, free):

| arm | docs | now |
|---|---|---|
| hybrid | 78.9% | **73.7%** |
| lazy | 75.4% | **70.2%** |
| targeted_only | 73.7% | **69.3%** |

Every figure fell 4.6–5.3 points, which is exactly the Issue #20 ingestion-edge
correction (`docs/12`). The *gap* was unchanged: −3.6 became −3.5.

## 3. The gap is two cells, and one pair

Per cell (6 CausalLine cells, oracle detector):

| cell | hybrid | lazy | Δ |
|---|---|---|---|
| A/influencing | 0.421 | 0.421 | 0 |
| A/exposed_only | 0.947 | 0.947 | 0 |
| B/influencing | 0.579 | 0.579 | 0 |
| **B/exposed_only** | 0.947 | **0.842** | **−0.105** |
| C/influencing | 0.579 | 0.579 | 0 |
| **C/exposed_only** | 0.947 | **0.842** | **−0.105** |

The whole −3.5 points is two cells, each losing 2 events of 19. In both, one
pair differs:

```
S13 -> e0013   hybrid=('clean','counterfactual')   lazy=('tainted','self_report')
```

`exposed_only` is the control variant: the flagged source is present and must
never influence. Hybrid's counterfactual established *clean*. Lazy accepted a
**false-positive self-report** — which, being a positive, settles the pair and
so never buys the counterfactual that would have cleared it.

**This is not a scope problem.** Of the events hybrid asked about and lazy did
not, 0 (B) and 3 (C) were inside the closure — and the pair that actually
flipped was asked about by *both* arms. Closure scoping cannot fix a verdict
that differed on a question both arms asked. The hypothesised mechanism was
aimed at the wrong thing.

## 4. The two arms were never asking the same coin

`ScriptedClient._answer_self_report` draws its lie from `self._rng`, a single
stream shared with the task answers:

```python
lie = self._rng.random() < (self.self_report_false_negative_rate  # 0.25
                            if actually
                            else self.self_report_false_positive_rate)  # 0.10
```

The draw a given (event, source) pair receives therefore depends on **how many
draws came before it** — and that differs between the arms by construction: the
inline arm makes six self-report calls during the pipeline, the deferred arm
makes them later, interleaved with counterfactual replays. So a single-seed
comparison of hybrid against lazy is not a paired comparison. It is two
different coins.

Re-run with the lie keyed to `(seed, audited output, source id)` instead of to
call order — identical marginal rates, common random numbers, nothing in the
repository modified:

| | as shipped | paired |
|---|---|---|
| hybrid | 73.7% | 70.2% |
| lazy | 70.2% | 68.4% |
| **gap** | **−3.5** | **−1.8** |

Half the gap is the unpaired randomness. The per-cell signs then scatter in
**both** directions — one cell moves **+0.211 in lazy's favour**, another
−0.263 — and the baseline's own level moves 3.5 points between regimes. Six
cells at one seed cannot resolve an effect of this size.

## 5. Thirty seeds

30 seeds x 3 arms x 2 randomness regimes = 180 matrices. Paired per-seed
differences, in percentage points of work preserved:

### As shipped (the randomness the repository actually uses)

| arm | work preserved | analysis tok | total tok | unsafe | pair FN |
|---|---|---|---|---|---|
| hybrid | 69.4% (sd 3.91) | 4637 | 10213 | **0** | 9 / 870 |
| lazy | 69.7% (sd 5.11) | 2627 | 6187 | **0** | 8 / 272 |
| targeted_only | 69.3% (sd 0.00) | 1500 | 3900 | **0** | **30 / 210** |

| paired difference | mean | sd | 95% CI | crosses 0 | W/T/L |
|---|---|---|---|---|---|
| lazy − hybrid, work preserved | **+0.32** | 6.40 | [−1.97, +2.61] | **yes** | 17/4/9 |
| lazy − hybrid, total tokens | **−4027** | 545 | [−4222, −3832] | **no** | 0/0/30 |

### Paired randomness (common random numbers)

| paired difference | mean | sd | 95% CI | crosses 0 | W/T/L |
|---|---|---|---|---|---|
| lazy − hybrid, work preserved | **−0.15** | 5.74 | [−2.20, +1.91] | **yes** | 14/2/14 |
| lazy − hybrid, total tokens | **−3807** | 555 | [−4005, −3608] | **no** | 0/0/30 |

Task success was **180/180 for every arm in both regimes**.

## 6. What this establishes, and what it does not

**Established.**

1. **The −3.6 point chain-matrix quality loss is not reproducible.** Over 30
   seeds the paired difference is +0.32 (as shipped) or −0.15 (paired), both
   with confidence intervals spanning zero. The historical −3.6 lies **outside**
   both intervals. It was one draw from a distribution with a per-seed standard
   deviation of 6.4 points.
2. **The token saving is real and robust**: −39.4% of analysis+recovery as
   shipped, −37.4% paired, confidence interval nowhere near zero, **30 of 30
   seeds**, no exceptions.
3. **Safety is untouched.** Zero unsafe preservations, every arm, every seed,
   both regimes — 180 matrices.
4. **`targeted_only` remains a failure**, and the replication makes the reason
   sharper: it preserves the *same* work (69.3%) while committing **30 pair
   false negatives against the baseline's 9**. Work preserved alone does not
   discriminate between these arms; recall does.

**Not established, and stated because it cuts against the convenient reading.**

1. **"No difference" is not "no effect."** With sd 6.4 and n=30 the interval is
   ±2.3 points. This excludes a 3.6-point loss. It does **not** exclude a loss
   of up to about 2 points. A 100-seed run was started to narrow this and was
   stopped for machine contention; it is the obvious next measurement.
2. **The pair-false-negative denominators are not comparable.** Hybrid scored
   870 pairs and missed 9 (1.0%); lazy scored 272 and missed 8 (**2.9%**). Lazy
   examines far fewer pairs, so the absolute counts are equal while the rate is
   roughly three times worse. The absolute count is what safety depends on —
   and event-level unsafe preservation stayed 0 because the walk contaminates
   whatever it has not examined — but the rate is a real difference and should
   not be rounded away.
3. **The literal acceptance condition "pair false negatives must remain zero"
   is not met by the baseline either**: hybrid commits 7–9 across 30 seeds. The
   operative condition is *no worse than baseline*, which `lazy` meets (8 vs 9
   as shipped, 8 vs 7 paired).
4. **This is the scripted chain matrix only** — 6 cells, one topology, one
   detector. The fan-out result (72% cheaper, outcome-identical on 12/12 paired
   real runs) is unaffected and still stands on its own evidence.
5. **Closure scoping was never implemented**, because the diagnosis removed its
   reason to exist. If a future workload does show a reproducible scope-shaped
   loss, `src/eval/closure_scoped.py` is still the place to start.

## 7. Consequences for the record

| Claim, as written | Status |
|---|---|
| `gate1/cost_without_quality_loss.md` §4: lazy "recovers 3.6 points less work" on the chain matrix | **not reproducible**; +0.32 [−1.97, +2.61] over 30 seeds |
| same, §6: the reach-preserving / reach-reducing framing | **still sound as a mechanism**, but it does not follow that the chain matrix shows a measurable loss |
| `gate1/final_cost_direction.md` "Outcome B: cost-reduction objective not supported" | **rests on a difference inside the measurement error**. The objection it records is not supported by replication |
| `11-phase5-report.md` Phase 3: "costs 3.6 points of work preserved on attacked ones" | **same figure, same status** |
| `overview.md` §5.5: lazy is "−3.6 pts on the chain workflow ... per-workload, not global" | **the per-workload restriction is not supported** by this replication |
| Gate 1 is not part of the contribution | **unchanged.** Nothing here rehabilitates Gate 1, which failed on preserved work, not on noise |
| every safety number | **unchanged**, and reconfirmed on 180 matrices |

## 8. What should happen next

1. **100 seeds**, to narrow ±2.3 points to about ±1.3. Free, deterministic,
   roughly an hour on an otherwise idle machine.
2. **Consider making the scripted client's self-report deterministic per
   (seed, event, source)** rather than per call order. It preserves the
   marginal error rates exactly and makes every A/B on this matrix paired. It
   would change every scripted number in the repository, so it is a deliberate
   re-baselining and not a quiet fix — which is why it was done in a harness
   here and not in `src/eval/scripted.py`.
3. **Do not flip `lazy` on by default on this evidence.** The honest position is
   that it is *not shown to cost preserved work* and *is shown to save ~39% of
   tokens* — which is grounds to re-run the fan-out acceptance test on more
   workloads, not grounds to change a default on a 6-cell matrix.

## 9. Reproducing

```bash
# the current three-arm comparison (what section 2 reports)
python -m src.eval.lazy_selfreport

# the per-pair diagnosis of section 3, and the paired-randomness
# comparison of section 4 and the sweep of section 5, are harness scripts
# kept out of src/ deliberately: they monkey-patch the scripted client for a
# measurement and must never be importable by the production evaluation.
# data/runs/seedsweep/sweep-30.json holds the 180-matrix output.
```
