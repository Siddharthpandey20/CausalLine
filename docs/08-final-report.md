# 08 — Final Push Report

Covers Phases A–D of the Final Push brief. Supersedes `docs/07` where they
disagree; `07` remains the record of the integration sprint that preceded this.

Every number here was measured in this session. Where something was not run,
it says so and says what it would cost.

---

## 1. Verdict

**The classification moves up: from "C — functional prototype reporting honest
numbers, most of which are now worse" to "C+ / B− — functional prototype whose
central claim is now supported on every cell it is tested on, with its cost
story measured rather than assumed and its remaining failures understood."**

**Yes, the headline claim changed — it got stronger, and in the place that
mattered.**

Before this session, `docs/07` §7 opened with "CausalLine does not beat B1 in
general. On influencing scenarios under oracle it ties on B and loses on A and
C. Only the exposed-only claim survives." That sentence is now false. After
Phase A, CausalLine beats B1 on **all six** oracle cells, influencing included:

| scenario | before | after | B1 | gain |
|---|---|---|---|---|
| A influencing | 15.8% ± 0.0% | **44.2% ± 4.4%** | 21.1% | **+23.2** |
| A exposed-only | 93.7% ± 8.0% | **96.0% ± 4.8%** | 21.1% | +74.9 |
| B influencing | 57.9% ± 0.0% | **65.1% ± 1.0%** | 57.9% | **+7.2** |
| B exposed-only | 94.0% ± 4.8% | **95.6% ± 3.1%** | 57.9% | +37.7 |
| C influencing | 15.8% ± 0.0% | **66.5% ± 1.1%** | 52.6% | **+13.9** |
| C exposed-only | 93.0% ± 8.0% | **93.7% ± 6.3%** | 52.6% | +41.1 |

30 repetitions, 95% CIs, 96 cells, `data/results/campaign.json`.

The exposed-only column barely moved, which is the point: those wins always
came from the *provenance* model and were never in doubt. The influencing
column was where the recovery *planner* had to do the work, and it was failing
because Step 2 optimised a different condition from the one Step 4 verified.
One change fixed it.

Three things keep this at C+ rather than higher, and none is cosmetic:

1. **The analysis still does not pay for itself.** A/N is 1.23 at the short
   length under a real cost model — better than the 1.50 previously reported,
   and still above 1.
2. **Savings do not scale with workflow length.** Now measured rather than
   asserted: across a 42% longer trace, A/N is flat. `docs/06` §4 claims they
   scale. They do not.
3. **Real-model evidence is one channel and five pairs.** Everything else is a
   scripted client.

---

## 2. Master table

| # | Item | Status | Proof required → actually measured |
|---|---|---|---|
| **A** | Step 2 covers the contamination closure | **Done** | Covering target changed from source→output paths to `taint.events`. All six oracle cells now beat B1 (table above). Escalations across the deterministic matrix **22 → 16**. A/N **1.67 → 1.50** (flat model). `greedy_cover`, its cost function and the NP-hardness framing untouched |
| A.1 | Escalations drop for oracle | **Done** | Every oracle influencing cell **1 → 0** escalations, `scope=agent_restart` → `scope=selective` |
| A.2 | Escalations may rise for pessimistic | **Did not happen** | Pessimistic A and C were **already** 0% / `scope=exhausted` before the change; pessimistic B *improved* 57.9% → 63.2%. **No cell regressed anywhere in the matrix** |
| A.3 | Tests green with updated expectations | **Done** | 201 → 204. **No expectation needed updating** — which was itself the finding; see §3.1 |
| **B.1** | Live token validation, 3 channels | **Partial — 1 of 3** | `web` completed: **3/5 (60%)** agreement, **0 unsafe**, payload landed. `memory` died on its 2nd call, `agent_message` never ran |
| B.2 | Re-record cassette | **Not done — quota** | Needs 6 live requests |
| B.3 | Noise floors, 4 comparators | **Not done — quota** | Attempted today for `prose`. **0 trials collected**, all 8 requests 429'd. Nothing partial written |
| **C** | Longer / branching workflow | **Done** | `long` = 27 events, 22 sources, 6 agents, 8 checkpoints, mean exposures 5.8 (short: 19 / 15 / 5 / 4 / 3.8). Opt-in; short re-measured at **0 differing rows** across all 96 |
| C.1 | Does SPRT fire? | **Done — yes** | **9 observations, `proceed_selective`.** Trajectory `[+0.847, 0.0, −0.847, −1.695, −2.542, −3.389, −4.236, −5.084, −5.931]`, crossing −2.197 at observation 5. Short: 3 observations, `continue` |
| C.2 | Does `P` still saturate? | **Done — and it was the wrong quantity** | Per-node `P` **varies at both lengths** (6 → 8 distinct values, max 0.920 → 0.978). `run_probability`, the aggregate, is **1.000 at both** and gets there faster with more exposures |
| C.3 | GC freed bytes on longer trace | **Done — still 0** | 0 bytes at 4, 8, 9 and 15 checkpoints. See §3.4 |
| C.4 | A/N at new length vs old | **Done** | short 1.50 → **1.23**, long 1.78 → **1.24** under proportional cost. **Flat, not improving** |
| **8.1** | Non-flat token cost model | **Done** | `cost_model="proportional"`, billed by rendered length. Flat's pessimism quantified at **~22%** |
| **D.1** | Young/Daly interval wired | **Done** | Interval **2.66 events** (short) / **2.41** (long) vs ~4.75 / ~3.38 boundary spacing. Checkpoints 4 → 9 and 8 → 15; checkpoint bytes roughly double |
| **D.2** | README, pyproject, lock, CI | **Done** | Zero declared runtime deps + 3 extras; `requirements-lock.txt` pins what measurements used; CI runs bare-interpreter and analysis-extra jobs separately |
| D.3 | Suite runs offline | **Done — was false when claimed** | Blocked numpy/matplotlib to check: 6 tests failed with `ImportError` rather than skipping. Guarded. **206 passed / 7 skipped** bare, **212 passed / 1 skipped** with numpy |

Test suite: **201 → 212 passing**, 1 skipped. Two new files
(`test_step2_covers_step4.py`, `test_checkpoint_interval.py`).

---

## 3. New gaps and findings

### 3.1 Nothing in the suite pinned the Step 2 / Step 4 relationship

All 201 tests passed *unchanged* after the covering target was replaced. The
relationship between the step that plans a recovery and the step that verifies
it was asserted by no test at all. `tests/test_step2_covers_step4.py` now pins
`invalidation_set ⊇ taint.events` across every scenario × variant × detector.

### 3.2 A number in `docs/07` does not reproduce

`docs/07` §4.1 gives the shipped planner's A-influencing/**pessimistic** as
**68.4%**, and that figure is the entire reason "neither arrangement dominates"
was the conclusion — it made option (c) look like a trade. Re-measured on the
shipped code at both the default workdir and a fresh one (with the change
stashed): **0.0%, 2 escalations, `scope=exhausted`**.

With the correct figure, covering the closure dominates on every cell measured
and the decision was never a trade-off. Recorded rather than quietly corrected,
because it is why this looked harder than it was.

### 3.3 `read_trace()` was destroying the trace header

`refine_for_verdict()` reopens the log with
`append=True, meta={"record_kind": "refinement"}`, writing a **second** meta
record — and the reader **replaced** rather than merged. Every trace that went
through refinement, which is the entire hybrid campaign path, lost its model,
task, attributor and settings fingerprint.

It surfaced twice: the live token-validation result had to be labelled
`gemini-3.6-flash` by hand because `score_run()` read `"unknown"`, and replay
could not read the workflow shape it needed. Fixed by merging.

### 3.4 GC is inert by precondition, not by density — the `docs/07` prediction is wrong

`docs/07` §3a predicted "GC and the interval are complementary and neither does
anything alone — the interval would create the denser stream GC exists to
bound." Phase D wired the interval. The stream got denser. **GC still freed
zero bytes**, at four densities.

Four measurements, each ruling out one explanation:

| configuration | pairs cleared | bytes freed |
|---|---|---|
| attacked run, hybrid estimator | 79/156 (51%) | 0 |
| attacked run, `audit_rate=1.0` | 93/156 (60%) | 0 |
| **clean** run, `audit_rate=1.0` | 87/156 (56%) | 0 |
| clean run, `accept_self_report=True` | 129/156 (83%) | 0 |

Not the attack, not estimator coverage, not the clearance policy, not
checkpoint count. By event kind: `tool_call`, `tool_response`, `message` and
memory events clear at 96–100%; `agent_output`, `decision` and `plan` clear at
**0%**.

`confirmed_clean()` requires every exposure pair in a prefix to be **cleared**,
and clearing means *showing a pair did not influence*. A pair that genuinely
did influence is therefore never cleared — correctly — and one such pair
anywhere blocks that checkpoint permanently. Real influence is not an anomaly;
it is what a working agent run is made of.

GC's precondition is "a prefix provably free of influence", which a useful run
never has. The conservative direction is right — rewinding to a checkpoint
whose prefix carries contamination would carry it forward — but this is a
**design tension to state**, not a mechanism that starts working at scale.

### 3.5 Counterfactual attribution has a redundancy blind spot, and workflows walk into it

Building the Reviewer produced two independent instances of the same failure:
put a summary and its own inputs in one agent's context, and single-source
leave-one-out reports that **neither** influenced anything. The first time, the
Reviewer's `agent_output` had *no influence edges at all*, contamination
stopped before the Executor, and every influencing run escalated to
`restart_all` at 0% preserved.

`_answer_task` already documents this as a property of counterfactual
influence. What is new is that a pipeline can reach it just by being generous
with context, that it **silently severs the contamination chain**, and that it
gets worse as workflows lengthen. It still bites at the Coder in the long
workflow: with five findings in context instead of three, no single finding is
necessary, the script is attributed to the memory source alone, and
A-influencing/oracle escalates once — **11.1% against B1's 14.8%**. The
exposed-only cell is unaffected: **92.6% against 14.8%**.

This is a limitation of the **estimator**, not of the recovery method, and
subset testing is exponential. It is the most paper-relevant finding here.

### 3.6 A day's free-tier quota buys exactly one channel

`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, limit **20/day**, read off
the 429's quota metric. The `web` channel cost **exactly 20 recorded calls** —
not the ~24 `request_budget()` estimates. So one channel fits in one day, and
only just.

`run_live()` let `QuotaExhausted` propagate out of the whole function,
discarding the completed `web` scenario unscored and unwritten. A real
measurement costing a full day's quota was recovered only by scoring the trace
off disk afterwards. It now returns `(results, unfinished_channels)`, scores
each scenario as it finishes, merges into `token-validation.json` so a
multi-day run accumulates, and prints the `--channels` command to resume with.

### 3.7 The offline-suite claim was false

Stated in the README, then checked by blocking `numpy`/`matplotlib` with an
import hook: six tests failed with `ImportError` rather than skipping.
`requirements.txt` argues explicitly that "a research prototype that cannot run
its own test suite offline is worse than one with an optional component", and
the suite was quietly not meeting it. Guarded and now enforced by CI in a
separate job.

---

## 4. Claims that can safely be made

1. **Exposure is not influence, and it is worth real work.** 93.7–96.0% work
   preserved on exposed-only incidents against B1's 21.1–57.9%, 30
   repetitions, 95% CIs.
2. **CausalLine beats B1 on every oracle cell**, influencing included:
   +7.2 to +74.9 points. *(New. This could not be said before.)*
3. **Zero event-level unsafe preservations**, all 24 campaign cells × 30
   repetitions, under four detectors including a blind control and a real
   classifier, and under an adversarial self-reporter.
4. **Safety survives total detection failure.** At a simulated miss rate of 1.0
   CausalLine still records 0% unsafe; B1 and B2 record 50%.
5. **Step 2 now provably satisfies Step 4 by construction**, pinned by a test
   across every scenario × variant × detector.
6. **SPRT is an active mechanism at 27 events**, with a published trajectory
   and an early abort. *(New — it had never fired.)*
7. **The flat cost model was ~22% pessimistic**, now quantified rather than
   guessed.
8. **Group testing reduces counterfactual cost by 15%** on the inline path,
   with a measured applicability floor of ~4 candidates. *(Unchanged.)*
9. **A detector socket with two real, non-simulated occupants** and measured,
   complementary failure modes. *(Unchanged.)*

## 5. Claims that must NOT be made

1. **That the analysis pays for itself.** A/N is 1.23 at best. Above 1
   everywhere measured.
2. **That savings scale with workflow length.** Measured flat (1.23 → 1.24)
   across a 42% longer trace. `docs/06` §4 still asserts scaling; it is wrong
   and should be corrected.
3. **That checkpoint GC bounds storage in practice.** It frees 0 bytes at every
   density measured, for a structural reason (§3.4).
4. **That the attack-probability gate contributes anything.** `run_probability`
   saturates to 1.000 at both lengths.
5. **Anything general about real-model behaviour.** One channel, five pairs,
   one run. The 60% point estimate has an enormous interval and no claim should
   lean on it. What it does establish: the pipeline runs end-to-end against a
   real model, and the safe-direction property survived first contact — both
   errors were false positives, neither was a false clean.
6. **That the estimator handles redundant context.** It does not, measurably
   (§3.5), and it degrades as workflows lengthen.
7. **That the long workflow's influencing performance is good.** It is 11.1%
   against B1's 14.8% on A/oracle — CausalLine *loses* that cell. The cause is
   understood (§3.5) and is the estimator, not the planner.
8. **Optimality of anything.** Unchanged.

---

## 6. What is left, and what it costs

**Blocked on quota only** (free tier, 20/day, no billing):

| priority | item | cost | command |
|---|---|---|---|
| 1 | `memory` channel | 20 req = 1 day | `python -m src.eval.token_validation --channels memory` |
| 2 | `agent_message` channel | 20 req = 1 day | `... --channels agent_message` |
| 3 | `prose` noise floor | ~8 req | `python -m src.provenance.noise --call 1 --trials 8` |
| 4 | `code` / `json_shape` floors | ~16 req | `--call 5`, `--call 0` |
| 5 | re-record cassette | 6 req | artifact, not a measurement |

Results accumulate across days; `collect()` is resumable and cost nothing when
it hit the wall today.

**Not blocked, and genuinely open:**

- **The redundancy blind spot** (§3.5). Needs a design decision, not a fix:
  subset testing on small candidate sets, a redundancy-aware comparator, or
  accept and report it. Recommendation: report it — it is a real result and the
  fix is research.
- **GC's precondition** (§3.4). Weakening `confirmed_clean` from *every pair
  cleared* to *every pair examined* would make GC collect, but needs a safety
  argument that rewinding past a known contamination is sound because recovery
  replays it anyway. Plausible; out of scope here.
- **`docs/06` §4** asserts scaling that is now measured false. Correct it.
- The long workflow's influencing cells, which follow from the redundancy
  decision above.
