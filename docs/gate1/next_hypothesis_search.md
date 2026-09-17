# Gate 1 — search for a defensible next hypothesis

**Verdict: INCONCLUSIVE — INFORMATIONALLY UNDERSPECIFIED.**

No candidate survived environmental falsification, so **no real-LLM tokens were
spent** (Phase 7's stopping rule). The search produced something better than
another weak gate: a precise account of *why* every gate in this line behaved
the same way, and of exactly which piece of information the system does not
have.

The headline finding is uncomfortable and was verified twice:

> The Gate-1 rule `Â/N + f_structural ≤ 1` adds two terms that are
> **99–100% correlated**, because both are computed from the same contamination
> closure. A single constant reproduces the structural gate's decisions on
> **150 of 150** synthetic workflows, and the correlation is **0.9921 across 50
> real traces** — so it is not a simulator artefact.

| | |
|---|---|
| synthetic workflows | 150 (5 topologies × 10 patterns × 3 sizes) |
| real traces re-read | 50 (no new LLM runs) |
| real-LLM runs this phase | **0** |
| tests | **390 pass** (10 new) |
| `src/recovery/` changes | **none** |
| simulator bugs found and fixed | **2** (documented in §7) |

---

## 1. State after G_STRUCT and H2

- **G_STRUCT** falsified on hub topologies: `f_structural` saturates at 1.000
  while true influence is nil.
- **H2** falsified by a five-event counterexample: a source can be *removable*
  and the counterfactual still return *clean*, because removability and
  comparator observability are different properties.

The distinction that survived both: **exposure ≠ contamination ≠ causal
influence ≠ observable behavioural change ≠ recoverability ≠ economic value.**

## 2. Repository capabilities, classified by cost *and by soundness direction*

The previous audits classified signals as free/cheap/expensive. That was not
the useful axis. What decides whether a signal can help is **which direction it
is sound in**.

| signal | cost | sound for… | cannot establish |
|---|---|---|---|
| contamination closure / `structural_prior` | FREE | an **upper** bound on `f` | any lower bound |
| `Â` = region pairs × event cost | FREE | an **upper** bound on `A` | a tight estimate |
| code-path `record_structural` | FREE | exact verdicts, **tool events only** | anything about model events |
| `removability.check()` | FREE | that redaction *removes information* | that the comparator would *notice* |
| `carried_spans` | FREE | copy-like influence | semantic influence |
| counterfactual probe — **tainted** | EXPENSIVE | influence is **real** (grows region) | — |
| counterfactual probe — **clean** | EXPENSIVE | **nothing soundly** | it is the unsound direction |
| `group_test` | EXPENSIVE (1 call/group) | same as a probe | same limits |
| detector verdict | given | where to start | how far it spread |

**The asymmetry that decides the whole question:**

```
a TAINTED verdict  GROWS the contaminated region  -> sound, never enables INVESTIGATE
a CLEAN  verdict   SHRINKS it                     -> the only thing that can enable
                                                     INVESTIGATE, and the unsound one
```

## 3. Candidate hypotheses

| # | hypothesis | outcome |
|---|---|---|
| **C1 H_A** | ignore `f` entirely; decide on `Â/N` plus a fixed constant | **tested** — see §5. Not a survivor: it is a *control*, and it exposes the closure as vacuous |
| **C2 H_TIGHT** | tighten `Â` soundly (group testing settles an event's sources in one call, so bound by *events* not *pairs*) | **killed in §4** — 33/39 target cases are unreachable at any `Â`, including `Â = 0` |
| **C3 H_TAINT** | probe for *tainted* rather than *clean*, using the sound direction | **killed conceptually** — a sound verdict can only grow the region, so it can only make the gate restart more. Dead before implementation |
| **C4 H_LOWER** | find a sound **lower** bound on contamination so RESTART can be justified | **killed conceptually** — restart is already always safe, so a lower bound buys no safety; and the only free sound-clean evidence (code-path records) covers tool events, giving `f_lower ≈ 0` on every trace |
| **C5 H_SCOPE** | decide per *region* rather than per workflow; restart bad components, investigate good ones | **killed for the motivating case** — a hub closure is a single component, so it cannot be partitioned. Possibly useful for multi-source traces; **untested**, see §9 |
| **C6 H_WITNESS** | use independent downstream branches as witnesses of (non-)influence | **killed conceptually** — a hub topology provides no clean comparison group; every branch is downstream of the hub |

## 4. The impossibility argument, measured

A sound gate investigates iff `Â/N + f_upper ≤ 1`. Of G_STRUCT's **39 false
restarts**:

| | |
|---|---|
| `f_upper` already saturated at 1.0 | **33 / 39** |
| `f_upper < 1.0`, so a tighter `Â` could in principle flip it | 6 / 39 |

For the 33, investigating soundly would require `Â = 0` — impossible. For the
remaining 6, the required tightening ratio is **0.99–1.00×**: they sit exactly
on the boundary.

**So no sound tightening of either term fixes any of G_STRUCT's false
restarts.** That kills C2, and it is the reason C1 was worth testing instead.

## 5. The experiment that mattered — C1, the missing control

Every previous phase compared gates against each other and against B0/B1.
None compared them against *a constant*.

```
G_STRUCT  ==  H_A(f_assumed = 0.50)   on 150 / 150 cases
corr(Â/N, f_structural) = 1.0000  (synthetic)
                        = 0.9921  (50 real traces, re-read, no new runs)
```

| gate | acc | false RST | false REC | vs restart | unsafe |
|---|---|---|---|---|---|
| B0 always restart | 20% | 120 | 0 | 1.00 | 0 |
| B1 always investigate | 80% | 0 | 30 | 0.49 | 141 |
| **G_STRUCT** | **72%** | **39** | **3** | **0.58** | **58** |
| **H_A constant 0.50** | **72%** | **39** | **3** | **0.58** | **58** |
| H1 bottleneck | 90% | 0 | 15 | 0.46 | 141 |
| H2 sound bottleneck | 88% | 6 | 12 | 0.49 | 110 |
| H3 span | 84% | 0 | 24 | 0.46 | 141 |

**Why the two terms are one quantity:** `Â` counts exposure pairs *inside* the
closure; `f_structural` measures the cost *of* the closure. They are the same
object measured twice. Summing them is close to double-counting, and the rule
has roughly one degree of freedom.

## 6. Soundness, and what G_STRUCT's errors actually are

Verified on 150 workflows, **0 violations**: `f_structural ≥ f_true` and
`Â ≥ A`. Therefore

```
Â/N + f_structural ≤ 1   implies   A/N + f_true ≤ 1
```

so **G_STRUCT's INVESTIGATE decisions are correct**, and its only real error is
the false restart. Its 3 recorded "false recoveries" are **exact ties**
(estimate = truth = 1.0000; the gate uses `≤`, the oracle `<`) and carry **zero
regret**.

Read against the others: H1 and H3 eliminate false restarts entirely, and pay
for it with **15 and 24 false recoveries** and the maximum unsafe count (141,
equal to always-investigating). **Every measured improvement over G_STRUCT was
bought by taking an unsound step**, and each was subsequently falsified by a
counterexample exploiting exactly that step.

## 7. Two simulator bugs, found and documented separately

Both were caught by the soundness test itself, and both would have corrupted
the result in our favour or against it:

1. **Impossible influence edges.** The `all` pattern planted `bad → hub` for
   *every* hub, including hubs that never had the source in their exposures.
   `f_true` (1.000) then exceeded `f_structural` (0.502) and the sound bound
   looked unsound.
2. **Unobservable late influence.** The `deep` patterns planted influence on an
   already-logged analyst event whose recorded exposures did not contain the
   source — an unobservable cause no gate could find, which would have scored
   as every gate's failure.

Both fixed in `src/eval/gate1_sim.py`; the invariant is now asserted by
`test_a_source_cannot_influence_an_event_it_was_never_in`. Nothing in
`src/recovery/` was touched.

## 8. Safety analysis, and a tautology named as one

The invariant *"a gate cannot claim safe what the investigation would call
unsafe"* held in the H2 break test — and it holds **tautologically**: the gate's
evidence channel is the same probe the investigation uses, so its evidence is a
strict subset. Passing it is not evidence of safety and is not quoted as such.

The informative number is the unsafe total, and it tracks one thing only — **how
often the gate chooses to investigate**:

```
B0    0     (restarts always)
G_STRUCT 58
H2   110
H1/H3/B1 141  (investigate nearly always)
```

Safety here is bought by restarting, not by any gate's cleverness.

## 9. Final verdict per candidate

| candidate | verdict |
|---|---|
| C1 H_A | **not a survivor.** It equals G_STRUCT, is not sound, and is not a mechanism — it is the control that shows the closure carries no decision-relevant information |
| C2 H_TIGHT | **killed** — 33/39 target cases unreachable at any `Â` |
| C3 H_TAINT | **killed conceptually** — sound verdicts only grow the region |
| C4 H_LOWER | **killed** — `f_lower ≈ 0` on every trace; and a lower bound buys no safety |
| C5 H_SCOPE | **killed for the motivating case**; genuinely untested for multi-source traces |
| C6 H_WITNESS | **killed conceptually** — no clean comparison group under a hub |

**Zero survivors ⇒ no real-LLM phase**, per Phase 7.

## 10. The remaining research gap, stated precisely

A cheap, sound, ex-ante gate needs one of two things, and the system has
neither:

1. **A sound lower bound on contamination.** Would let RESTART be justified
   rather than assumed. The only free sound-clean evidence is
   `record_structural`, which covers tool events only, so `f_lower ≈ 0`.
2. **A clean verdict that can be trusted.** Two independent failure modes make
   it untrustworthy — redundancy (`docs/03` #16, detectable via removability)
   and comparator blindness (H2's counterexample, **not** detectable by
   anything the system computes).

Without one of these, the sound region of the decision space is exactly what
G_STRUCT already occupies — and that region is reproducible by a constant.

## 11. Recommended next experiment

**Not another gate.** Two directions, in order:

1. **Make `A` cheap enough that the decision stops mattering.** The regret of
   wrongly investigating is bounded by `A` (Gate 2 caps the replay). At
   `A/N = 0.05` — which the counterfactual-only configuration reached on real
   traces in the break test — the worst case of always investigating is a 5%
   overspend, and **no gate is worth its complexity**. This is a measurement
   question about the investigation, not a decision question.
2. **Change the action space, not the estimator.** `restart only the region
   that cannot be soundly probed`, rather than restart-all-or-investigate-all.
   This is a planner question; C5 is its degenerate gate-shaped form and it
   failed for that reason. It is the one direction here that has not been
   tested and is not already known to be informationally blocked.

---

## FINAL VERDICT

**INCONCLUSIVE — INFORMATIONALLY UNDERSPECIFIED.**

Six candidates were generated; four were killed by conceptual counterexamples
before any implementation; one was killed by measurement (33/39 cases
unreachable); one was tested and turned out to be a control rather than a
mechanism. No candidate survived, so no real-LLM tokens were spent.

What was established instead is sharper than another gate would have been: the
decision rule this project has been refining for three phases adds two terms
that are the **same quantity measured twice** (correlation 1.0000 synthetic,
0.9921 on 50 real traces), a **constant reproduces it exactly on 150/150
cases**, and the only operation that could improve it — a trusted clean verdict
— is precisely the one the system cannot soundly produce.
