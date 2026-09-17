# H2 — adversarial break test

**Verdict: FALSIFIED.**

On a 14-case suite built to attack its assumptions, H2 scores **64%** —
**below the 66–71% of `G_STRUCT`, the gate it was meant to replace** — with
**3 false recoveries** where the previous environmental run reported zero. The
minimal counterexample is **five events**.

H2 was not modified, tuned, or repaired at any point in this experiment. No LLM
was run. `src/recovery/` is untouched.

| | |
|---|---|
| cases | 14 |
| tests | **380 pass** (5 new, preserving the counterexample) |
| production changes | **none** |

---

## 1. The smallest counterexample

Five events. A hub with two analysts. One planted source.

```
probe(S1, e0003) -> CLEAN
  removable?           True     <- the guard PASSES
  truly influences?    True     <- the guard was WRONG
  comparator can see?  False

true contaminated events: 3 of 5
N = 300   A = 100   R = 300     oracle = RESTART
H2 = INVESTIGATE                3 contaminated events preserved
```

Preserved as
`tests/test_gate1_sim.py::TestH2IsFalsifiedByComparatorBlindness`. It
reproduces identically at K=2, 3, 4 and 8.

### The mechanism

H2's safety argument is that `removability.check()` guards against believing an
unsound clean verdict. **It does — for the failure mode it checks, and that is
not the only one.**

```
removability asks:   does redacting this source remove its information?
the probe needs:     would the comparator NOTICE if the output changed?
```

These are different questions. D-026 removed text comparison from the
comparator deliberately, so a decision signature can miss a real semantic
change. A source can therefore be **perfectly removable** — redaction genuinely
deletes the information — while the counterfactual still returns *clean*
because the comparator does not register the resulting change.

Redundancy (`docs/03` #16) makes the verdict unsound *and* leaves a signature:
non-removability. Comparator blindness makes the verdict unsound and leaves
**no signature at all**. The guard has nothing to catch it by.

## 2. Suite and raw results

`nonrem` = pairs `removability.check()` would reject.

| case | group | N | A | R | A/N | f_true | f_struct | nonrem | oracle |
|---|---|---|---|---|---|---|---|---|---|
| R1-cmpblind08 | removability | 900 | 100 | 900 | 0.11 | 1.00 | 1.00 | **0** | RESTART |
| R2-cmpblind16 | removability | 1700 | 100 | 1700 | 0.06 | 1.00 | 1.00 | **0** | RESTART |
| R3-deephidden | removability | 1700 | 101 | 100 | 0.06 | 0.06 | 1.00 | 1 | INVESTIGATE |
| R4-fanin-redun | removability | 1700 | 100 | 200 | 0.06 | 0.12 | 0.12 | 17 | INVESTIGATE |
| B1-hubtaintloc | selection | 1700 | 1700 | 100 | 1.00 | 0.06 | 1.00 | 0 | RESTART |
| B2-hubtaintloc24 | selection | 2500 | 2500 | 100 | 1.00 | 0.04 | 1.00 | 0 | RESTART |
| B3-mhub-one | selection | 1800 | 900 | 200 | 0.50 | 0.11 | 0.50 | 0 | INVESTIGATE |
| D1-hub-redun | redundancy | 1700 | 100 | 1700 | 0.06 | 1.00 | 1.00 | 17 | RESTART |
| D2-chain-redun | redundancy | 400 | 100 | 200 | 0.25 | 0.50 | 1.00 | 2 | INVESTIGATE |
| N1-blocked08 | fallback | 900 | 100 | 0 | 0.11 | 0.00 | 1.00 | 10 | INVESTIGATE |
| N2-blocked24 | fallback | 2500 | 100 | 0 | 0.04 | 0.00 | 1.00 | 26 | INVESTIGATE |
| E1-hub-one08 | economic | 900 | 900 | 200 | 1.00 | 0.22 | 1.00 | 0 | RESTART |
| E2-chain-one | economic | 400 | 300 | 200 | 0.75 | 0.50 | 1.00 | 0 | RESTART |
| M1-mhub-none | multihub | 1800 | 100 | 0 | 0.06 | 0.00 | 0.50 | 0 | INVESTIGATE |

### Required comparison

| gate | acc | false RST | false REC | probe tok | vs restart | vs oracle | unsafe | preserved |
|---|---|---|---|---|---|---|---|---|
| B0 always restart | 50% | 7 | 0 | 0 | 1.00 | 1.72 | 0 | 0 |
| B1 always investigate | 50% | 0 | 7 | 0 | 0.62 | 1.07 | 48 | 15000 |
| **G_STRUCT (falsified)** | **71%** | 4 | 0 | 0 | 0.82 | 1.40 | **2** | 4900 |
| **H2 sound bottleneck** | **64%** | **2** | **3** | 1000 | 0.77 | 1.32 | **31** | 6900 |

**H2 is beaten by the gate it was built to replace**, on accuracy (64% vs 71%),
on unsafe preservations (31 vs 2), and it pays 1000 probe tokens to get there.
It wins only on preserved work (6900 vs 4900) — by investigating cases it
should not have.

### H2 per case

| case | H2 | oracle | probes | verdict |
|---|---|---|---|---|
| R1-cmpblind08 | INV | RST | 1 | **FALSE RECOVERY** |
| R2-cmpblind16 | INV | RST | 1 | **FALSE RECOVERY** |
| R3-deephidden | INV | INV | 1 | ok |
| R4-fanin-redun | INV | INV | 0 | ok |
| B1-hubtaintloc | RST | RST | 0 | ok |
| B2-hubtaintloc24 | RST | RST | 0 | ok |
| B3-mhub-one | INV | INV | 0 | ok |
| D1-hub-redun | RST | RST | 0 | ok |
| D2-chain-redun | INV | INV | 1 | ok |
| N1-blocked08 | RST | INV | 0 | **FALSE RESTART** |
| N2-blocked24 | RST | INV | 0 | **FALSE RESTART** |
| E1-hub-one08 | RST | RST | 1 | ok |
| E2-chain-one | INV | RST | 3 | **FALSE RECOVERY** |
| M1-mhub-none | INV | INV | 0 | ok |

## 3. Failure-mode classification

| id | mode | result | classification |
|---|---|---|---|
| **F1** | false recovery | **3** (R1, R2, E2) | **hypothesis falsification** |
| **F2** | contaminated work preserved after H2 says investigate | **yes** — 9 and 17 events on R1/R2 | **hypothesis falsification** |
| **F3** | spends probe cost and is still wrong | **yes** — E2 spent 3 probes and still made a false recovery | hypothesis falsification |
| **F4** | misses a profitable case because the bottleneck was uninformative | **not observed** | insufficient test construction — see §5 |
| **F5** | removability guard gives a false sense of safety | **yes, decisively** — R1/R2 have `nonrem = 0` | **hypothesis falsification** |
| **F6** | fallback recreates the falsified failure | **yes** — N1/N2, `A/N` 0.04–0.11, `f_true` 0.00 | expected by design, but it *is* the original failure returning |
| **F7** | works only because the distribution matched the heuristic | **yes** — 94% on the previous suite, 64% here | **hypothesis falsification** |

## 4. The safety invariant

> A gate must never claim a recovery is safe that the underlying investigation
> itself would classify as unsafe.

**Not violated: 0 violations in 14 cases**, tested two ways — with H2's probe
verdicts discarded, and with them **reused** by the investigation (the
deployment a real system would build, since those counterfactuals cost money).

| | B1 unsafe | H2 isolated | H2 reused |
|---|---|---|---|
| R1-cmpblind08 | 9 | 9 | 9 |
| R2-cmpblind16 | 17 | 17 | 17 |
| D1-hub-redun | 17 | **0** | **0** |
| all others | equal | equal | equal |

**But the invariant holds trivially, and that matters more than the pass.** The
gate uses the *same probe* as the investigation, so its evidence is a strict
subset of the investigation's; it cannot conclude anything the investigation
would not. The invariant is close to unfalsifiable in this architecture, so
passing it is **weak evidence and should not be quoted as a safety result**.

The number that actually describes H2's safety behaviour is the unsafe total:
**31 against G_STRUCT's 2**, because H2 chooses to investigate in cases where
the investigation's own blind spots then fire.

## 5. What was attacked and what was not

**Attacked and broken:** the removability assumption (§1), the fallback (N1/N2),
economic flip stability (E2), and distributional robustness (F7).

**Attacked and survived:** redundancy proper (D1 — H2 correctly restarts where
H1 did not; D2 correct), multi-hub (B3, M1 correct), deep hidden influence (R3
correct).

**Attacked but the case could not be constructed: F4.** `hub_tainted_local` was
built to make the max-closure probe uninformative — real influence at the hub
that does not propagate, so only per-branch clearing could prune the closure.
It failed to be a counterexample for a structural reason worth recording: when
the hub is tainted, the investigation must check every downstream pair anyway,
so `A/N` rises to **1.00** and restart becomes genuinely correct. H2 restarting
is then right. **The same self-limiting effect seen in the real-LLM family D**:
whatever makes the closure expensive to prune also makes the investigation
expensive, and restart correct.

**Not represented by the environmental model at all:**
- probe noise. `ProbeOracle` is exact; a real counterfactual has D-026's noise
  floor, and a noisy clean verdict is another route to F5.
- variable event cost (`EVENT_COST` is constant).
- group testing as a probe primitive — `probe_group` exists and no gate used it.
- detector error. The flagged set is always correct here.

## 6. Classification of the result

**This is a hypothesis falsification, not an implementation bug.** H2's guard is
correctly implemented and correctly answers the question it asks. The hypothesis
— that *removability is a sufficient soundness guard for an ex-ante bottleneck
probe* — is what fails. Removability covers one of at least two ways a clean
verdict can be unsound, and the uncovered one leaves no observable trace.

It is also partly **insufficient test construction in the previous phase**: the
environmental suite that reported 94% contained redundancy but no comparator
blindness, so it tested the guard only against the failure the guard was built
from. F7 is the honest name for that.

## 7. What can and cannot be claimed

**Can:** H2 is falsified by a five-event counterexample; its guard does not
cover comparator-blind verdicts; its no-probe fallback reproduces the
originally-falsified behaviour; on an adversarial distribution it is beaten by
the gate it replaces.

**Cannot:** that H2 is useless — it still corrects the hub case (R3, M1, B3) and
correctly restarts on redundancy where H1 did not (D1). That the safety
invariant is meaningful evidence — it is near-vacuous here. That these
frequencies are representative — 14 cases, all constructed by me to be hard.

---

## FINAL DECISION

**FALSIFIED.**

Three false recoveries, two false restarts, 64% accuracy against `G_STRUCT`'s
71%, and 31 unsafe preservations against its 2 — on a suite of 14 cases built
to attack its assumptions rather than to measure it.

The counterexample is five events and the mechanism is exact: **a source can be
removable and a counterfactual can still be wrong.** `removability.check()`
asks whether redaction removes the information; it cannot ask whether the
comparator would notice the difference, and D-026 made the comparator
deliberately insensitive to text. H2's safety rests on treating one soundness
condition as if it were the whole of soundness.

The mechanism is not repaired here, per the brief.
