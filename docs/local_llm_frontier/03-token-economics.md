# Local LLaMA frontier — 3. Where the tokens go, and what would make recovery cheaper than restart

The question this project has to answer before it is publishable as a *cost*
result: **when is selective recovery cheaper than just re-running the workflow?**
Right now it never is, on either frontier. This is the measurement of why, and
what would change it.

Regenerate: the numbers below come from the GPU campaign's original-run traces
under `data/runs/local_llama/`.

---

## 1. The win condition, written down

B0 re-executes the workflow: cost `N`.
CausalLine pays analysis `A`, then replays the contaminated fraction `f` of it.

```
CausalLine wins  <=>  A + f·N  <  N  <=>  A/N + f  <  1
```

**Measured on the completed local GPU campaign (60 runs, n = 17 landed):**

| | identified | delivered |
|---|---|---|
| A (analysis tokens per run) | 6 489 | 6 489 |
| N (full-restart tokens) | 5 520 | 5 520 |
| **A/N** | **1.18** | **1.18** |
| f (fraction that must be recomputed) | 0.39 | **1.00** |
| **A/N + f** | **1.57** | **2.18** |

Both columns need to be under 1.00 and neither is.

**The two columns, and why the second is the real one.** `f` identified is the
contaminated fraction CausalLine works out it must recompute — 39% of the
workflow. `f` delivered is what it actually recomputed: **all of it**, because
`verify()` refused to certify the selective replay on 17 of 17 landed runs and
the planner escalated to a full restart (D-086). On this frontier CausalLine
did not pay `A + f·N`; it paid `A + (selective replay) + N`.

The analysis alone costs more than the restart it is trying to avoid, before
any of that. That is `docs/03` #7's collapse condition, and it is now measured
on three models.

---

## 2. Where the tokens actually are

Per original run, averaged over 36 local runs:

| purpose | tokens/run | calls/run | share |
|---|---|---|---|
| `pipeline` (the real work) | 5 530 | 6.8 | 49% |
| `self_report` | **3 540** | 6.8 | **31%** |
| `counterfactual` | 2 203 | 2.6 | 20% |
| **total** | **11 274** | | |

And the shape of it, which is the part that matters:

| | prompt | output | ratio |
|---|---|---|---|
| pipeline | 4 496 | 1 053 | 4.3 : 1 |
| self_report | 3 196 | 360 | **8.9 : 1** |
| **all** | **7 692 (84%)** | 1 414 (16%) | |

**84% of every token spent is prompt, not generation.** The analysis is not
expensive because the model writes a lot — it is expensive because the same
context is re-sent, over and over. Self-report is the extreme case: it re-sends
the event's output plus the source catalogue to get back a small JSON object,
at nearly 9 tokens in per token out.

That single fact reframes the cost problem. Optimising *what the model says*
buys almost nothing. Optimising *what we send it, and how often* is the whole
lever.

---

## 3. Three levers, projected from the measured numbers

### Lever 1 — lazy self-report (D-082, already built, default off)

Self-report currently fires on every model event of every run, before anyone
knows whether an attack happened. D-082 defers it to detection time and was
measured on the scripted matrix at analysis 4 500 → 2 500 (−44%), with safety
unchanged on every axis and **100% of the cost removed from clean runs**.

Removing self-report from the analysed path here:

```
A 6 489 -> 2 949    A/N 1.18 -> 0.53    A/N + f = 0.92     (at f identified = 0.39)
```

**This one lever alone crosses the line — but only against `f` identified.**
Against `f` delivered it does not: `0.53 + 1.00 = 1.53`. Lazy self-report makes
the *analysis* affordable; it does nothing about a planner that escalates to a
full restart because verification cannot certify the replay. **Both problems
have to be fixed for the cost result to turn positive, and D-086 is the one
this project has not started on.**

It is built, tested, and off by default because it costs 3.6 points of
identified work preserved (D-082).

### Lever 2 — prompt caching

84% of tokens are prompt, and the source block is re-sent on every pipeline
call, every self-report and every counterfactual. Hosted providers bill a cached
prefix at roughly a tenth of the normal rate. Applied on top of lever 1:

| cached share of prompt | A | A/N | A/N + f |
|---|---|---|---|
| 50% | 1 641 | 0.30 | **0.70** |
| 75% | 1 047 | 0.19 | **0.59** |
| 90% | 690 | 0.13 | **0.53** |

Not implemented. It changes no verdict and no metric — it is purely a billing
change — which makes it the cheapest available improvement to the one number
that currently sinks the method.

### Lever 3 — localized contamination (the workflow-scale hypothesis)

The intuition is that restart wins because the workflow is small, and that a
larger workflow with a small contaminated region would flip it. **The algebra
agrees and the measurement does not — for a reason worth stating precisely.**

Measured, short (19 events) against long (24 events, five agents, two research
rounds):

| shape | n | events | A | N | A/N | f | **A/N + f** |
|---|---|---|---|---|---|---|---|
| short | 5 | 19 | 5 271 | 4 758 | 1.11 | 0.37 | **1.48** |
| long | 6 | 24 | 7 285 | 6 911 | 1.05 | 0.42 | **1.48** |

`A/N` improved slightly (1.11 → 1.05) and `f` rose slightly (0.37 → 0.42), and
**the sum did not move at all**. A 26% longer workflow bought nothing.

**Why: `A` scales with the contaminated region, not with the workflow.** Make
the workflow bigger and the poison spreads further too, so more pairs need
examining and `f` stays put. This independently reproduces the Phase C finding
already in `docs/06` §4 — that the investigation-to-restart ratio stays flat as
the workflow lengthens — on a real model rather than a scripted one.

**So the hypothesis is right only with its qualifier.** Size alone does nothing.
What is needed is workflows where contamination is genuinely *localized* — `f`
must fall as `N` grows. With lever 1 applied:

| f | A/N + f (lazy) | A/N + f (lazy + 75% cache) |
|---|---|---|
| 0.40 | 0.92 | 0.59 |
| 0.30 | 0.82 | 0.49 |
| 0.20 | 0.72 | 0.39 |
| 0.10 | 0.62 | 0.29 |

A testbed where one poisoned source touches 10% of a long workflow is where this
method is supposed to pay, and when this was written **the project had never
built one.** Every scenario here plants a source the Coder or Researcher reads
directly, so it propagates to most of the run by construction.

> **SUPERSEDED 16-09-2026 — it has now been built and run.** The fan-out
> workflow (D-091) holds the contaminated region at a constant three events
> while the trace grows, giving `f` = 0.21 / 0.12 / **0.06** at K = 4 / 8 / 16.
> Measured with lazy self-report on, `A/N + f` came out at **0.93** (K=8) and
> **0.59** (K=16) -- the win condition met on executed runs, against the 0.92
> projected below. See `docs/local_llm_frontier/04`, and read its §5 before
> quoting either number: the chain result is *not* overturned, and the testbed
> was designed to have the property being tested.

---

## 4. The cost gate: what exists, and what does not

There *is* a runtime gate, and it works — `planner.greedy_cover`:

```python
if spent + best.cost > cap and best.kind != "restart_all":
    return [restart_all]          # cap = restart_all_cost(trace)
```

If accumulated selective **replay** would cost more than re-running everything,
the planner abandons selective recovery and restarts. That is real and it fires.

**Two things it does not do, and both matter:**

1. **`cap` counts replay only, never analysis.** `restart_all_cost()` is
   `trace.pipeline_tokens()` and the comparison is against `Action.cost`, which
   is replay. The dominant term — `A`, at 1.18 × N — is not in the comparison at
   all. The gate protects against expensive *replays*; it cannot see the
   expensive *investigation* that precedes them.

2. **The abort that was designed for exactly this is not wired.**
   `sprt_investigate.config_for(analysis_tokens, restart_tokens)` derives the
   SPRT's hypotheses from the real cost model, so the investigation can stop
   early when contamination is spreading past the point where selective recovery
   pays. Grepped across the whole repository: **`config_for` has no caller
   outside its own module and its own tests, and `sprt_config=` is passed by
   nobody.** So `refine_for_verdict` runs on `SPRTConfig()`'s defaults,
   `f_star = 0.3` / `f_star_high = 0.7` — taste numbers, not measurements.

This is the same built-but-disconnected pattern the project has hit before
(`run_code` in D-064, the calibration in D-070, the empty `Calibration()` in
D-079). The mechanism exists, is tested standalone, and never reached an
experiment.

**The fix is two lines at the call site** — pass
`sprt_config=config_for(analysis_tokens, restart_tokens)` — plus the
re-measurement that obliges. It is the highest-value unwired thing left.

---

## 5. What this means for the paper

> **UPDATED 16-09-2026.** Everything in this section is about the **chain**
> workflow and remains true of it. On the fan-out workflow with lazy
> self-report, CausalLine *is* cheaper -- `A/N + f` = 0.93 at K=8 and 0.59 at
> K=16, measured. The claim below is therefore now conditional on workflow
> shape rather than absolute. `docs/local_llm_frontier/04`.

**On chain-shaped workflows, do not claim CausalLine is cheaper. It is not, and
three models agree.**

The claim the measurements support is narrower than it looked, and narrower
than the earlier draft of this file said: CausalLine **identifies a smaller
contaminated region than any baseline, unanimously (17W–0L–0T, p = 0.00002), at
equal safety.** That is a claim about identification. On the local frontier it
**delivered** none of that preservation, because verification refused to certify
the replay every time and the planner escalated (D-086). It buys a better
answer to "what is contaminated", not saved tokens and — so far, nowhere in this
project — not demonstrated preserved work.

That becomes a *cost* result only when the preserved work is worth more than the
tokens — irreversible side effects, human review, or computation that cannot be
reproduced — and this testbed has none of those. Building one, or building a
workflow with genuinely localized contamination, is the experiment that would
turn the honest negative above into a positive.
