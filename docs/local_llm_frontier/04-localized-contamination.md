# Local LLaMA frontier — 4. Localized contamination, and the first measured win

**This is the first time in this project that `A/N + f` has come out below 1.00
on runs that were actually executed.** It is also the first time CausalLine has
*delivered* a selective recovery instead of escalating to a full restart.

Both results come from a workflow shape that did not exist until now, and the
honest reading of them depends entirely on understanding why that shape was
needed. §5 is the list of things this does **not** show.

Regenerate: `python -m src.eval.fanout_campaign --repeats 3` then
`python -m src.eval.fanout_report`. Raw:
`data/results/fanout/fanout-campaign.json`.

---

## 1. Why a new workflow shape was necessary

Every measurement in this project until now used one shape: a **chain**
(Researcher → Coder → Executor). On a chain, a poisoned source read near the
front contaminates everything after it, so the contaminated fraction `f` is
large *by construction*. `docs/local_llm_frontier/03` §3 measured exactly that:
a 26% longer workflow moved `A/N + f` by **0.00**, because lengthening a chain
lengthens the contaminated part of it too.

CausalLine's claim has always been that it pays when contamination is
**localized**. That case had never been built, so the claim had never been
tested — the project had only ever measured itself in the regime where it
cannot win.

**The fan-out shape** (`src/tracing/fanout.py`): `K` analysts, each fetching and
reading its **own** document; an aggregator that collects the findings; an
executor that compares. Exactly one analyst's document carries a planted note.

The contaminated region is then that analyst's output, the aggregator, and the
final output — **three events, whatever `K` is** — against a trace of `3K + 2`:

| K | events | region | f |
|---|---|---|---|
| 4 | 14 | 3 | 0.21 |
| 8 | 26 | 3 | 0.12 |
| 16 | 50 | 3 | **0.06** |

`f` is a dial for the first time, and `N` grows while the analysis does not.

---

## 2. Two faults this shape exposed, both found by measurement and both against us

Neither was predicted; both were caught because the numbers looked wrong.

**1. The baselines were not running.** Documents were logged with no
`origin_event`, and `baselines.entry_events` locates a flagged source by exactly
that. B1 and B2 therefore discarded **nothing at all** — `blast=0`,
`preserved=1.00` — and CausalLine was being compared against two baselines that
were silently doing no work. That would have flattered it enormously. Documents
now arrive through a tool call, as web sources do everywhere else in the project.

**2. The attack destroyed the information it poisoned.** The payload was
spliced into the only document carrying the required fact, so redacting the
flagged source removed the fact too, every replay produced a wrong answer,
verification refused it and CausalLine escalated on every run. The payload is
now a **separate planted source** beside the clean report — which is how every
other scenario in this project already plants an attack
(`GeneratedScenario.apply` appends a page; it does not corrupt one).

Only after both fixes did the shape measure anything.

---

## 3. Results — 24 runs, 4 design points, 2 arms, 3 repetitions

`llama3.2:3b`, 100% on GPU, 694s wall clock, concurrency 1.

### 3.1 Coverage, and the control

| design | K | arm | n | landed | task ok | events |
|---|---|---|---|---|---|---|
| fan04-inf | 4 | eager / lazy | 3 / 3 | 3 / 3 | 0 / 0 | 14 |
| fan08-inf | 8 | eager / lazy | 3 / 3 | 3 / 3 | 0 / 0 | 26 |
| fan16-inf | 16 | eager / lazy | 3 / 3 | 3 / 3 | 0 / 0 | 50 |
| fan08-exp | 8 | eager / lazy | 3 / 3 | **0 / 0** | **3 / 3** | 26 |

**The control never landed: 0 of 6.** Present, flagged, instructing nothing, and
it never influenced an output.

**`task ok` is 0 on every attacked run and 3/3 on every control.** That pair is
the whole answer to `docs/03` #15, and it is the *opposite* of the chain
result. The attacked runs fail the task **because the attack worked** — the
canary replaced a value — which is a correct measurement, not a broken testbed.
The controls prove the 3B model can do this task when nobody is attacking it.

### 3.2 Delivered recovery — and it is *delivered*, not identified

Landed runs only. `preserved` is what the **executed** plan kept, after any
escalation (D-086).

| arm | method | preserved | escalated | blast | tokens | unsafe |
|---|---|---|---|---|---|---|
| eager | B0 full restart | 0.0% | 0/9 | 30.0 | 1391 | 0 |
| eager | B1 agent taint | 82.7% ±0.06 | 0/9 | 4.0 | 331 | 0 |
| eager | B2 topology closure | 78.4% ±0.07 | 0/9 | 5.0 | 331 | 0 |
| eager | **CausalLine** | **87.0% ±0.04** | **0/9** | **3.0** | 3579 | 0 |
| lazy | B0 full restart | 0.0% | 0/9 | 30.0 | 1391 | 0 |
| lazy | B1 agent taint | 82.7% ±0.06 | 0/9 | 4.0 | 331 | 0 |
| lazy | B2 topology closure | 78.4% ±0.07 | 0/9 | 5.0 | 331 | 0 |
| lazy | **CausalLine** | **87.0% ±0.04** | **0/9** | **3.0** | **1357** | 0 |

**Zero escalations, on all 18 landed runs, in both arms.** On the chain
workflow it escalated 17 of 17. And it is not that verification became lenient:
**all 18 CausalLine selective replays passed the task check** — the replay,
with the planted source redacted, produces the correct values.

Blast radius is a **constant 3** against B0's 30. That constant is the shape's
whole point.

### 3.3 The break-even number, measured

`A` = analysis tokens. `N` = B0's full-restart tokens. `f = 1 − delivered`.

| arm | K | n | A | N | A/N | f | **A/N + f** | |
|---|---|---|---|---|---|---|---|---|
| eager | 4 | 3 | 1647 | 632 | 2.61 | 0.21 | 2.82 | loses |
| eager | 8 | 3 | 2851 | 1200 | 2.38 | 0.12 | 2.49 | loses |
| eager | 16 | 3 | 5248 | 2340 | 2.24 | 0.06 | 2.30 | loses |
| lazy | 4 | 3 | 850 | 632 | 1.34 | 0.21 | 1.56 | loses |
| lazy | 8 | 3 | 983 | 1200 | 0.82 | 0.12 | **0.93** | **WINS** |
| lazy | 16 | 3 | 1246 | 2340 | 0.53 | 0.06 | **0.59** | **WINS** |

**Both levers are required, and neither is sufficient.**

- Localized contamination alone (eager arm) never wins: 2.30 at best. `f` falls
  to 0.06 and it still loses, because `A` is 2.24 × `N`.
- Lazy self-report alone is not enough either: at K=4 it is 1.56.
- Together, at K≥8, the condition is met.

Note *why* the lazy arm scales: `A` grows from 850 → 1246 while `N` grows from
632 → 2340. Deferring the self-report ties the analysis to the **contaminated
region**, which is constant, instead of to the **workflow**, which is not.
The eager arm's `A` grows 1647 → 5248 because it asks every event of every run.

### 3.4 The projection, checked

`docs/local_llm_frontier/03` §3 projected **0.92** by subtracting the measured
self-report cost from runs collected with it switched on.

| | |
|---|---|
| projected | 0.92 |
| measured, lazy K=8 | **0.93** |
| measured, lazy K=16 | **0.59** |

**The projection held, and at K=8 it was accurate to 0.01.** At K=16 the real
result is 0.33 better than projected, because the projection assumed `f = 0.40`
from the chain workflow and localized contamination brings it to 0.06.

### 3.5 Paired sign test, on delivered work

| comparison | record | mean delta | p |
|---|---|---|---|
| CausalLine vs B0 | **18W–0L–0T** | +87.0 pts | **0.00001** |
| CausalLine vs B1 | **18W–0L–0T** | +4.3 pts | **0.00001** |
| CausalLine vs B2 | **18W–0L–0T** | +8.7 pts | **0.00001** |

Unanimous. And unlike every previous campaign in this project, **this is the
delivered number, not the identified one.**

### 3.6 Safety

Zero unsafe preservations for all four methods.
**Safety was tied. It is not a result, and it is not evidence that CausalLine is
safer than B1.** The distinguishing results here are delivered work
preservation and cost.

---

## 4. What changed in the algorithm to get here

Three wirings, all of which existed and none of which reached an experiment:

1. **D-087 — the SPRT's hypotheses now come from the cost model.**
   `config_for` had no caller outside its own tests, so every investigation this
   project ever ran used `f_star = 0.3 / 0.7`, numbers nobody measured. Both
   inputs are now read off the trace.
2. **D-088 — the planner's cap, examined rather than changed.** Adding the
   already-spent analysis to it was requested and is *strictly worse*: `A` is
   sunk and cancels, so counting it only turns cheap selective replays into full
   restarts — 3367 tokens worse per run on the chain campaign's own numbers, and
   never better. It is a switch, off by default, with the arithmetic in a test.
3. **D-089 — lazy self-report reached a campaign.** Built under D-082 and
   default-off ever since. It is the single largest effect measured here:
   `A` at K=16 falls from 5248 to 1246, a **76% reduction**, with identical
   preservation, identical blast radius and identical safety.

---

## 5. What this does NOT show

**Read this section before quoting any number above.**

1. **It does not overturn the chain result.** On chain-shaped workflows
   selective recovery is still more expensive than restarting — measured on
   three models, and `docs/local_llm_frontier/02` stands unchanged. The claim is
   conditional: *when contamination is localized and the self-report is
   deferred*, CausalLine wins.
2. **The testbed was built for this regime.** The fan-out shape was designed
   because the method claims to pay under localized contamination. That is a
   legitimate test of a conditional claim, but it is not an independent
   discovery — this is a workflow chosen to have the property, and it must be
   presented that way, not as a general result.
3. **n = 3 per cell.** 24 runs total. The sign test is unanimous and exact, but
   the per-cell means have three points behind them and the intervals shown are
   suppressed below n = 3 for exactly that reason.
4. **One model, one workflow family, one attack channel** (`web`), one poisoned
   analyst out of K. Redundant-source cases are still not isolated
   (`docs/03` #15's sibling, `docs/06` §2.2).
5. **Safety was tied, so nothing here is a safety result.**
6. **The task is easier than the chain's on purpose.** Extraction, not code
   generation. That is what makes verification able to certify a replay at all
   (§3.1) — but it also means the recovered work is cheap to recompute, and a
   workload of genuinely expensive work has still not been tested.
7. **`A/N + f < 1` is a token result.** It says nothing about wall clock,
   latency, or the irreversible side effects that would make preserved work
   worth more than tokens.

---

## 6. What is still owed

1. **A second model on this shape.** Everything here is `llama3.2:3b`.
2. **More repetitions**, and more poisoned-analyst positions than index 0.
3. **The redundant-source case**, still not isolated on any frontier.
4. **A workload where the preserved work is expensive**, which is the condition
   under which the *identified*-preservation result from the chain campaigns
   would also become a cost result.
5. **The hosted frontier on the fan-out shape**, which would turn a one-model
   result into a two-model one.
