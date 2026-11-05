# The consolidated 56-agent validation: B0, B2, CausalLine, IREC

Date: 2026-11-05. Branch `feat/irec`. **IREC remains off by default and
unmerged.** This document is the authority on all four methods' costs on the
56-agent workload; where it disagrees with
[`IREC_LIVE_VALIDATION.md`](IREC_LIVE_VALIDATION.md), this one is correct.

**Verdict: GO, conditional on one deferred question (§8).**

---

## 1. The blocker, and the design that replaced a from-scratch campaign

A from-scratch 4-way campaign — re-running the 15 original 56-agent pipelines
and then all four recoveries — was costed before anything was spent:

| | tokens |
|---|---:|
| re-run the 15 originals | 163,299 |
| B0 + B1 + B2 replays | 352,140 |
| CausalLine (analysis + all attempts) | 360,306 |
| IREC | 68,816 |
| **total** | **944,561  (945k)** |

That is **13.7× the design actually used**, needs roughly **170 Gemini calls**
against a ~20-per-day free tier (about nine days), and takes many hours of CPU
inference. Reported here rather than silently absorbed.

**It would also have been scientifically worse.** A 56-agent real-model run is
non-deterministic. Re-running the originals would put B0/B2/CausalLine on
*different traces* from the ones IREC is scored on, so the comparison would stop
being paired at exactly the point where pairing matters.

**The design used instead: all four methods scored on the same 15 saved
original traces.** The recorded B0/B1/B2/CausalLine rows were produced inside
the same run as one another, against that run's trace, with that run's detector
verdict and `_tools_for` fixtures. IREC is replayed against the identical trace
with the identical flagged set. Workload, contamination placement and seed are
therefore not merely matched — **they are the same objects**. This is a stronger
form of pairing than re-running could provide, and it is not a substitution of a
different experiment: it is the same comparison, with the confound removed.

**What was run fresh:** IREC through the **integrated** `src/recovery/irec.py`,
not the prototype client used for the first campaign. Those are two separate
implementations of the same two rules and the integration path had never been
exercised end to end. It reproduces the prototype exactly (§2).

### Accounting, as applied

- **Defect A does not affect this path.** `src/eval/real_llm.py` already passes
  `tools=_tools_for(...)` to the baselines *and* to CausalLine. The defect was
  `experiment.py:run_cell`, the scripted path, only. Verified in the source.
- **Defect B does affect it.** CausalLine's recorded `replay_tokens` is its
  final attempt alone; every attempt is recovered from the escalation traces the
  harness kept and charged.
- **IREC's fallback term is additive and never discounted.** It is zero because
  IREC escalated zero times, and it is reported as zero rather than omitted.
- The typed interface is **not used**: `handoff_projection` stays at its default
  of `None`, the shipped free-text behaviour.

---

## 2. Integration check: the integrated module reproduces the prototype

`20260917/large`, same trace, same flagged set:

| quantity | prototype | integrated | match |
|---|---:|---:|---|
| spliced | 2 | 2 | yes |
| re-executed | 53 | 53 | yes |
| cutoff kept | 5 | 5 | yes |
| changed | 48 | 48 | yes |
| replay tokens | 10,014 | 10,014 | yes |
| discarded | 102 | 102 | yes |
| unsafe preservations | 0 | 0 | yes |
| work preserved | 29.7% | 29.7% | yes |

One scoring trap was found and fixed while doing this, and it is worth
recording because it is the mirror image of the defect `discarded_events()`
exists to prevent. Scoring IREC on `report.invalidation` — its re-executed
**model** events — against B2's full 137-event closure produced **48 unsafe
preservations and 47/95 recall** on this placement. IREC is instead scored on
what it did not preserve, read off the recovered bytes: every replaced model
output, every event whose recomputed bytes differ (the messages, tool calls and
memory operations the pipeline rebuilt from a changed upstream), and every event
whose stored output still holds flagged content (issue #20). Under that
definition the same run is **0 unsafe and 95/95 recall**. Events the cutoff kept
are preserved, which is the mechanism's point: their bytes are identical.

**Reproducibility note.** `20260919/medium` cost 5,145 tokens here against 5,239
in the first campaign — a 1.8% difference from real-model nondeterminism between
two IREC runs of the same placement. Every other placement matched to the token.

---

## 3. Safety and correctness: counts, denominators, rates

15 placements, 2,175 events, 541 truly-contaminated events (observed truth).

| method | unsafe preservations | contamination escapes | task success | verification | work preserved |
|---|---:|---:|---:|---|---:|
| B0 full restart | **0 / 541** = 0.0% | 0 / 541 | **13 / 15** | not verified | 0 / 2,175 = 0.0% |
| B1 agent taint | **0 / 541** = 0.0% | 0 / 541 | 15 / 15 | not verified | 995 / 2,175 = 45.7% |
| B2 topology closure | **0 / 541** = 0.0% | 0 / 541 | **14 / 15** | not verified | 985 / 2,175 = 45.3% |
| CausalLine (corrected) | **0 / 541** = 0.0% | 0 / 541 | 15 / 15 | 12/15 selective | 1,020 / 2,175 = 46.9% |
| **IREC** | **0 / 541** = 0.0% | **0 / 541** | **15 / 15** | **15 / 15 PASS** | **1,602 / 2,175 = 73.7%** |

- **IREC recall: 541 / 541 = 100%.**
- **IREC escalations: 0.** Fallback tokens: 0. Control-flow divergences: 0.
  D-068 byte re-check survivors: 0.
- **CausalLine: 4 verification failures over 15 runs, 3 runs escalated** (one to
  `restart_all`, which preserves 0% by definition).
- **Full restart fails the task in 2 of 15 runs**, and B2 in 1 of 15, while B1,
  CausalLine and IREC pass all 15.

**On "exact ground truth".** Truth here is **observed** — canary-token presence
plus the pipeline's own code-path records — not exact by construction. The
exact-per-pair ground truth lives in the scripted matrix, where IREC is
**0 unsafe on all 24 cells** and `0/38` across both arms of the C2 ablation.
The two modes are reported separately and never pooled, per `CLAUDE.md`.

---

## 4. By contamination regime, landed placements only

### small (n = 5)

| method | mean tokens | work preserved | unsafe | task |
|---|---:|---:|---:|---:|
| B0 | 10,694 | 0.0% | 0/57 | 5/5 |
| B1 | 1,596 | 89.9% | 0/57 | 5/5 |
| B2 | 1,595 | 89.9% | 0/57 | **4/5** |
| CausalLine | 20,577 | 72.6% | 0/57 | 5/5 |
| **IREC** | **1,206** | **92.1%** | 0/57 | 5/5 |

`R = 0.760`, 95% CI **[0.719, 0.801]** — **0.8× target met.**
Mechanism: spliced 49.8, re-executed 5.2, cutoff kept 0.0 (**yield 0%**).

### medium (n = 3)

| method | mean tokens | work preserved | unsafe | task |
|---|---:|---:|---:|---:|
| B0 | 10,678 | 0.0% | 0/51 | 3/3 |
| B1 | 7,952 | 38.4% | 0/51 | 3/3 |
| B2 | 7,952 | 37.7% | 0/51 | 3/3 |
| CausalLine | 23,751 | 44.6% | 0/51 | 3/3 |
| **IREC** | **4,975** | **88.3%** | 0/51 | 3/3 |

`R = 0.626`, 95% CI **[0.606, 0.645]** — **0.8× target met.**
Mechanism: spliced 33.7, re-executed 21.3, cutoff kept 13.3 (**yield 63%**).

### large (n = 5)

| method | mean tokens | work preserved | unsafe | task |
|---|---:|---:|---:|---:|
| B0 | 10,337 | 0.0% | 0/433 | 5/5 |
| B1 | 9,772 | 9.2% | 0/433 | 5/5 |
| B2 | 9,772 | 8.6% | 0/433 | 5/5 |
| CausalLine | 25,683 | 23.7% | 0/433 | 5/5 |
| **IREC** | **9,318** | **35.9%** | 0/433 | 5/5 |

`R = 0.954`, 95% CI **[0.893, 1.014]** — **0.8× target missed.** The CI's upper
bound crosses 1.0, so on this evidence IREC in the large regime cannot be
claimed to be *strictly* cheaper than B2; two of the five runs are exactly equal
to it. Mechanism: spliced 6.0, re-executed 49.0, cutoff kept 6.0 (yield 12%).

Why large cannot reach 0.8× is established in
[`IREC_LIVE_VALIDATION.md`](IREC_LIVE_VALIDATION.md) §6: `|D|`, the set of
events whose prompt actually changed and which any sound black-box method must
re-query, is **94.6%** of the closure's model work there. ~0.95 is the floor and
IREC is at it.

### The two non-landed placements, separate

Their safety zeros are arithmetic, so they carry no safety evidence. Their
mechanism result is the clearest in the campaign:

| placement | IREC | B2 | R | work preserved | cutoff |
|---|---:|---:|---:|---:|---:|
| 20260917 medium | 671 | 8,199 | **0.08** | **145 / 145** | 4 / 4 |
| 20260920 medium | 507 | 7,819 | **0.06** | **145 / 145** | 3 / 3 |

Agents were exposed to the payload and demonstrably ignored it. IREC
established that by intervention, preserved everything, and spent 6–8% of the
cheaper baseline.

---

## 5. Paired comparisons, pooled over the 13 landed placements

Student *t*, 95%, n = 13. Negative means IREC is lower.

| comparison | mean | 95% CI | excludes 0 | win/tie/loss |
|---|---:|---|---|---|
| IREC − B0, tokens | **−5,357** | [−7,706, −3,009] | **yes** | 13 / 0 / 0 |
| IREC − B2, tokens | **−1,011** | [−1,712, −310] | **yes** | 11 / 2 / 0 |
| IREC − CausalLine, tokens | **−18,077** | [−20,076, −16,079] | **yes** | 13 / 0 / 0 |
| IREC − B2, work preserved | **+23.0 pts** | [+10.8, +35.2] | **yes** | 13 better / 0 / 0 |
| IREC − CausalLine, work preserved | **+22.3 pts** | [+5.6, +38.9] | **yes** | 11 better / 2 / 0 |

**Every one of the five paired comparisons excludes zero, and IREC never loses a
single placement on any of them.** The two ties on tokens against B2 are the
large-regime runs where `|D|` is the whole closure.

### Pooled totals

| method | tokens | vs min(B0, B2) |
|---|---:|---:|
| **IREC** | **67,544** | **0.837** |
| B2 topology closure | 80,689 | 1.00 |
| B0 full restart | 137,186 | 1.70 |
| CausalLine (corrected) | 302,550 | **3.75** |

`IREC / CausalLine = 0.223` — a **78% reduction**, essentially all of it the
deleted attribution stage (16,006 tokens per run, about two thirds of
CausalLine's cost).

CausalLine's recorded total over these placements was 297,658; defect B adds
**+4,892**.

---

## 6. A correction to this project's own earlier numbers

`IREC_LIVE_VALIDATION.md` and `IREC_FINAL_DECISION.md` first reported
CausalLine's pooled cost as **312,706** and `IREC / CausalLine` as **0.216**.
Both were wrong. An analysis script added the *recorded* `replay_tokens` to the
escalation-trace totals, but the recorded figure **is** the final attempt, so the
final attempt was counted twice on each escalated run.

Corrected: CausalLine **302,550**, ratio **0.223**, `CausalLine / min` **3.75**
(was 3.88). The corpus-wide defect-B ratio for the primary set is **1.047×**,
not 1.143×, and CausalLine's mean per-run total is **23,343 = 2.21× B0**, not
24,020 = 2.27×.

Unaffected: the per-run table in `IREC_COST_ACCOUNTING.md` §3 and the **1.63×**
pooled over the five escalated runs — those sum the attempt traces and never add
the recorded figure on top.

**The error flattered IREC in every instance.** Both documents now carry the
correction inline.

---

## 7. Preserved as unresolved: the live-versus-scripted work-preservation discrepancy

Not investigated, by instruction, and **not explained away**:

- On this 56-agent workload IREC preserves **more** work than CausalLine in all
  three regimes (92.1 / 88.3 / 35.9% against 72.6 / 44.6 / 23.7%), and the
  paired difference is **+22.3 pts, CI [+5.6, +38.9]**, 11 better / 2 tied /
  0 worse.
- On the scripted matrix IREC preserves **less** than CausalLine hybrid
  (61.9% against 68.3%).

Both are measured and both stand. No experiment was launched to reconcile them
and no interpretation is asserted here. It remains an open question, and it is
the reason §8's recommendation is conditional rather than unqualified.

---

## 8. GO / NO-GO

# GO for promotion, conditional on resolving §7

### Acceptance criteria, all met

| required | result |
|---|---|
| recall on contaminated events | **541 / 541 = 100%** (observed truth); 0 unsafe on all 24 scripted cells (exact truth) |
| zero unsafe preservations | **0 / 541** |
| zero contamination escapes | **0 / 541**; 0 D-068 byte re-check survivors |
| no degradation in task success | **15 / 15**, against B0's 13/15 and B2's 14/15 |
| recovery correctness | production `verify()` **15 / 15 PASS**, 0 escalations, 0 divergences |

### Economic result

- **Minimum requirement** (cheaper than both baselines): cheaper than B0 in
  **15/15**; cheaper than B2 in **11/15**, **equal in 2/15** (large, where the
  contamination reaches the whole closure), and the remaining 2 are the
  non-landed placements at R 0.06–0.08. **Never more expensive than either, in
  any placement.** Met-or-tied everywhere; strictly met in 13/15.
- **Preferred 0.8× target:** **met in small (0.760) and medium (0.626), missed
  in large (0.954)**, where ~0.95 is the proven floor for any sound black-box
  method.
- **Against current CausalLine: 0.223×**, with every paired comparison
  excluding zero.

### Why GO

IREC is cheaper than every alternative, preserves substantially more work than
every alternative, and is the only method that passes verification on all 15
runs with zero escalations — while matching the best safety record in the field
(every method scored 0 unsafe here, so safety is a floor IREC meets rather than
an edge it wins). It achieves this with **no attribution stage at all**, and the
evidence that the stage it replaces was not merely redundant but *wrong* on at
least one pair — `(S3, e0010)` — is in
[`IREC_SAFETY_AND_NOVELTY.md`](IREC_SAFETY_AND_NOVELTY.md) §4.

### Why conditional

1. **§7 is unresolved.** The work-preserved direction reverses between the two
   evaluation modes. Promoting a default on a metric that points two ways in two
   modes would be premature, and the pending C3 investigation is the right place
   to settle it.
2. **n = 5, 3, 5 per regime.** The CIs are honest but wide; the large regime's
   upper bound crosses 1.0.
3. **The large regime does not meet the preferred target** and provably cannot.
4. **A3 rests on 96.7%** reproduction, with all residual variance traced to one
   event's runtime cache state (`IREC_PROMOTION_CONDITIONS.md` §1). Hosted
   providers were 30/30, but that is an observation, not a contract.
5. **`run_python` is not replay-safe.** This workload never calls it; a workload
   with irreversible external effects needs a suppression layer first.

### Recommended sequence

1. Resolve §7 (C3). It is free and offline.
2. Keep `recovery_strategies=("causalline",)` as the default until then.
3. On resolution, promote IREC to default **for workloads whose tools are
   idempotent**, retaining CausalLine as the fallback strategy and the
   comparison baseline, and keep both scored in every future campaign.
4. Do not delete self-report or counterfactual attribution: they are the
   instruments that detected the counterfactual's own false clean.

---

## 9. Campaign cost

| | |
|---|---|
| IREC model calls | **342** — local 294, NVIDIA 27, Gemini 21 |
| IREC tokens | **68,722** |
| wall clock | **5.3 minutes** (the local model's cache was warm; the first campaign took 53 min) |
| degraded providers | none |
| B0 / B1 / B2 / CausalLine | **0 new tokens** — read from the recorded rows |
| avoided | **875,839 tokens** against a from-scratch campaign |

IREC line running total: **183,042 tokens**.

### Reproducing

```bash
python docs/research/prototype/irec_campaign56.py          # all 15, resumable
python docs/research/prototype/irec_campaign56.py --limit 1  # one placement
```

Records in `data/runs/irec-campaign56/`, preserved under
`data/results/irec-campaign56/`. Needs `GEMINI_API_KEY` and `NVIDIA_API_KEY_1`,
and Ollama serving `llama3:latest`. The consolidation is offline and free.
