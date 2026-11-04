# 16 — Six investigations into what is actually wrong

Date: 2026-11-05. Branch `main`, at `7d9e4cf`.

**No production code was changed.** Every number below is read off result files
and traces already on disk, or recomputed from them by deterministic offline
analysis. Zero API calls were made. `623 passed, 1 skipped` before and after.

This document supersedes **`docs/14` on how its small-regime result should be
read** (§3) and **`docs/07`/`docs/08`/`docs/10` on how `work_preserved` should
be compared across methods** (§4). It retracts nothing that was measured; it
changes which statistic the measurements support.

---

## 0. Summary of the six

| # | Hypothesis | Verdict |
|---|---|---|
| H-B | Verification failures | **Resolved.** All 6 are the same evaluation artifact. But the mechanism producing them is load-bearing elsewhere and **must not be relaxed.** |
| H-D | Restart versus splice | **Supported, not significant.** CausalLine 30/30 on the task check, B0 25/30. Exact McNemar p=0.0625. |
| H-A | Small-regime escalation | **Confirmed.** `docs/14`'s −17.38 is one escalation averaged with four normal runs. Selective recovery wins in every regime of both corpora. |
| H-C | Fair comparison | **Confirmed and inverted.** Under a method-neutral safety+success constraint CausalLine goes from worst (51.6%) to best (77.4%), with 0 losses on 16 cells. |
| H-F | Detector sensitivity | **Confirmed.** The headline is a 51.7-point band. Recovery-success is a function of the detector alone and discriminates nothing. |
| H-E | Cost optimization | **Partly supported.** −39% tokens, 100/100 seeds, no detectable quality or safety cost — but a measurable *attribution-recall* cost, and never tested at 56 agents. |

---

## 1. H-B — the six verification failures

### What they are

Six verification failures across the 30 real-model runs: four in the primary
set (`mixed56-workload-varied`, in three runs) and two in the control
(`mixed56-validation`, in two runs).

All six have the identical reason, and nothing else:

```
verify failed at scope=selective: ['task-level check failed',
  'NOTE: the original run failed the task check too, so this failure is not
   evidence that recovery made anything worse (docs/03 #15)']
```

- 6/6 `task-level check failed`
- 0/6 leftover taint, 0/6 referential inconsistency, 0/6 D-068 independent
  re-check hits
- 6/6 on a run whose **original task had already failed**

### Were the escalations load-bearing? No — measured, not argued

The harness kept the recovered trace from **each attempted scope**
(`-CausalLine-esc1.jsonl`, `-esc2`, then the delivered one). So the
counterfactual the run-level result cannot answer is on disk. Re-walking each
one for surviving canary tokens:

| set | seed | regime | original: events carrying the token | at scope 1 (selective) | delivered |
|---|---|---|---:|---|---|
| primary | 20260917 | small | 4 (9 contaminated events) | **0 token, 0 contaminated** | restart_all |
| primary | 20260920 | small | 6 (13) | **0, 0** | agent_restart |
| primary | 20260921 | small | 4 (9) | **0, 0** | agent_restart |
| control | 20260919 | large | 31 (68) | **0, 0** | agent_restart |
| control | 20260919 | small | 4 (9) | **0, 0** | agent_restart |

**In all five, selective recovery had already removed every trace of the
contamination.** The ladder then climbed anyway.

These five are **5/5 of every escalation in the 30-run corpus.** No run
escalated for any other reason.

### The actual cause

The task-level check conflates two different questions:

1. did recovery remove the contamination? — the system's job; answered *yes*
   at selective scope in all five;
2. does the final output satisfy the task? — a property **the original run did
   not have either.**

Because contamination was fully gone at selective scope and the task check
still failed, the defect that fails the task must live **outside the
contaminated region.** Selective recovery correctly preserves that region —
pre-existing defect included — so the check fails. Escalating regenerates more
of the trace and *incidentally overwrites* the defect, so the check passes.

Classification: **evaluation artifact.** Not genuine contamination, not
nondeterminism, not an attribution error. Selective recovery is penalised for
correctly preserving work.

### What it costs

Using each regime's own selective runs as the counterfactual:

| set | seed | regime | delivered | CL % | preserved-work forfeited | CL replay tok | peer replay tok |
|---|---|---|---|---:|---:|---:|---:|
| primary | 20260917 | small | restart_all | 0.0 | **+91.0 pts** | 10,687 | 1,310 |
| primary | 20260920 | small | agent_restart | 88.3 | +2.8 | 1,835 | 1,830 |
| primary | 20260921 | small | agent_restart | 92.4 | −1.4 | 1,207 | 1,207 |
| control | 20260919 | large | agent_restart | 14.5 | +13.1 | 9,272 | 9,272 |
| control | 20260919 | small | agent_restart | 92.4 | +1.4 | 1,221 | 1,221 |

Mean **+21.4 points** forfeited (four of five lost work; one gained 1.4 points
nominally). The single `restart_all` is 91 points and **8× the replay tokens**
of its peers.

### And yet the mechanism must not be relaxed

`src/recovery/verify.py` already refuses the obvious fix — relaxing to
`task_success or not original_task_success` — on the grounds that the task
check is an *end-to-end detector of surviving contamination*. That refusal is
correct, and this investigation measured it independently rather than taking
it on faith.

Observing (not modifying) `verify()` across all 24 scripted design points,
there are **15 calls where the relaxed predicate would have passed a trace the
strict one failed.** Every one is in a `*/influencing/{blind,pessimistic}`
cell — exactly where the detector is bad — and in every one the three other
signals are zero, so no conjunctive relaxation using the signals on hand would
catch them either.

What stopping early would cost is **already measured**, because B1 and B2 never
verify: they *are* the counterfactual for "CausalLine without the ladder."
Under the `blind` detector:

| | work preserved | unsafe per run | unsafe cells |
|---|---:|---:|---:|
| B1 / B2 (no ladder) | **100.0%** | **1.33** | 3/6 |
| CausalLine (ladder) | 50.0% | **0.00** | 0/6 |

The ladder converts 1.33 unsafe preservations per run into zero. The 100% is
the score for doing nothing while every contaminated event survives.

**Verdict.** The six failures are evaluation artifacts of an unconditioned task
check, and they cost a mean 21.4 points of preserved work in the real-model
corpus. The same check is the only thing delivering zero unsafe preservations
when the detector is poor. **The predicate does not move.** The defensible
response is to *report* escalation separately (§3), not to weaken verification.

**Missing evidence** for a real fix: a signal that separates "the task fails
because contamination survived" from "the task fails because it already did."
Neither the post-recovery contamination walk nor the D-068 re-check provides
it — both are clean in both situations.

---

## 2. H-D — restart versus splice

Task check over all 30 real-model runs:

| | held it (original passed, n=2) | repaired it (original failed, n=28) | overall |
|---|---:|---:|---:|
| B0 full restart | **0/2** | 25/28 | **25/30** |
| B1 agent taint | 2/2 | 24/28 | 26/30 |
| B2 topology closure | 2/2 | 24/28 | 26/30 |
| **CausalLine** | 2/2 | **28/28** | **30/30** |

Exact McNemar against CausalLine, on the paired runs:

| comparison | discordant, peer only | discordant, CausalLine only | exact two-sided p |
|---|---:|---:|---:|
| B0 vs CausalLine | 0 | 5 | **0.0625** |
| B1 vs CausalLine | 0 | 4 | 0.1250 |
| B2 vs CausalLine | 0 | 4 | 0.1250 |

Every discordant pair in the corpus favours CausalLine, and none favours any
baseline. **At n=30 that is p=0.0625 — suggestive, and not significant at
0.05.** It must be reported that way.

The two runs whose original task *passed* are the interesting cell: full
restart broke both, every other method held both. Both had
`payload_landed = False` — the model ignored the planted instruction — so per
`CLAUDE.md` their *safety* numbers are trivially zero, but their *task* numbers
are meaningful: the pipeline had produced a correct answer, and regenerating
everything destroyed it. **n=2.**

Cost of that reliability (mean tokens per run, 30 runs):

| | replay | analysis | total | vs B0 |
|---|---:|---:|---:|---:|
| B0 | 10,587 | 0 | 10,587 | 1.00× |
| B1 | 6,297 | 0 | 6,297 | 0.59× |
| B2 | 6,268 | 0 | 6,268 | 0.59× |
| CausalLine | 6,538 | **15,981** | 22,519 | **2.13×** |

CausalLine's replay is *cheaper* than full restart. Its total is 2.13× because
**attribution is 71% of its cost.** That, and not replay, is the only thing
worth optimising.

**Verdict.** Splicing logged outputs is more task-stable than regenerating
them, by 5 discordant pairs to 0, at p=0.0625. The mechanism is plausible —
spliced work cannot be re-broken by a non-deterministic model — but the
evidence is suggestive, not conclusive, and the strongest cell has n=2.

---

## 3. H-A — the small-regime result, recalculated

`docs/14` reports that CausalLine **loses in small** (paired −17.38 against
B2). Split by the scope recovery actually delivered at:

**Primary (`mixed56-workload-varied`)**

| regime | delivered scope | n | CL − B2 mean | sd |
|---|---|---:|---:|---:|
| small | selective | 2 | **+2.76** | 0.00 |
| small | agent_restart | 2 | 0.00 | 0.00 |
| small | restart_all | 1 | **−92.41** | — |
| small | *all five, as published* | 5 | *−17.38* | *41.97* |
| medium | selective | 5 | **+7.03** | 1.85 |
| large | selective | 5 | **+15.17** | 1.54 |

**Control (`mixed56-validation`)**

| regime | delivered scope | n | CL − B2 mean | sd |
|---|---|---:|---:|---:|
| small | selective | 4 | **+1.38** | 0.00 |
| small | agent_restart | 1 | 0.00 | — |
| small | *as published* | 5 | *+1.10* | *0.62* |
| medium | selective | 5 | **+6.21** | 0.00 |
| large | selective | 4 | **+13.10** | 0.00 |
| large | agent_restart | 1 | 0.00 | — |
| large | *as published* | 5 | *+10.48* | *5.86* |

Three facts follow.

1. **Selective recovery wins in every regime of both corpora**, by +1.38 to
   +15.17 points, with a within-scope standard deviation of 0.00–1.85.
2. **Every non-positive cell is an escalation**, and an escalation's value is
   fixed by construction, not measured: `agent_restart` here lands on the same
   preserved set as the closure baseline (0.00 by arithmetic), and
   `restart_all` preserves 0% by definition (−92.41 is just the baseline's own
   92.41%).
3. The published −17.38 with sd 41.97 on n=5 is **an average of a design
   constant and four measurements.** It describes neither.

**How `docs/14`'s headline should read.** Not "wins in medium and large, loses
in small", but: *selective recovery beat the closure baseline in 11 of 11 runs
where it was delivered, by +2.76 (small), +7.03 (medium) and +15.17 (large)
points; in 4 of 15 runs verification escalated and the delivered scope was the
baseline's or a full restart.* The win/tie/loss count for small is **2/2/1**,
not a loss.

This is a reporting correction, not a re-measurement. Nothing in `docs/14`'s
underlying data changes, and the escalations are real events that belong in the
result — separately.

---

## 4. H-C — a comparison that does not reward keeping contaminated work

96 cells × 30 repetitions, ground truth known by construction. One constraint
added at a time:

| method | (a) all 24 cells | (b) drop `blind` | (c) safe cells only | (d) safe **and** recovery succeeded |
|---|---:|---:|---:|---:|
| B0 full restart | 0.0% | 0.0% | 0.0% (24/24) | 0.0% |
| B1 agent taint | **63.2%** | 50.9% | 55.8% (20/24) | 65.8% |
| B2 topology closure | 57.0% | 42.7% | 48.4% (20/24) | 57.9% |
| **CausalLine** | 51.6% | 52.1% | 51.6% (24/24) | **77.4%** |

B1 and B2 commit 0.42 unsafe preservations per run and are unsafe in 4 of 24
cells each; CausalLine is 0.00 and 0 of 24. Column (d) has n=16 for every
method.

**(a) is the published comparison and it is not a fair one.** It rewards
methods for work kept by mislabelling contaminated work as clean. B1's 63.2%
includes cells scoring 100% while committing 4 unsafe preservations.

**(b)** The `blind` detector flags nothing. Its own docstring calls it "the
control that shows what the metrics look like when no incident is reported at
all: every method preserves everything, and every truly contaminated event is
an unsafe preservation." It exists to prove the scoring can report failure, and
averaging it into a performance number scores inaction as perfection. Dropping
it alone moves B1 from 63.2% to 50.9% and puts CausalLine ahead.

**(d) is the metric this project is actually about:** on the cells where a
method both recovered successfully and preserved nothing contaminated, how much
work did it keep? (`recovery_success` already means `task_success and
unsafe_preservations == 0`.)

**The filter is method-neutral, which is the property that makes it usable.**
The 16 qualifying cells are **identical for all four methods**, and all 8
dropped cells are dropped because *no method* recovered successfully in them
(`recovery_success_rate = 0` for B0, B1, B2 and CausalLine alike). Nothing is
excluded on CausalLine's behalf.

Per-cell, on those same 16:

| comparison | CausalLine wins | ties | **losses** |
|---|---:|---:|---:|
| CausalLine vs B1 agent taint | 7 | 9 | **0** |
| CausalLine vs B2 topology closure | 7 | 9 | **0** |

**Verdict.** The reported ordering (B1 63.2% > B2 57.0% > CausalLine 51.6%) is
an artifact of averaging over cells where no method recovered at all, four of
which additionally credit the baselines for unsafe preservations. Under a
method-neutral safety-and-success constraint the ordering inverts —
**CausalLine 77.4%, B1 65.8%, B2 57.9%** — and CausalLine does not lose a
single cell.

The honest headline is a **dominance claim, not a mean**: CausalLine is never
worse and is better on 7 of 16, while being the only non-trivial method with
zero unsafe preservations in all 24 cells.

---

## 5. H-F — detector sensitivity

Work preserved, by detector:

| detector | B0 | B1 | B2 | CausalLine | B1/B2 unsafe per run | CL unsafe |
|---|---:|---:|---:|---:|---:|---:|
| oracle | 0.0% | 43.9% | 29.8% | **71.0%** | 0.00 | 0.00 |
| heuristic | 0.0% | 78.9% | 71.9% | 66.0% | 0.33 | 0.00 |
| blind | 0.0% | 100.0% | 100.0% | 50.0% | **1.33** | 0.00 |
| pessimistic | 0.0% | 29.8% | 26.3% | 19.3% | 0.00 | 0.00 |
| **span** | 0.0 | 70.2 | 73.7 | **51.7** | | |

Three things to report.

**The headline is a band, not a number.** CausalLine's work preserved runs
**71.0% down to 19.3%** depending only on the detector in front of it. Any
single figure quoted without its detector is close to meaningless. The
baselines' spans are *wider* (70.2 and 73.7 points), so this is not a weakness
peculiar to CausalLine — but it is a property of the whole evaluation.

**Recovery-success discriminates nothing.** It is 100% / 83% / 50% / 33% by
detector and **identical for all four methods within each detector**,
including B0. It is a measure of the detector, not of the recovery method, and
should not be quoted as a result about methods.

**CausalLine's cost structure is detector-independent where it matters.** Its
analysis cost is flat at 769 tokens across all four detectors; only replay
grows as the detector worsens (151 → 203 → 300 → 467). Attribution does not
get more expensive when the detector gets worse.

### The one genuine weakness this exposes

Under the `pessimistic` detector — which flags every source of the same kind as
a planted one, and which the detector's own docstring calls "the condition
where the exposure/influence distinction should pay most" — CausalLine preserves
**0.0%** on four design points where B1 safely preserves 10.5–21.1%. In all
four, CausalLine's replay cost is **exactly 600 tokens, identical to full
restart**, plus 663–903 tokens of analysis. It escalated to `restart_all` and
paid for attribution on the way.

So the condition the design is aimed at is the condition where it currently
degrades to *worse than full restart* — same replay cost, plus analysis. This
is mitigated but not excused by the fact that `recovery_success_rate = 0` for
every method in all four of those cells: nobody recovers there, and B1's
preserved work is preserved inside a failed recovery. It remains the clearest
open weakness in the system.

---

## 6. H-E — deferred self-report, on 100 seeds

The sweep completed: **100 seeds × 3 arms × 2 randomness regimes = 600
matrices, 3,600 cell evaluations, 0 API calls, ~54 minutes.**

| | hybrid (shipped) | lazy | targeted_only |
|---|---:|---:|---:|
| work preserved | 69.9% (sd 4.07) | 70.1% (sd 5.12) | 69.3% (sd 0.00) |
| analysis tokens | 4,620 | **2,633** | 1,500 |
| total tokens | 10,175 | **6,190** | 3,900 |
| unsafe preservations | **0** | **0** | 0 |
| task success | 600/600 | 600/600 | 600/600 |
| pair false negatives | 29 / 2,929 = **0.99%** | 24 / 892 = **2.69%** | 100 / 700 = 14.29% |

Paired per-seed difference, lazy − hybrid:

| | shipped randomness | paired (common random numbers) |
|---|---|---|
| work preserved | +0.26 pts, 95% CI **[−1.02, +1.55]** — contains 0 | +0.21 pts, CI [−0.83, +1.25] — contains 0 |
| total tokens | **−3,985**, CI [−4,086, −3,884] — excludes 0, **100/100 seeds** | −3,916, CI [−4,010, −3,822], 100/100 |
| analysis tokens | −1,987 = **−43.0%** | −1,952 = −42.4% |
| total saving | **−39.2%** | −38.6% |

### What this establishes

- **The cost reduction is real and tight.** −39% of analysis+replay, every one
  of 100 seeds, under both randomness regimes, with an interval nowhere near
  zero.
- **`docs/15`'s prediction held.** 30 seeds gave +0.32 pts, CI [−1.97, +2.61];
  100 seeds give +0.26, CI [−1.02, +1.55]. The point estimate barely moved and
  the interval narrowed as predicted. The −3.6 that once closed this direction
  stays retracted.
- **Safety is untouched**: 0 unsafe preservations and 600/600 task success in
  all 600 matrices, for both arms, under both randomness regimes.
- Two of six cells (`B/influencing`, `C/influencing`) are **exactly identical**
  between the arms — 0.00 difference, sd 0.00, 100/100 ties. All the variance
  is in the other four.

### What this does not establish

**It is not a free optimization, and should not be described as one.** The pair
false-negative *rate* roughly triples: 0.99% → 2.69% (shipped), 0.63% → 2.83%
(paired). In the paired regime lazy's *absolute* count is also worse (25 vs 19).
Lazy loses attribution recall at the pair level; what makes that invisible in
the outcome metrics is that the escalation ladder absorbs it.

**The comparison is not apples-to-apples.** Lazy scores ~890 pairs where hybrid
scores ~2,930 — it examines roughly 70% fewer, because it never asks inline.
A rate computed on a different denominator is not a like-for-like recall
figure. *Missing evidence:* a per-pair matched comparison restricted to the
pairs **both** arms examine. That is cheap and offline, and it has not been
done.

**It has never been tested at 56 agents.** Every figure here is scripted-mode,
on a 6-cell chain matrix. Per `CLAUDE.md` these numbers may not be put in a
table with a real-model number, and they do not license a default change.

**Verdict.** A statistically defensible −39% token reduction with no detectable
cost in work preserved, safety or task success, and a measurable cost in
pair-level attribution recall. Supported as an **option**, which it already is.
**Not supported as a new default** until the matched-pair analysis and a
56-agent acceptance run exist.

---

## 7. Consolidated findings

### 7.1 What is actually wrong, or unnecessarily expensive

1. **The reporting, more than the system.** Two of the project's headline
   numbers — `docs/14`'s "loses in small" and the scripted ordering "B1 63.2% >
   CausalLine 51.6%" — are artifacts of averaging over cells that are design
   constants or total failures. Both reverse under statistics that are
   method-neutral and defensible (§3, §4). Nothing was mis-measured; the wrong
   summary was taken.
2. **Attribution is the cost, not replay.** 71% of CausalLine's real-model cost
   is analysis (15,981 of 22,519 tokens per run). Its *replay* is cheaper than
   full restart. Every cost argument should be about attribution.
3. **An unconditioned task check drives 100% of escalations** and cost a mean
   21.4 points of preserved work across five runs (§1) — but relaxing it is
   measured to cost safety, so this is a known, quantified, *accepted* cost,
   not a bug to fix.
4. **Under a pessimistic detector the system degrades to worse than full
   restart** — 0% preserved, full-restart replay cost, plus analysis — in the
   very condition the design is aimed at (§5). This is the clearest unsolved
   weakness.
5. **The noise floor is uncalibrated for the mixed-model testbed.** All 30
   real-model runs carry `uncalibrated: calibration was measured on
   'gemini-3.6-flash' but the current model is 'mixed'`. Every clean verdict in
   the primary result rests on an unmeasured noise floor. The runs were made
   with every facet counted, which is the conservative setting, so this
   understates nothing — but it is a standing limitation.

### 7.2 Which cost reductions are supported by evidence

| reduction | evidence | status |
|---|---|---|
| Deferred self-report (`lazy`), −39% of analysis+replay | 100 seeds, 600 matrices, CI [−4,086, −3,884], 100/100 seeds, 0 unsafe, 600/600 task | **Supported** on the scripted matrix, as an option. Not as a default. |
| Avoiding the false-alarm escalations (would save ~9,400 replay tokens on the one `restart_all`) | 5/5 escalations verifiably unnecessary (§1) | **Rejected.** The only available predicate change is measured to cost safety. |
| `targeted_only`, −62% | 100 seeds | **Rejected**, confirmed again: 14.29% pair false-negative rate against hybrid's 0.99%. |

Everything else previously investigated stays closed: H1 per-node compromise
probability, H2 structural prior, H3 adaptive budget, Gate 1, memoization,
group testing, replay/splicing, checkpoint GC, "savings scale with workflow
length", and closure-scoped deferral (`docs/15`).

### 7.3 Which improvements are safe to implement

**Safe, and recommended — reporting only, no logic change:**

1. Report every per-regime result **split by delivered scope**, with escalations
   counted and named rather than averaged in (§3).
2. Report `work_preserved` under the **method-neutral safety-and-success
   constraint**, as a dominance count (wins/ties/losses) rather than a mean
   (§4).
3. Report work preserved as a **detector band** with the detector named, never
   as a bare figure (§5).
4. Stop quoting **recovery-success** as a result about methods; it is a
   property of the detector (§5).
5. Correct `docs/14`'s headline and `docs/13`'s to match §3.

**Safe, already true:** `lazy_self_report` stays off by default
(`tests/test_cost_without_quality_loss.py` pins this).

**Not safe:** any relaxation of the task-level check in
`src/recovery/verify.py` (§1). Any change to recovery thresholds. Making
`lazy` the default before a matched-pair analysis and a 56-agent acceptance
run.

### 7.4 Expected total cost reduction

**Measured, scripted mode:** −39.2% of analysis+replay (10,175 → 6,190 tokens
per matrix), 100/100 seeds.

**Projected, real-model 56-agent mode — a projection, not a measurement:**
analysis is 15,981 of CausalLine's 22,519 tokens per run. If the −43.0%
analysis saving transferred unchanged, that is ≈6,870 tokens saved per run,
giving ≈15,650 — **2.13× B0 falling to ≈1.48× B0.** This number has not been
measured and must not be quoted as a result. It is the size of the prize that
would justify a 56-agent acceptance run, and nothing more.

**No other cost reduction is currently supported.** The escalation saving is
rejected on safety grounds.

### 7.5 Remaining limitations

1. **Sample size.** Five runs per regime per corpus. H-D's strongest cell —
   full restart breaking a task that was working — is **n=2**. The McNemar
   result is **p=0.0625, not significant at 0.05**.
2. **Lazy self-report has never run at 56 agents**, and scripted and real-model
   numbers may not be pooled.
3. **The pair false-negative comparison for lazy is not like-for-like**
   (different denominators); the matched-pair analysis is missing and is cheap.
4. **No fix exists for the false-alarm escalations** from the signals
   `verify()` currently computes. A signal separating "the task failed because
   contamination survived" from "the task failed because it already did" is the
   single most valuable missing piece of evidence in the project.
5. **The pessimistic-detector degradation is unexplained and unfixed** (§5).
6. **The noise floor is uncalibrated for the `mixed` model** on all 30
   real-model runs (§7.1.5).
7. **Real-model ground truth is observed, not constructed** — canary-token
   presence plus code-path records. An influence leaving no token behind is
   invisible, and every affected run understates contamination. `docs/09` §9
   bounds this.
8. Two of the primary runs have `payload_landed = False`; their safety numbers
   are arithmetic, not results.

---

## 8. Reproducing this document

Nothing here needs an API key or a GPU, and nothing calls a model.

```
python -m pytest tests/ -q                      # 623 passed, 1 skipped
python -m src.eval.mixed_validation_report      # the per-run tables of sections 1-3
```

The §1 escalation measurement re-walks
`data/runs/mixed56-*/seed*/*/...-CausalLine-esc*.jsonl` for canary tokens via
`src.eval.real_llm.ground_truth`. §4 and §5 read `data/results/campaign.json`.
§6 reads `data/results/seedsweep/sweep-100.json`, which is committed so a fresh clone can check it (regenerable in ~54 min with no API calls:
`python -m src.eval.selfreport_pairing --sweep 100 --out data/runs/seedsweep/sweep-100.json`). The `verify()` probe of §1
observes the real function through a wrapper and modifies nothing.
