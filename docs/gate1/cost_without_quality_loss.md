# Same recovery, lower cost — what actually qualifies

**Result: one mechanism qualifies on one workload, and it must be validated per
workload rather than adopted globally.**

Under the constraint *same correctness, same recall, same safety, lower cost*,
almost everything this project has built is disqualified — including Gate 1,
the thing three phases were spent on. What survives is a **72% analysis-cost
reduction with byte-identical recovery outcomes on 12 paired real runs**, which
**does not** hold on a second workload.

| | |
|---|---|
| paired real runs (same seeds, same design points) | 12 |
| real traces re-read | 54 |
| new LLM runs | **0** |
| tests | **393 pass** (8 new) |
| `src/recovery/` changes | **none** |
| production default | unchanged — baseline is still the default path |

---

## 1. Why Gate 1 is disqualified

`BASELINE` = the existing full investigation + recovery, no gate.

| mechanism | unsafe | work preserved | total cost | vs BASELINE |
|---|---|---|---|---|
| **B_BASELINE (no gate)** | 141 | **109,600** | 67,800 | 1.00 |
| B0 full restart | 0 | 0 | 139,000 | 2.05 |
| Gate 1 `G_STRUCT` | 58 | 74,500 | 80,900 | **1.19** |
| Gate 1 `H1 bottleneck` | 141 | 101,800 | 64,100 | 0.95 |

Gate 1 **cannot violate constraints 1–3**: a RESTART decision recomputes
everything, so recall is 1.0 and unsafe is 0 by construction. That is exactly
why it is the wrong tool here — it buys its safety by **destroying preserved
work**, which is the product.

- `G_STRUCT` costs **1.19× the baseline** and loses 35,100 tokens of preserved
  work. It is worse on both axes.
- `H1` saves 5% and preserves less work.

**Gate 1 is not a cost optimization of the baseline. It is a different, more
conservative recovery policy that happens to be cheaper sometimes.**

## 2. What was enumerated, and what each costs in quality

| lever | cost effect | quality effect | verdict |
|---|---|---|---|
| Gate 1 (restart early) | +19% / −5% | destroys preserved work | **disqualified** (§1) |
| SPRT abort / budget caps | cheaper | leaves pairs unchecked → contaminated → less work preserved | disqualified, same mechanism |
| `targeted_only` (drop self-report) | −67% analysis | **1 pair false negative — recall dropped** | **FAILURE** (constraint 7) |
| group testing | — | — | **already maximal**: measured 1 counterfactual per event, **0 duplicates** across 175 calls. No headroom |
| prompt caching | billing only | **none by construction** | qualifies, but see §5 |
| **lazy self-report** | **−72%** on fan-out | **identical** on fan-out, **−3.6 pts** on the chain matrix | **§3 — workload-dependent** |

## 3. The one mechanism that qualifies, and where

12 paired real runs, `llama3.2:3b`, identical design points and seeds.
Baseline = eager self-report (the shipped default).

| case | A baseline | A cheaper | saving | preserved | blast | unsafe |
|---|---|---|---|---|---|---|
| fan04-inf ×3 | 1647 | 850 | 48% | 0.79 / **0.79** | 3 / **3** | 0 / **0** |
| fan08-exp ×3 | 2675 | 412 | 85% | 1.00 / **1.00** | 0 / **0** | 0 / **0** |
| fan08-inf ×3 | 2851 | 983 | 66% | 0.88 / **0.88** | 3 / **3** | 0 / **0** |
| fan16-inf ×3 | 5248 | 1246 | 76% | 0.94 / **0.94** | 3 / **3** | 0 / **0** |
| **total** | **37,263** | **10,473** | **72%** | identical | identical | identical |

**Zero cases where the cheaper variant preserved less work, discarded a smaller
region, or was less safe.** Against the acceptance criteria:

1. preserves the baseline's correctness guarantees — **yes, outcome-identical**
2. recall 1.0 — **yes**
3. zero unsafe introduced — **yes**
4. existing flow intact as fallback — **yes**, still the default
5. measurably reduces cost — **yes, 72%**

**SUCCESS, on this workload.**

## 4. And where it does not hold

Re-measured on the scripted chain matrix (`src/eval/lazy_selfreport.py`, free,
deterministic):

| metric | hybrid (baseline) | lazy | targeted_only |
|---|---|---|---|
| analysis tokens | 4500 | 2500 | 1500 |
| recovery tokens | 5300 | 3500 | 2400 |
| **work preserved** | **79.0%** | **75.4%** | 73.7% |
| unsafe preservations | 0 | **0** | 0 |
| **pair false negatives** | 0 | **0** | **1** |
| escalations | 0 | 0 | 1 |

- **`targeted_only` is a FAILURE**: one pair false negative is a recall loss,
  and constraint 7 says that is a failure regardless of the 67% saving.
- **`lazy` keeps recall and safety** (0 false negatives, 0 unsafe) but recovers
  **3.6 points less work**. Under constraint 4 — *do not trade recovery quality
  for token savings* — that disqualifies it **on this workload**.

The repository's own tooling already says so, unprompted:

```
VERDICT
  lazy reproduces hybrid on the attacked matrix: NO -- the deferral changed an answer
```

**So the honest scope is: outcome-identical and 72% cheaper on the fan-out
workload; outcome-changing on the chain matrix.** It is a per-workload
validation, not a global switch — which is why it remains off by default.

### Why the two workloads differ

On fan-out, an analyst's context is one document and one planted note, so the
frontier reaches every event that matters and deferral changes nothing but
timing. On the chain, events carry many sources and the frontier stops earlier,
so some pairs never get their self-report and stay `unchecked` — which the walk
contaminates. The deferral does not lose information; it loses *reach*.

## 5. Prompt caching — the only structurally-identical lever, and its ceiling

Self-report is **80% of all analysis cost** (360 calls / 105,960 tokens against
the counterfactuals' 175 / 25,853), and **71.3% of analysis tokens are prompt**.

Measured against the shipped template over 141 real self-report calls:

| | tokens | share |
|---|---|---|
| CONSTANT (system + template skeleton) | 29,046 | **92.5%** |
| VARIABLE (kind + output + catalogue) | 2,370 | 7.5% |

**But the contiguous prefix before the first variable field is 4 tokens.**
Prefix caching can only hold a prefix, so the realistically cacheable part today
is the **88-token system prompt** — about 39% of a 223-token call — not the
92.5%. Capturing the rest would require reordering the template so the constant
text precedes the variable fields, and **that is a prompt change that could
alter answers**, so it is not the zero-risk billing change the rest of this
section describes.

Not measured end to end: the local backend does not bill for a cached prefix, so
the saving is arithmetic from the token counts, not an observed invoice.

## 6. What this means for the research question

> Can we obtain the SAME recovery quality as the current expensive pipeline for
> LOWER cost?

**On the fan-out workload, yes: 72% less analysis, identical outcomes.** On the
chain workload, the same mechanism costs 3.6 points of preserved work, and the
only lever that is cheaper still costs a unit of recall.

The generalisable finding is narrower than either number:

> The cost reductions available here are **reach-preserving or
> reach-reducing**, and which one a given mechanism is depends on the workflow
> shape, not on the mechanism. Deferring the self-report changes nothing when
> the frontier reaches every event anyway, and changes outcomes when it does
> not. That is a property of the trace, is measurable per workload before
> adoption, and cannot be settled once globally.

## 7. Limitations

1. **12 paired runs, one model, one workflow family** for the positive result.
2. The fan-out runs were **deterministic** — repeats returned identical
   `(N, A, R)` — so the 12 runs are 4 design points.
3. The chain comparison is scripted, not real-model.
4. Caching savings are arithmetic, not observed on a billing provider.
5. `work_preserved` is treated as "recovery quality". Under a narrower reading
   where only recall and unsafe count, `lazy` would pass on both workloads.
   The stricter reading is used throughout because constraint 4 names recovery
   quality explicitly.
6. Nothing here tests whether the *baseline itself* is correct; it is taken as
   the reference by construction.

## 8. Recommendation

**Do not adopt any of these globally, and do not adopt Gate 1 at all.**

1. Keep the baseline as the default. It still is.
2. Treat the deferral as a **per-workload optimization with an acceptance
   test**: run both arms on a handful of representative traces and adopt it only
   where the recovery outcome is identical, as it was here. The test is cheap —
   the fan-out validation cost 12 runs — and is now encoded in
   `tests/test_cost_without_quality_loss.py`.
3. Take the prefix-caching win (system prompt, ~39% of self-report prompt) since
   it is semantically free; leave the template reordering alone unless someone
   is prepared to re-validate every verdict after it.

---

## VERDICT

**Partial success, precisely scoped.**

- Gate 1: **disqualified** — costs 1.19× the baseline and destroys preserved
  work.
- `targeted_only`: **FAILURE** — recall dropped.
- Group testing: **already maximal**, no headroom.
- Lazy self-report: **SUCCESS on the fan-out workload** (72% cheaper,
  outcome-identical on 12/12 paired runs); **disqualified on the chain matrix**
  (−3.6 points of preserved work). Per-workload, not global.
- Prompt caching: qualifies structurally; realistic ceiling ~39% of self-report
  prompt without a prompt change; unmeasured on a billing provider.

There is no mechanism here that is both cheaper and outcome-identical
*everywhere*. That is the honest answer to the question as posed.
