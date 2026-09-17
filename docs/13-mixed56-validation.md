# The 56-agent validation: 5 seeds x 3 regimes, on the fixed code

> **SUPERSEDED AS THE PRIMARY RESULT (18-09-2026).** This campaign repeats a
> *fixed* workload five times per regime, so it measures model
> non-determinism rather than workload variation -- the limitation its own
> Sec 9 states. `docs/14-workload-varied-validation.md` runs the same 15-cell
> design with 15 *distinct* attack placements and is the result to quote.
>
> This document is kept, not retired: the fixed-workload control is what
> establishes that the medium regime's sd of 0.00 is determinism rather than a
> defect, and that two runs escalate where three identical ones do not. Its
> headline (13 wins, 2 ties, 0 losses) is **more flattering than the varied
> campaign**, which records a loss -- that difference is the point.

> This supersedes sections 6-12 of `docs/real_mixed_model_60_agent_experiment.md`,
> which reported a single run per regime taken before issue #20 was fixed.
> That document's architecture, Phase 0 findings and fault log (sections 0-5c)
> stand unchanged and are the reference for *what* was run.

## A. What this is, and what was frozen

One question: **does fine-grained, influence-aware recovery identify a smaller
recovery region than coarse topology-based recovery, and does that translate
into additional preserved correct work?**

Everything that could be tuned to flatter the answer was fixed before the first
run and not touched afterwards:

| frozen | |
|---|---|
| agent topology | 56 agents, six stages, 145 events |
| provider assignment | 50 local `llama3:latest`, 2 `gemini-3.8-flash`, 4 NVIDIA nemotron |
| attack payloads | one per channel, unchanged, canary `ZZ999` |
| regime definitions | placements in `MixedScenario.build` |
| baselines | B0 / B1 / B2 as already implemented |
| CausalLine configuration | default; restart remains escalation, never primary |
| evaluation metrics | `RecoveryScore` as already implemented |
| detector | oracle, as in the earlier campaign |
| selection criteria | every completed run is included |

The only thing this pass decides is **how many times**. Seeds are a fixed
list (`src/eval/mixed_validation.SEEDS`), written down rather than drawn from a
clock.

## B. Why repetition was necessary rather than nice

The single pre-fix run of the `small` regime returned **CausalLine 0%**,
escalated all the way to `restart_all` after verification failed twice. Across
the fifteen runs below, the same regime returns **92.4%-93.8%**, and never
worse.

The models are non-deterministic, and the end-to-end check is 12 codes exactly,
in order. Whether a replay satisfies it is partly a dice roll, and the ladder
responds by climbing a rung. In these fifteen runs that happened twice (Sec 8),
costing the gain on those runs but not the outcome. In the single pre-fix run
it happened twice in one run and cost everything.

**So a single run of this workload measures the draw as much as the method.**
Both the earlier "+17.2 pts" and the earlier "0%" were unstable readings of a
stable system, in opposite directions. That is what this document exists to
replace -- and the replication also exposes a second problem with it, which
Limitation 1 states plainly: repeating a *fixed* workload measures model noise,
not workload variation.

## C. How to read the numbers

**Paired.** Each run scores all four methods on the same trace, under the same
attack, with the same seed. The quantity of interest is the per-run difference
`CausalLine - B2`, never the difference of two independently-averaged columns.

**Two axes, not one.** `work_preserved` and `unsafe_preservations` are not
independent: any mechanism that preserves more work is mechanically a candidate
for preserving something it should not. A gain with a non-zero unsafe count is
not a gain. The large regime's first measured "+17.2 points" was exactly that,
and it was the defect, not the method (`docs/12-issue20-correction.md`).

**Cost includes attribution.** CausalLine pays analysis *plus* replay; B2 pays
replay only. Preserved work is never quoted without the token cost beside it.

**No significance claim.** Five seeds per regime supports descriptive
statistics and a win/tie/loss count. It does not support a p-value anyone
should act on, and none is computed.

---
## 1. Final experimental table

**15 of 15 planned runs completed.**

| seed | regime | `f` true | escapes | B0 | B1 | B2 | CausalLine | CL-B2 | unsafe (CL) | delivered scope | task |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 20260917 | small | 6.2% | 0 | 0.0% | 92.4% | 92.4% | 93.8% | +1.4 | 0 | selective | OK |
| 20260918 | small | 6.2% | 0 | 0.0% | 92.4% | 92.4% | 93.8% | +1.4 | 0 | selective | OK |
| 20260919 | small | 6.2% | 0 | 0.0% | 92.4% | 92.4% | 92.4% | +0.0 | 0 | agent_restart | OK |
| 20260920 | small | 6.2% | 0 | 0.0% | 92.4% | 92.4% | 93.8% | +1.4 | 0 | selective | OK |
| 20260921 | small | 6.2% | 0 | 0.0% | 92.4% | 92.4% | 93.8% | +1.4 | 0 | selective | OK |
| 20260917 | medium | 54.5% | 0 | 0.0% | 40.0% | 39.3% | 45.5% | +6.2 | 0 | selective | OK |
| 20260918 | medium | 54.5% | 0 | 0.0% | 40.0% | 39.3% | 45.5% | +6.2 | 0 | selective | OK |
| 20260919 | medium | 54.5% | 0 | 0.0% | 40.0% | 39.3% | 45.5% | +6.2 | 0 | selective | OK |
| 20260920 | medium | 54.5% | 0 | 0.0% | 40.0% | 39.3% | 45.5% | +6.2 | 0 | selective | OK |
| 20260921 | medium | 54.5% | 0 | 0.0% | 40.0% | 39.3% | 45.5% | +6.2 | 0 | selective | OK |
| 20260917 | large | 72.4% | 0 | 0.0% | 15.2% | 14.5% | 27.6% | +13.1 | 0 | selective | OK |
| 20260918 | large | 72.4% | 0 | 0.0% | 15.2% | 14.5% | 27.6% | +13.1 | 0 | selective | OK |
| 20260919 | large | 72.4% | 0 | 0.0% | 15.2% | 14.5% | 14.5% | +0.0 | 0 | agent_restart | OK |
| 20260920 | large | 72.4% | 0 | 0.0% | 15.2% | 14.5% | 27.6% | +13.1 | 0 | selective | OK |
| 20260921 | large | 72.4% | 0 | 0.0% | 15.2% | 14.5% | 27.6% | +13.1 | 0 | selective | OK |

## 2. Per-regime analysis (paired, CausalLine - B2)

| regime | n | CL mean | B2 mean | paired diff mean | median | stdev | min | max | CL>B2 | CL=B2 | CL<B2 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| small | 5 | 93.5% | 92.4% | +1.10 | +1.38 | 0.62 | +0.00 | +1.38 | **4** | 1 | **0** |
| medium | 5 | 45.5% | 39.3% | +6.21 | +6.21 | 0.00 | +6.21 | +6.21 | **5** | 0 | **0** |
| large | 5 | 25.0% | 14.5% | +10.48 | +13.10 | 5.86 | +0.00 | +13.10 | **4** | 1 | **0** |

All figures in percentage points of work preserved. Paired: each difference is one run's CausalLine against the same run's B2, on the same trace and the same attack.

## 3. Aggregate analysis

- runs: **15**
- paired difference (CausalLine - B2): mean **+5.93 pts**, median +6.21, sd 5.07, range +0.00 to +13.10
- CausalLine > B2 in **13** runs, = in 2, < in **0**

**No significance test is reported.** Five seeds per regime, three regimes whose effects differ in sign, and a per-run difference whose spread within a regime is far smaller than the spread between them. A p-value computed over that would be arithmetic, not evidence.

## 4. Safety analysis

| regime | runs | unsafe (B0/B1/B2/CL) | closure escapes | payload landed | recall |
|---|---:|---|---:|---:|---|
| small | 5 | 0 / 0 / 0 / **0** | 0 | 5/5 | 1.0 |
| medium | 5 | 0 / 0 / 0 / **0** | 0 | 5/5 | 1.0 |
| large | 5 | 0 / 0 / 0 / **0** | 0 | 5/5 | 1.0 |

`unsafe` is event-level unsafe preservation: recovery kept an event that was truly contaminated. It must be 0. A preservation gain bought with a non-zero count here is not a gain, and the large regime's first measured '+17.2 pts' was exactly that (issue #20).

## 5. Economic analysis

CausalLine pays analysis **plus** replay; B2 pays replay only. The comparison is preserved work against total tokens, never preserved work alone.

| regime | CL analysis | CL replay | CL total | B2 total | CL/B2 cost | CL-B2 preserved | tokens per point gained |
|---|---:|---:|---:|---:|---:|---:|---:|
| small | 16291 | 1084 | 17374 | 1238 | 14.03x | +1.10 pts | 14,623
| medium | 15766 | 7839 | 23605 | 7755 | 3.04x | +6.21 pts | 2,554
| large | 15810 | 9272 | 25082 | 9272 | 2.71x | +10.48 pts | 1,508

## 6. Escalation and verification

| regime | selective delivered | verification failures | agent-restart | full restart | escalation budget spent |
|---|---:|---:|---:|---:|---:|
| small | 4/5 | 1 | 1 | 0 | 0 |
| medium | 5/5 | 0 | 0 | 0 | 0 |
| large | 4/5 | 1 | 1 | 0 | 0 |

## 7. External API usage

| regime | runs | Gemini calls | Gemini tokens | NVIDIA calls | NVIDIA tokens | local calls | degraded agents |
|---|---:|---:|---:|---:|---:|---:|---:|
| small | 5 | 46 | 23713 | 60 | 15005 | 799 | 0 |
| medium | 5 | 60 | 28585 | 120 | 27870 | 1230 | 0 |
| large | 5 | 62 | 28683 | 124 | 28094 | 1423 | 0 |
## 8. Failure cases

**Two runs of fifteen did not deliver selectively.** Seed 20260919 on `small`
and seed 20260919 on `large` both failed verification once and escalated to
`agent_restart`, where they delivered correctly. In both, CausalLine's preserved
work fell back to exactly B2's (+0.0), which is the designed behaviour: the
ladder gives up the gain rather than ship an uncertified recovery.

Worth naming precisely, because it is the mechanism the whole design rests on:
**verification failing is not recovery failing.** The 12-of-12 exact check is
satisfied by the models copying cleanly *and* by recovery removing the attack;
a fresh copy slip in the replay fails it for a reason that has nothing to do
with contamination. Only CausalLine is ever charged for this, because only
CausalLine checks. B1 and B2 shipped unverified recoveries in all 15 runs and
were never charged, and in these 15 they happened to be correct.

**No run required a full restart.** `restart_all` was never reached, and the
escalation budget was never exhausted.

**No run was degraded.** Zero agents fell back to the local model through quota
refusal or provider error across all 15 runs, so every run is the topology it
claims to be.

## 9. Limitations

1. **Three design points, replicated five times — not fifteen workloads.**
   This is the most important limitation and it is visible in the data:
   `f_true` is *identical* across all five seeds within each regime (6.2% /
   54.5% / 72.4%), and the medium regime's paired difference has standard
   deviation **0.00**. `MixedScenario.build` takes no seed: the corpus, the
   payloads and the poisoned placements are byte-identical across seeds. The
   seed reaches only the attributor and the models' own sampling.

   So the replication measures **model non-determinism**, which is real and
   worth measuring — two runs escalated where three identical ones did not —
   but it does **not** measure variation across workloads. The effective sample
   for the structural comparison is **n = 1 per regime**, confirmed five times.

2. **One topology.** 56 agents, one shape, one provider assignment. Nothing
   here says how the gain behaves as the hub fan-out, the stage depth or the
   provider mix change.

3. **Oracle detector.** The flagged set is exactly the planted set. This
   measures recovery after detection, which is the project's stated scope, and
   says nothing about detection.

4. **The task is a copy task.** Each agent moves a five-character code.
   `docs/real_mixed_model_60_agent_experiment.md` Sec 5b records why (D-090:
   on a small local model, capability failures rather than scoring artefacts
   blocked the recovery conclusion three campaigns running). It also means
   nothing here shows recovery works on hard tasks.

5. **Cost is measured in tokens, not money or latency.** The token ratios in
   Sec 5 are real; they are not a price comparison across three providers with
   different rates, and CausalLine's analysis calls are routed to the same
   model that produced the output, so a cheaper attribution model would change
   the economics and is untested.

6. **Scripted and real-LLM numbers are not mixed.** `CLAUDE.md` forbids it and
   nothing here does it. The scripted matrix
   (`docs/12-issue20-correction.md`) is a separate mode with ground truth known
   by construction.

## 10. Claims the data supports

- On this 56-agent, three-provider topology, under an oracle detector,
  **influence-aware recovery identified a strictly smaller recovery region than
  the topology closure in 13 of 15 runs and never a larger one.**
- **It preserved more correct work than B2 in 13 of 15 runs, tied in 2, and lost
  in 0**, with a mean paired difference of **+5.93 points**.
- The advantage **grows with the contaminated fraction**: +1.10 pts at
  f = 6.2%, +6.21 at f = 54.5%, +10.48 at f = 72.4%. That is the direction the
  method predicts — the more a reachability closure over-discards, the more
  there is to recover.
- **Safety held completely: 0 unsafe preservations for every method in every
  run, 0 contamination escapes from the structural closure, recall 1.0, and all
  15 runs delivered a correct final result.**
- The closure invariant (contamination never escapes `b2_topology_closure`)
  **holds on a topology it had never been tested on** — a broadcast hub, six
  stages, three attack channels including memory, which has no call-graph edge
  back to its writer.
- **Attribution is expensive and the overhead can erase the benefit
  economically**: CausalLine costs **14.0x** B2's tokens on the small regime to
  buy 1.1 points (14,623 tokens per point), against **2.7x** on the large
  regime for 10.5 points (1,508 tokens per point).
- **Restart escalation is necessary**: 2 of 15 runs could not certify a
  selective recovery and escalated to agent restart, where they delivered.

## 11. Claims the data does NOT support

- **Any claim about how often the gain appears across workloads.** Three design
  points, five replicates of model noise each. See Limitation 1.
- **Any statistical significance claim.** None is computed and none would be
  meaningful at this design.
- That the gain generalises to other topologies, other detectors, harder tasks,
  or a fleet that is mostly frontier models.
- That CausalLine is *cheaper* than the baselines. It is decisively more
  expensive in tokens in every regime; it preserves more work at higher cost.
- That CausalLine beats full restart in general. B0 preserved 0% in all 15 runs
  by definition, and restart remains the correct answer whenever the
  contaminated fraction approaches 1.
- Anything about detection.
## 12. Publication-readiness assessment

### Verdict: **B — ready to write, but one clearly defined experiment is still required**

Not A, and the reason is a single sentence in the data rather than a matter of
taste: **`f_true` is identical across all five seeds in every regime, and the
medium regime's paired difference has standard deviation 0.00.**
`MixedScenario.build` takes no seed, so the corpus, payloads and poisoned
placements are byte-identical across seeds. The replication varies model
sampling, not the workload.

That is a real and useful thing to have measured — it is how we know two runs
escalate where three identical ones do not — but it means the table is **three
design points confirmed five times**, not fifteen independent observations. A
reviewer will notice the zero standard deviation immediately and ask what the
seed changed. The honest answer is "not the experiment", and a quantitative
claim about how often the gain appears cannot rest on it.

### The one experiment required

**Vary the attack placement across seeds, holding the regime definition fixed.**

Concretely: make `MixedScenario.build` take the seed and use it to choose
*which* records, memory entries and verifiers are poisoned, subject to the
regime's contaminated-fraction band. Then five seeds give five genuinely
different workloads inside each regime and the within-regime spread becomes a
measurement instead of a constant.

Everything needed already exists — the placements are plain tuples in
`build()`, the bands are stated, and `measured_fractions` verifies after the
fact what a run actually reached. It is a change of a few lines plus a rerun of
the same 15 runs, on the same frozen architecture, at the same cost
(~4.5 hours, ~170 Gemini and ~300 NVIDIA calls).

### What is already strong enough to write

- The **safety result is unqualified and is the strongest thing here**: 0
  unsafe preservations, 0 closure escapes, recall 1.0, 15/15 correct outcomes,
  across three contamination regimes and three attack channels — one of which
  (memory) has no call-graph edge back to its writer.
- The **economics are honest and unflattering**, which is what makes them
  usable: 2.7x-14.0x the token cost, with cost-effectiveness improving as
  contamination grows. A paper that reports this is much harder to attack than
  one that reports preserved work alone.
- The **negative results are documented and kept** — Gate 1's falsification,
  issue #20's invalid +17.2, the scenario-B gain that went to zero. They are
  part of the story, not embarrassments to be pruned.
- The **narrow claim as stated in the brief is supported in every clause**,
  including the "however": attribution overhead does erase the benefit
  economically at low contamination, and restart escalation was necessary in 2
  of 15 runs.

### What must not be written

The word "significant", any p-value, any claim about the *frequency* of the
gain, and the number **31.7%** — the pre-fix large-regime figure that was
entirely a safety defect (`docs/12-issue20-correction.md` Sec 5).

### Method status

- **CausalLine remains the primary method.** It was not redesigned, retuned, or
  replaced. The only change in this pass was a provenance *recording* fix.
- **Restart remains the escalation fallback**, never the primary action:
  selective -> verification -> agent restart -> full restart. 13 of 15 runs
  delivered at `selective`, 2 at `agent_restart`, 0 needed `restart_all`.
- Gate 1 is **not** part of the contribution. Its falsification stands as
  recorded and no further campaign was spent on it.
