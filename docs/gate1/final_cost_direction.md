# Final cost direction — the decision

## OUTCOME B — the original cost-reduction objective is not currently supported

> **Under the current architecture and available evidence, we have not found a
> defensible way to reduce investigation/recovery cost while preserving the
> baseline outcome in general.**

One narrow exception stands and is already documented: on fan-out-shaped
workloads the deferred self-report is outcome-identical and 72% cheaper
(12/12 paired real runs, `cost_without_quality_loss.md`). It is a per-workload
result with a cheap acceptance test, not a general optimization.

The two architectural changes that would make the problem tractable are in §6.
Both are small, both are about *when information is recorded*, and neither is an
estimator change.

| | |
|---|---|
| real traces analysed | 290 |
| new LLM runs | **0** |
| tests | **396 pass** (3 new) |
| `src/recovery/`, `src/provenance/`, `src/eval/experiment.py` | **pristine** — the failed mechanism was reverted |

---

## 1. What was searched, and what was measured

The question was reframed as an optimization problem: *where is the baseline
doing work it does not need?*

| direction | measured | verdict |
|---|---|---|
| duplicate model calls (memoization) | **0 duplicate prompts** across 1,383 model calls; the content store is content-addressed so this is exact | **no headroom** |
| group testing | **exactly 1 counterfactual per event, 0 duplicates** across 175 calls | **already maximal** |
| replay / splicing | already reuses logged outputs for every non-invalidated event | **already maximal** |
| prompt caching | 92.5% of the self-report prompt is constant, but the contiguous prefix before the first variable field is **4 tokens** | ceiling ≈ the 88-token system prompt (~39%); the rest needs a template reorder, which is a prompt change |
| **skipping provably-irrelevant work** | **68% of self-report calls (62% of spend) land outside the closure** | **§2–§4 — the one real find, and it failed** |

## 2. The find: work that provably cannot matter

Self-report is **80% of all analysis cost** (360 calls / 105,960 tokens against
the counterfactuals' 175 / 25,853). The baseline asks it on **every model event
of every run**, during the pipeline, before anyone knows what the detector will
flag.

Measured: **68% of those calls (62% of spend) land on events outside the B2
closure of the flagged set** — events contamination can never reach.

**The irrelevance is exact, not heuristic.** Taking each real trace, dropping
every influence and check record on an out-of-closure event, and recomputing the
contaminated region:

```
traces with check records : 290
recovery region IDENTICAL : 290
recovery region CHANGED   :   0
```

Across everything on disk: **529 of 1,639 self-report calls (32%) and 121,707 of
432,150 tokens (28%)** are provably irrelevant to the recovery outcome — 62–68%
on the fan-out originals specifically.

### The invariant it rests on, measured and self-checkable

Contamination propagates from flagged sources along influence and `derived_from`
edges; the closure is agent-reachability from where those sources entered. That
the first is always inside the second is **not proved here — it is measured**:

- **0 escapes on 290 real traces**
- **0 escapes on all three attack channels** of the scripted matrix (web,
  **memory**, agent_message) — memory is the case where a cross-agent flow might
  have no call-graph parent edge

It is also cheaply checkable at runtime, so a topology that violated it would be
caught rather than silently mis-recovered. Both facts are pinned by
`tests/test_cost_without_quality_loss.py`.

## 3. Why the saving cannot be taken

**The work cannot be skipped without also being deferred**, and deferral is not
free.

The closure requires the detector's flagged set, which requires a completed run.
The self-report runs *during* the pipeline. So "skip the out-of-closure calls"
is only implementable as "defer the self-report to after detection, then ask
about the closure only".

That was implemented and tested. On the chain matrix, where the frontier-scoped
deferral (D-082's lazy) is known to lose 3.6 points:

| mode | analysis | recovery | **work preserved** | unsafe | pair FN |
|---|---|---|---|---|---|
| **hybrid (BASELINE)** | 4500 | 5300 | **78.9%** | 0 | 0 |
| lazy (frontier scope) | 2500 | 3500 | 75.4% | 0 | 0 |
| **closure scope** | 3200 | 4100 | **77.2%** | 0 | 0 |

Closure scoping recovers about half the lost work — and **still lands 1.7 points
below the baseline**. Under the hard constraint *preserved correct work ≥
baseline*, that is a **FAILURE**. The mechanism was discarded, not tuned.

## 4. Root cause, measured

The residual gap is **not** caused by scope. It is caused by deferral itself.

Running the same scenario with and without an inline attributor, **before any
refinement**:

| | inline attributor | no attributor |
|---|---|---|
| check records | 49 | 37 |
| **structural records** | **25** | **18** |
| **influence edges** | **21** | **3** |

**The pipeline's own provenance records depend on whether an attributor was
installed during execution** — not merely on what that attributor concluded.
Deferring therefore changes the trace *before* any scoping decision is made, and
no amount of scoping can recover it.

A second confirmation: post-hoc attribution rebuilt through the production
`request_for` produced **41 records against the inline pass's 49**, and wrote
`clean` where the baseline wrote `tainted` — the unsafe direction. The trace as
stored does not contain everything the inline pass saw.

## 5. Why this closes the direction

The saving is real, exact, and unreachable:

```
provably irrelevant work          28-68% of self-report cost   PROVEN (290/290)
                                             |
                              requires deferral to identify
                                             |
deferral changes the pipeline's records      17 structural, 18 influence edges
                                             |
                                    costs preserved work       78.9% -> 77.2%
```

Every other lever is either already maximal (group testing, splicing,
memoization) or is a billing change with a 39% ceiling (caching).

## 6. The architectural changes that would make it tractable

Both are small, both concern *when information is recorded*, and both would
unlock a saving that is **already proven not to affect the outcome**.

### 6.1 Make the pipeline's provenance records independent of attribution

Today, installing an attributor changes the structural records the pipeline
writes (25 vs 18) and the influence edges (21 vs 3). If execution-time recording
were fixed — the pipeline always writing the same structural facts, with
attribution a pure *consumer* of them — then deferring the self-report would
move only the self-report call. Closure scoping would then be exactly
outcome-identical, and the 28–68% saving becomes available.

This is the higher-value change and the smaller one.

### 6.2 Persist the attribution request at execution time

`request_for` cannot rebuild what the inline pass saw, which is why post-hoc
attribution produced `clean` where the baseline produced `tainted`. Storing the
request the attributor was given — it is already constructed at execution time —
would make post-hoc attribution faithful, and therefore freely schedulable:
before detection, after detection, or not at all.

## 7. What this phase did not do

- No real-LLM runs. The equivalence was established exactly by recomputation on
  traces already on disk, so spending tokens would have added nothing.
- No production change survives. The experimental `self_report_scope="closure"`
  hook was reverted once the mechanism failed; `src/recovery/`,
  `src/provenance/` and `src/eval/experiment.py` are byte-identical to before.
- No claim that the closure invariant is a theorem. It is measured on 290
  traces and three channels, and it is checkable at runtime.

## 8. Limitations

1. The closure invariant is empirical. A pipeline that hands information
   between agents without a cross-agent `parent` edge could break it; none of
   the three channels here does.
2. The 78.9% → 77.2% comparison is the scripted chain matrix (6 CausalLine
   rows), deterministic but small.
3. The 68%/62% skippable figures are from the fan-out originals; across all
   traces on disk the figure is 32%/28%.
4. Prompt-caching savings are arithmetic from token counts, never observed on a
   billing provider.

---

## DECISION

**B — ORIGINAL COST-REDUCTION OBJECTIVE NOT CURRENTLY SUPPORTED.**

There is one genuine, exactly-quantified pocket of unnecessary work — 28–68% of
self-report cost, proven irrelevant to the recovery outcome on 290 of 290 real
traces. It cannot be taken, because identifying it requires deferring
attribution, and deferral changes the pipeline's own provenance records before
any scoping decision is made, costing 1.7 points of preserved work against a
baseline that must not be degraded.

Every other lever is already maximal or is a billing change.

**Required to make it tractable:** make the pipeline's provenance records
independent of whether attribution runs (§6.1), and persist the attribution
request at execution time (§6.2). Until one of those lands, the honest position
is that the baseline's cost is the price of its guarantees.
